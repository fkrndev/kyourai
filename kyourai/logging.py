"""Logging setup for Kyourai — profile-aware, structured logging.

Mirrors Hermes' hermes_logging.py pattern but trimmed to kyourai's needs.
Logs go to:
  - ~/.kyourai/logs/agent.log    (INFO+, console + file)
  - ~/.kyourai/logs/errors.log   (WARNING+, file only)
  - stderr                       (WARNING+ when no file handler)

All paths are profile-aware via get_kyourai_home() — switching KYOURAI_HOME
mid-process picks up the new log directory on next setup_logging() call.

Usage:
  from kyourai.logging import setup_logging
  setup_logging(verbose=False)  # call once at startup
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from kyourai.constants import get_kyourai_home

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_LOG_BYTES = 5 * 1024 * 1024  # 5 MB per file
_BACKUP_COUNT = 3  # keep 3 rotated files

_configured = False


def get_log_dir() -> Path:
    """Return the log directory for the active KYOURAI_HOME."""
    return get_kyourai_home() / "logs"


def setup_logging(
    *,
    verbose: bool = False,
    log_to_file: bool = True,
    log_to_stderr: bool = True,
) -> None:
    """Configure logging for Kyourai.

    Args:
        verbose: If True, set root level to DEBUG; otherwise INFO.
        log_to_file: If True, write to ~/.kyourai/logs/agent.log + errors.log.
        log_to_stderr: If True, also emit to stderr (WARNING+).
    """
    global _configured

    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)

    # Remove existing handlers to avoid duplicates on re-init
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # Console (stderr) handler — WARNING+ by default, DEBUG+ if verbose
    if log_to_stderr:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.DEBUG if verbose else logging.WARNING)
        console.setFormatter(formatter)
        root.addHandler(console)

    # File handlers
    if log_to_file:
        log_dir = get_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)

        # agent.log — INFO+
        agent_log = log_dir / "agent.log"
        agent_handler = RotatingFileHandler(
            agent_log,
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        agent_handler.setLevel(level)
        agent_handler.setFormatter(formatter)
        root.addHandler(agent_handler)

        # errors.log — WARNING+
        error_log = log_dir / "errors.log"
        error_handler = RotatingFileHandler(
            error_log,
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        error_handler.setLevel(logging.WARNING)
        error_handler.setFormatter(formatter)
        root.addHandler(error_handler)

    _configured = True


def is_configured() -> bool:
    """Return True if setup_logging() has been called."""
    return _configured


def get_log_paths() -> dict[str, Path | None]:
    """Return paths to active log files (for display in CLI)."""
    log_dir = get_log_dir()
    return {
        "agent": log_dir / "agent.log" if log_dir.exists() else None,
        "errors": log_dir / "errors.log" if log_dir.exists() else None,
    }
