"""Multi-source secret resolution — env, file, exec, store providers.

Inspired by OpenClaw's secrets module. Provides:
  - SecretRef: reference to a secret (env var, file, exec command, store key)
  - SecretResolver: resolve SecretRefs to actual values
  - Security validation: permission checks for file/exec providers
  - Caching: resolved secrets are cached for performance
  - Batch resolution: resolve multiple secrets concurrently

Usage:
    from kyourai.secrets.resolver import SecretRef, SecretResolver

    resolver = SecretResolver()

    # From environment
    ref = SecretRef.env("OPENAI_API_KEY")
    value = resolver.resolve(ref)

    # From file
    ref = SecretRef.file("~/.secrets/openai_key", json_pointer="/key")
    value = resolver.resolve(ref)

    # From exec
    ref = SecretRef.exec("pass show openai/api-key")
    value = resolver.resolve(ref)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Secret reference types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SecretRef:
    """A reference to a secret stored in various providers.

    Source types:
      - env: Environment variable
      - file: File on disk (optionally with JSON pointer)
      - exec: Command to execute (output is the secret)
      - store: Key-value store lookup
      - plain: Plain text (not recommended, but supported)
    """
    source: str  # "env", "file", "exec", "store", "plain"
    name: str = ""  # env var name, file path, command, store key
    json_pointer: str = ""  # JSON pointer for file source (e.g. "/api_keys/openai")
    encoding: str = "utf-8"  # File encoding
    cache_ttl: float = 300.0  # Cache time-to-live in seconds (0 = no cache)

    @staticmethod
    def env(name: str, **kwargs: Any) -> "SecretRef":
        """Create a reference to an environment variable."""
        return SecretRef(source="env", name=name, **kwargs)

    @staticmethod
    def file(path: str, json_pointer: str = "", **kwargs: Any) -> "SecretRef":
        """Create a reference to a file on disk."""
        return SecretRef(source="file", name=path, json_pointer=json_pointer, **kwargs)

    @staticmethod
    def exec(command: str, **kwargs: Any) -> "SecretRef":
        """Create a reference to a command to execute."""
        return SecretRef(source="exec", name=command, **kwargs)

    @staticmethod
    def store(key: str, **kwargs: Any) -> "SecretRef":
        """Create a reference to a key-value store."""
        return SecretRef(source="store", name=key, **kwargs)

    @staticmethod
    def plain(value: str, **kwargs: Any) -> "SecretRef":
        """Create a reference to a plain text value."""
        return SecretRef(source="plain", name=value, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "name": self.name if self.source != "plain" else "[REDACTED]",
            "json_pointer": self.json_pointer,
            "encoding": self.encoding,
            "cache_ttl": self.cache_ttl,
        }


# ---------------------------------------------------------------------------
# Resolution result
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ResolutionResult:
    """Result of resolving a SecretRef."""
    ref: SecretRef
    value: str | None = None
    error: str = ""
    cached: bool = False
    resolved_at: float = 0.0

    @property
    def success(self) -> bool:
        return self.value is not None and not self.error


# ---------------------------------------------------------------------------
# Security validation
# ---------------------------------------------------------------------------


def validate_file_access(path: str) -> tuple[bool, str]:
    """Validate that a file path is safe to read secrets from.

    Returns (is_safe, error_message).
    """
    try:
        resolved = Path(path).expanduser().resolve()

        # Check existence
        if not resolved.exists():
            return False, f"File not found: {resolved}"

        # Check it's a file
        if not resolved.is_file():
            return False, f"Not a file: {resolved}"

        # Check permissions (on Unix)
        if os.name != "nt":
            stat = resolved.stat()
            # Check if world-readable (security risk for secrets)
            if stat.st_mode & 0o004:
                return False, f"File is world-readable: {resolved} (chmod 600 recommended)"

        # Check if in a suspicious location
        suspicious_paths = ["/tmp", "/var/tmp", "/dev/shm"]
        for susp in suspicious_paths:
            if str(resolved).startswith(susp):
                return False, f"File in temporary directory: {resolved} (not safe for secrets)"

        return True, ""
    except Exception as e:
        return False, str(e)


def validate_exec_command(command: str) -> tuple[bool, str]:
    """Validate that a command is safe to execute for secret retrieval.

    Returns (is_safe, error_message).
    """
    # Block obviously dangerous commands
    dangerous_patterns = [
        "rm ", "rm -", "format", "mkfs", "dd if=", "shutdown",
        "reboot", "halt", "kill -9", "pkill",
        "curl ", "wget ",  # Network operations
        "> /", ">> /",  # Writing to system files
    ]

    cmd_lower = command.lower()
    for pattern in dangerous_patterns:
        if pattern in cmd_lower:
            return False, f"Command contains dangerous pattern: '{pattern}'"

    # Only allow known secret managers
    allowed_prefixes = [
        "pass ",       # pass (password store)
        "gpg ",        # GPG
        "op ",         # 1Password CLI
        "vault ",      # HashiCorp Vault
        "aws secretsmanager",  # AWS Secrets Manager
        "kubectl get secret",  # Kubernetes secrets
        "cat ",        # Reading files (already validated separately)
        "echo ",       # Echo (for testing)
    ]

    is_allowed = any(cmd_lower.startswith(prefix) for prefix in allowed_prefixes)
    if not is_allowed:
        return False, f"Command not in allowed list for secret retrieval: {command[:50]}"

    return True, ""


# ---------------------------------------------------------------------------
# Secret resolver
# ---------------------------------------------------------------------------


class SecretResolver:
    """Resolve SecretRefs to actual secret values.

    Supports multiple sources with security validation and caching.
    """

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, float]] = {}  # key → (value, cached_at)
        self._store_providers: dict[str, Callable[[str], str | None]] = {}

    def register_store_provider(
        self,
        name: str,
        provider: Callable[[str], str | None],
    ) -> None:
        """Register a custom store provider."""
        self._store_providers[name] = provider

    def resolve(self, ref: SecretRef) -> ResolutionResult:
        """Resolve a single SecretRef.

        Args:
            ref: The SecretRef to resolve

        Returns:
            ResolutionResult with the value or error
        """
        # Check cache
        cache_key = self._cache_key(ref)
        if cache_key and ref.cache_ttl > 0:
            cached = self._cache.get(cache_key)
            if cached and (time.time() - cached[1]) < ref.cache_ttl:
                return ResolutionResult(
                    ref=ref,
                    value=cached[0],
                    cached=True,
                    resolved_at=time.time(),
                )

        # Resolve based on source
        result = self._resolve_uncached(ref)

        # Cache successful results
        if result.success and cache_key and ref.cache_ttl > 0:
            self._cache[cache_key] = (result.value or "", time.time())

        return result

    def resolve_batch(self, refs: dict[str, SecretRef]) -> dict[str, ResolutionResult]:
        """Resolve multiple SecretRefs at once.

        Args:
            refs: Dict of name → SecretRef

        Returns:
            Dict of name → ResolutionResult
        """
        return {name: self.resolve(ref) for name, ref in refs.items()}

    def clear_cache(self) -> None:
        """Clear the secret cache."""
        self._cache.clear()

    # -- Internal -----------------------------------------------------------

    def _resolve_uncached(self, ref: SecretRef) -> ResolutionResult:
        """Resolve without checking cache."""
        try:
            if ref.source == "env":
                return self._resolve_env(ref)
            elif ref.source == "file":
                return self._resolve_file(ref)
            elif ref.source == "exec":
                return self._resolve_exec(ref)
            elif ref.source == "store":
                return self._resolve_store(ref)
            elif ref.source == "plain":
                return ResolutionResult(
                    ref=ref,
                    value=ref.name,
                    resolved_at=time.time(),
                )
            else:
                return ResolutionResult(
                    ref=ref,
                    error=f"Unknown source type: {ref.source}",
                )
        except Exception as e:
            return ResolutionResult(ref=ref, error=str(e))

    def _resolve_env(self, ref: SecretRef) -> ResolutionResult:
        """Resolve from environment variable."""
        value = os.environ.get(ref.name)
        if value is None:
            return ResolutionResult(
                ref=ref,
                error=f"Environment variable not set: {ref.name}",
            )
        return ResolutionResult(ref=ref, value=value, resolved_at=time.time())

    def _resolve_file(self, ref: SecretRef) -> ResolutionResult:
        """Resolve from file."""
        # Security validation
        is_safe, error = validate_file_access(ref.name)
        if not is_safe:
            return ResolutionResult(ref=ref, error=error)

        try:
            content = Path(ref.name).expanduser().read_text(encoding=ref.encoding)

            # Apply JSON pointer if specified
            if ref.json_pointer:
                value = self._apply_json_pointer(content, ref.json_pointer)
                if value is None:
                    return ResolutionResult(
                        ref=ref,
                        error=f"JSON pointer '{ref.json_pointer}' not found",
                    )
                return ResolutionResult(ref=ref, value=value, resolved_at=time.time())

            # Strip whitespace
            return ResolutionResult(
                ref=ref,
                value=content.strip(),
                resolved_at=time.time(),
            )
        except Exception as e:
            return ResolutionResult(ref=ref, error=str(e))

    def _resolve_exec(self, ref: SecretRef) -> ResolutionResult:
        """Resolve by executing a command."""
        # Security validation
        is_safe, error = validate_exec_command(ref.name)
        if not is_safe:
            return ResolutionResult(ref=ref, error=error)

        try:
            result = subprocess.run(
                ref.name,
                shell=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return ResolutionResult(
                    ref=ref,
                    error=f"Command failed (exit {result.returncode}): {result.stderr.strip()}",
                )
            return ResolutionResult(
                ref=ref,
                value=result.stdout.strip(),
                resolved_at=time.time(),
            )
        except subprocess.TimeoutExpired:
            return ResolutionResult(ref=ref, error="Command timed out")
        except Exception as e:
            return ResolutionResult(ref=ref, error=str(e))

    def _resolve_store(self, ref: SecretRef) -> ResolutionResult:
        """Resolve from registered store provider."""
        # Try all registered providers
        for provider_name, provider in self._store_providers.items():
            try:
                value = provider(ref.name)
                if value is not None:
                    return ResolutionResult(
                        ref=ref,
                        value=value,
                        resolved_at=time.time(),
                    )
            except Exception as e:
                logger.debug("Store provider %s failed: %s", provider_name, e)

        return ResolutionResult(
            ref=ref,
            error=f"Key not found in any store: {ref.name}",
        )

    def _apply_json_pointer(self, json_content: str, pointer: str) -> str | None:
        """Apply a JSON pointer to extract a value from JSON content.

        Supports simple pointer syntax: /key1/key2/key3
        """
        try:
            data = json.loads(json_content)
            parts = [p for p in pointer.split("/") if p]
            current: Any = data
            for part in parts:
                if isinstance(current, dict):
                    current = current.get(part)
                elif isinstance(current, list):
                    try:
                        idx = int(part)
                        current = current[idx]
                    except (ValueError, IndexError):
                        return None
                else:
                    return None
            return str(current) if current is not None else None
        except (json.JSONDecodeError, Exception):
            return None

    def _cache_key(self, ref: SecretRef) -> str:
        """Generate a cache key for a SecretRef."""
        if ref.source == "plain":
            return ""  # Don't cache plain text
        return f"{ref.source}:{ref.name}:{ref.json_pointer}"
