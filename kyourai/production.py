"""Production hardening — config validation, graceful shutdown, health checks.

This module provides:
  - Config validation + migration (validate config.yaml at startup)
  - Graceful shutdown (SIGTERM/SIGINT handling, cleanup resources)
  - Detailed health check (DB status, memory status, active sessions)
  - Structured logging (JSON format for observability)

Usage:
  from kyourai.production import (
      validate_config,
      GracefulShutdown,
      HealthChecker,
      setup_structured_logging,
  )
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config validation + migration
# ---------------------------------------------------------------------------

# Current config schema version
CONFIG_VERSION = 1

# Required config keys with defaults
CONFIG_DEFAULTS: dict[str, Any] = {
    "agent": {
        "model": "openai:gpt-4o",
        "max_turns": 20,
        "enable_curator": True,
        "enable_skills": True,
        "enable_cron": True,
        "verify_output": False,
    },
    "memory": {
        "provider": "holographic",
        "curator_interval": 300,
        "trust_decay_rate": 0.01,
    },
    "api": {
        "host": "127.0.0.1",
        "port": 18789,
    },
    "logging": {
        "level": "INFO",
        "structured": False,
    },
}

# Known config keys (for validation — warns on unknown keys)
KNOWN_KEYS: dict[str, set[str]] = {
    "agent": {"model", "max_turns", "enable_curator", "enable_skills",
              "enable_cron", "verify_output", "extra_instructions",
              "skills_allowlist", "language", "response_style", "code_style"},
    "memory": {"provider", "curator_interval", "trust_decay_rate",
               "hrr_dim", "max_facts", "recall_threshold"},
    "api": {"host", "port", "api_key", "cors_origins"},
    "logging": {"level", "structured", "file"},
    "tools": {"enabled", "terminal_timeout", "web_search_provider"},
    "subagent": {"enabled", "max_concurrent", "model"},
    "rate_limit": {"enabled", "max_requests", "window_seconds"},
    "retry": {"max_attempts", "backoff_base", "backoff_max"},
}


@dataclass(slots=True)
class ValidationResult:
    """Result of config validation."""
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    migrated: bool = False
    config: dict[str, Any] = field(default_factory=dict)


def validate_config(config_path: Path | None = None) -> ValidationResult:
    """Validate and migrate config file.

    Args:
        config_path: Path to config.yaml. If None, uses default location.

    Returns:
        ValidationResult with validation status, errors, warnings, and
        the normalized config.
    """
    from kyourai.constants import get_kyourai_home

    if config_path is None:
        config_path = get_kyourai_home() / "config.yaml"

    result = ValidationResult(valid=True)

    # If config doesn't exist, create with defaults
    if not config_path.exists():
        result.config = _deep_copy_defaults(CONFIG_DEFAULTS)
        result.warnings.append(
            f"Config file not found at {config_path}, using defaults"
        )
        return result

    # Load config
    try:
        raw = config_path.read_text(encoding="utf-8")
        config = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        result.valid = False
        result.errors.append(f"Invalid YAML: {e}")
        return result
    except Exception as e:
        result.valid = False
        result.errors.append(f"Failed to read config: {e}")
        return result

    # Migrate: check version
    version = config.get("_version", 0)
    if version < CONFIG_VERSION:
        config = _migrate_config(config, version)
        result.migrated = True

    # Validate: check required keys and fill defaults
    config = _merge_defaults(config, CONFIG_DEFAULTS)

    # Validate: check for unknown keys
    for section, keys in config.items():
        if section.startswith("_"):
            continue
        if not isinstance(keys, dict):
            result.warnings.append(
                f"Config section '{section}' should be a dict, got {type(keys).__name__}"
            )
            continue
        known = KNOWN_KEYS.get(section, set())
        if known:
            unknown = set(keys.keys()) - known
            if unknown:
                result.warnings.append(
                    f"Unknown keys in [{section}]: {', '.join(sorted(unknown))}"
                )

    # Validate: type checks for critical values
    agent = config.get("agent", {})
    if not isinstance(agent.get("model", ""), str):
        result.errors.append("agent.model must be a string")
    if not isinstance(agent.get("max_turns", 20), int) or agent.get("max_turns", 20) < 1:
        result.errors.append("agent.max_turns must be a positive integer")

    memory = config.get("memory", {})
    interval = memory.get("curator_interval", 300)
    if not isinstance(interval, (int, float)) or interval < 0:
        result.errors.append("memory.curator_interval must be a non-negative number")

    api = config.get("api", {})
    port = api.get("port", 18789)
    if not isinstance(port, int) or port < 1 or port > 65535:
        result.errors.append("api.port must be between 1 and 65535")

    result.valid = len(result.errors) == 0
    result.config = config
    return result


def _deep_copy_defaults(defaults: dict[str, Any]) -> dict[str, Any]:
    """Deep copy the defaults dict."""
    import copy
    return copy.deepcopy(defaults)


def _migrate_config(config: dict[str, Any], from_version: int) -> dict[str, Any]:
    """Migrate config from old version to current."""
    if from_version < 1:
        # v0 → v1: no breaking changes, just add _version
        config["_version"] = CONFIG_VERSION
    return config


def _merge_defaults(config: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Merge config with defaults (config takes precedence)."""
    result = _deep_copy_defaults(defaults)
    for key, value in config.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key].update(value)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


