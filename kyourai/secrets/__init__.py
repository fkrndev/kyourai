"""Secrets module — multi-source secret resolution."""

from kyourai.secrets.resolver import (
    SecretRef,
    SecretResolver,
    ResolutionResult,
    validate_file_access,
    validate_exec_command,
)

__all__ = [
    "SecretRef",
    "SecretResolver",
    "ResolutionResult",
    "validate_file_access",
    "validate_exec_command",
]