class GracefulShutdown:
    """Handle SIGTERM/SIGINT for graceful shutdown.

    Registers signal handlers and provides a way to register cleanup
    callbacks that run before the process exits.

    Usage:
        shutdown = GracefulShutdown()
        shutdown.register(cleanup_db)
        shutdown.register(cleanup_memory)
        # ... run main loop ...
        # On SIGTERM/SIGINT, cleanup callbacks run in reverse order
    """

    def __init__(self) -> None:
        self._callbacks: list[Callable[[], None]] = []
        self._shutting_down = False
        self._registered = False

    def register(self, callback: Callable[[], None]) -> None:
        """Register a cleanup callback. Callbacks run in reverse order."""
        self._callbacks.append(callback)

    def install(self) -> None:
        """Install signal handlers."""
        if self._registered:
            return
        self._registered = True

        # Windows only supports SIGINT (Ctrl+C)
        signal.signal(signal.SIGINT, self._handle_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """Signal handler — run cleanup and exit."""
        if self._shutting_down:
            # Second signal — force exit
            logger.warning("Second signal received, forcing exit")
            sys.exit(1)

        self._shutting_down = True
        logger.info("Shutdown signal received (signal %d), cleaning up...", signum)

        # Run callbacks in reverse order
        for callback in reversed(self._callbacks):
            try:
                callback()
            except Exception as e:
                logger.error("Cleanup callback failed: %s", e)

        logger.info("Shutdown complete")
        sys.exit(0)

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down


# ---------------------------------------------------------------------------
# Health checker
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HealthStatus:
    """Health check result for a single component."""
    name: str
    healthy: bool
    detail: str = ""
    latency_ms: float = 0.0


@dataclass(slots=True)
class HealthReport:
    """Full health check report."""
    status: str  # "ok" or "degraded"
    version: str
    timestamp: float
    components: list[HealthStatus] = field(default_factory=list)

    @property
    def all_healthy(self) -> bool:
        return all(c.healthy for c in self.components)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "version": self.version,
            "timestamp": self.timestamp,
            "components": [
                {
                    "name": c.name,
                    "healthy": c.healthy,
                    "detail": c.detail,
                    "latency_ms": round(c.latency_ms, 2),
                }
                for c in self.components
            ],
        }


class HealthChecker:
    """Check health of all Kyourai components."""

    VERSION = "0.1.0"

    def check_all(self) -> HealthReport:
        """Run all health checks."""
        components: list[HealthStatus] = []

        # Check database
        components.append(self._check_database())

        # Check memory store
        components.append(self._check_memory())

        # Check KYOURAI_HOME
        components.append(self._check_home_dir())

        # Check config
        components.append(self._check_config())

        all_healthy = all(c.healthy for c in components)
        return HealthReport(
            status="ok" if all_healthy else "degraded",
            version=self.VERSION,
            timestamp=time.time(),
            components=components,
        )

    def _check_database(self) -> HealthStatus:
        """Check if session database is accessible."""
        start = time.time()
        try:
            from kyourai.state import SessionDB
            db = SessionDB()
            count = db.count_sessions()
            db.close()
            latency = (time.time() - start) * 1000
            return HealthStatus(
                name="database",
                healthy=True,
                detail=f"{count} sessions",
                latency_ms=latency,
            )
        except Exception as e:
            return HealthStatus(
                name="database",
                healthy=False,
                detail=str(e),
                latency_ms=(time.time() - start) * 1000,
            )

    def _check_memory(self) -> HealthStatus:
        """Check if memory store is accessible."""
        start = time.time()
        try:
            from kyourai.memory.holographic.store import MemoryStore
            store = MemoryStore()
            count = store.count_facts()
            latency = (time.time() - start) * 1000
            return HealthStatus(
                name="memory",
                healthy=True,
                detail=f"{count} facts",
                latency_ms=latency,
            )
        except Exception as e:
            return HealthStatus(
                name="memory",
                healthy=False,
                detail=str(e),
                latency_ms=(time.time() - start) * 1000,
            )

    def _check_home_dir(self) -> HealthStatus:
        """Check if KYOURAI_HOME is accessible."""
        try:
            from kyourai.constants import get_kyourai_home
            home = get_kyourai_home()
            exists = home.exists()
            writable = os.access(str(home), os.W_OK) if exists else False
            return HealthStatus(
                name="home_dir",
                healthy=exists and writable,
                detail=str(home),
            )
        except Exception as e:
            return HealthStatus(
                name="home_dir",
                healthy=False,
                detail=str(e),
            )

    def _check_config(self) -> HealthStatus:
        """Check if config is valid."""
        try:
            result = validate_config()
            return HealthStatus(
                name="config",
                healthy=result.valid,
                detail=f"valid={result.valid}, warnings={len(result.warnings)}",
            )
        except Exception as e:
            return HealthStatus(
                name="config",
                healthy=False,
                detail=str(e),
            )


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


class StructuredFormatter(logging.Formatter):
    """JSON formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        import json

        log_entry = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Add extra fields if present
        for key, value in record.__dict__.items():
            if key not in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            }:
                try:
                    json.dumps(value)
                    log_entry[key] = value
                except (TypeError, ValueError):
                    log_entry[key] = str(value)

        return json.dumps(log_entry, ensure_ascii=False)


def setup_structured_logging(
    level: str = "INFO",
    structured: bool = False,
    log_file: Path | None = None,
) -> None:
    """Configure logging for production.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        structured: If True, output JSON structured logs
        log_file: Optional file to write logs to
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    # Console handler
    console = logging.StreamHandler(sys.stderr)
    if structured:
        console.setFormatter(StructuredFormatter())
    else:
        console.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    root.addHandler(console)

    # File handler (optional)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
        file_handler.setFormatter(StructuredFormatter())
        root.addHandler(file_handler)

    # Reduce noise from libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.INFO)
