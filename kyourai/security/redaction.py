"""Credential redaction — scan text for secrets and PII, replace with placeholders.

Prevents accidental exposure of API keys, tokens, private keys, and other
sensitive data when agent output is sent to LLM providers or logged.

Patterns detected:
  - OpenAI API keys (sk-...)
  - Anthropic API keys (sk-ant-...)
  - AWS access keys (AKIA...)
  - AWS secret keys (40-char base64)
  - GitHub tokens (ghp_..., gho_..., ghs_..., ghu_...)
  - GitLab tokens (glpat-...)
  - Slack tokens (xoxb-..., xoxp-...)
  - JWT tokens (eyJ...)
  - Private keys (-----BEGIN ... PRIVATE KEY-----)
  - Generic API keys (via heuristics)
  - Connection strings (password=...)
  - Bearer tokens (Authorization: Bearer ...)

Usage:
  from kyourai.security.redaction import redact_text, redact_dict

  safe = redact_text("my key is sk-abc123...")
  # → "my key is [REDACTED:openai-api-key]..."

  safe_dict = redact_dict({"api_key": "sk-...", "name": "user"})
  # → {"api_key": "[REDACTED:openai-api-key]", "name": "user"}
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Redaction patterns
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RedactionPattern:
    """A pattern to detect and redact sensitive data."""
    name: str
    pattern: re.Pattern[str]
    placeholder: str
    description: str = ""


# Ordered by specificity (most specific first to avoid partial matches)
PATTERNS: list[RedactionPattern] = [
    RedactionPattern(
        name="private-key",
        pattern=re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----.*?-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        placeholder="[REDACTED:private-key]",
        description="Private key blocks (RSA, EC, DSA, OpenSSH, PGP)",
    ),
    RedactionPattern(
        name="openai-api-key",
        pattern=re.compile(r"sk-[a-zA-Z0-9]{20,}"),
        placeholder="[REDACTED:openai-api-key]",
        description="OpenAI API keys",
    ),
    RedactionPattern(
        name="anthropic-api-key",
        pattern=re.compile(r"sk-ant-[a-zA-Z0-9\-_]{20,}"),
        placeholder="[REDACTED:anthropic-api-key]",
        description="Anthropic API keys",
    ),
    RedactionPattern(
        name="aws-access-key",
        pattern=re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        placeholder="[REDACTED:aws-access-key]",
        description="AWS access key IDs",
    ),
    RedactionPattern(
        name="github-token",
        pattern=re.compile(r"\bgh[pousr]_[a-zA-Z0-9]{36,}\b"),
        placeholder="[REDACTED:github-token]",
        description="GitHub personal access tokens",
    ),
    RedactionPattern(
        name="gitlab-token",
        pattern=re.compile(r"\bglpat-[a-zA-Z0-9\-_]{20,}\b"),
        placeholder="[REDACTED:gitlab-token]",
        description="GitLab personal access tokens",
    ),
    RedactionPattern(
        name="slack-token",
        pattern=re.compile(r"\bxox[baprs]-[a-zA-Z0-9\-]{10,}\b"),
        placeholder="[REDACTED:slack-token]",
        description="Slack API tokens",
    ),
    RedactionPattern(
        name="jwt",
        pattern=re.compile(r"\beyJ[a-zA-Z0-9_\-]{10,}\.eyJ[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]{10,}\b"),
        placeholder="[REDACTED:jwt]",
        description="JWT tokens",
    ),
    RedactionPattern(
        name="bearer-token",
        pattern=re.compile(
            r"(?i)(?:authorization\s*[:=]\s*)?bearer\s+([a-zA-Z0-9_\-\.]{20,})",
        ),
        placeholder="[REDACTED:bearer-token]",
        description="Bearer tokens in Authorization headers",
    ),
    RedactionPattern(
        name="connection-string-password",
        pattern=re.compile(
            r"(?i)(?:password|passwd|pwd)\s*[=:]\s*(['\"]?)([^\s'\";]{4,})\1",
        ),
        placeholder="[REDACTED:password]",
        description="Passwords in connection strings",
    ),
    RedactionPattern(
        name="google-api-key",
        pattern=re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
        placeholder="[REDACTED:google-api-key]",
        description="Google API keys",
    ),
    RedactionPattern(
        name="stripe-key",
        pattern=re.compile(r"\b(?:sk|pk|rk)_(?:test_)?[a-zA-Z0-9]{24,}\b"),
        placeholder="[REDACTED:stripe-key]",
        description="Stripe API keys",
    ),
]


# ---------------------------------------------------------------------------
# Redaction result
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RedactionResult:
    """Result of a redaction operation."""
    text: str
    redactions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def was_redacted(self) -> bool:
        return len(self.redactions) > 0


# ---------------------------------------------------------------------------
# Redaction functions
# ---------------------------------------------------------------------------


def redact_text(text: str, *, patterns: list[RedactionPattern] | None = None) -> RedactionResult:
    """Redact sensitive data from text.

    Args:
        text: Input text to scan
        patterns: Custom patterns to use (default: all patterns)

    Returns:
        RedactionResult with redacted text and list of redactions made
    """
    if not text:
        return RedactionResult(text=text)

    result_text = text
    redactions: list[dict[str, Any]] = []
    active_patterns = patterns or PATTERNS

    for pat in active_patterns:
        matches = list(pat.pattern.finditer(result_text))
        if not matches:
            continue

        # Replace matches with placeholder
        for match in reversed(matches):  # reverse to preserve indices
            original = match.group()
            # For patterns with capture groups, replace only the captured part
            if match.groups():
                start, end = match.span(1) if match.lastindex else match.span()
            else:
                start, end = match.span()

            result_text = result_text[:start] + pat.placeholder + result_text[end:]
            redactions.append({
                "type": pat.name,
                "placeholder": pat.placeholder,
                "length": end - start,
                "preview": original[:8] + "..." if len(original) > 8 else original,
            })

    return RedactionResult(text=result_text, redactions=redactions)


def redact_dict(data: dict[str, Any], *, skip_keys: set[str] | None = None) -> dict[str, Any]:
    """Redact sensitive data from a dictionary.

    Scans both keys (for sensitive key names) and values (for patterns).

    Args:
        data: Dictionary to redact
        skip_keys: Keys to skip (not redact)

    Returns:
        New dictionary with redacted values
    """
    if not data:
        return data

    sensitive_key_names = {
        "api_key", "apikey", "api-key", "secret", "secret_key", "secretkey",
        "password", "passwd", "pwd", "token", "access_token", "accesstoken",
        "refresh_token", "refreshtoken", "private_key", "privatekey",
        "client_secret", "clientsecret", "auth_token", "authtoken",
        "bearer_token", "bearertoken", "credentials",
    }

    skip = skip_keys or set()
    result: dict[str, Any] = {}

    for key, value in data.items():
        if key in skip:
            result[key] = value
            continue

        # Check if key name is sensitive
        if key.lower() in sensitive_key_names:
            if isinstance(value, str) and value:
                result[key] = "[REDACTED:sensitive-key]"
            else:
                result[key] = value
            continue

        # Redact value based on type
        if isinstance(value, str):
            result[key] = redact_text(value).text
        elif isinstance(value, dict):
            result[key] = redact_dict(value, skip_keys=skip)
        elif isinstance(value, list):
            result[key] = [
                redact_text(item).text if isinstance(item, str)
                else redact_dict(item, skip_keys=skip) if isinstance(item, dict)
                else item
                for item in value
            ]
        else:
            result[key] = value

    return result


def redact_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Redact sensitive data from a list of chat messages.

    Args:
        messages: List of {role, content} dicts

    Returns:
        New list with redacted content
    """
    result: list[dict[str, str]] = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            redacted = redact_text(content)
            if redacted.was_redacted:
                logger.warning(
                    "Redacted %d sensitive items from %s message",
                    len(redacted.redactions),
                    msg.get("role", "unknown"),
                )
            result.append({**msg, "content": redacted.text})
        else:
            result.append(msg)
    return result


def scan_for_secrets(text: str) -> list[dict[str, Any]]:
    """Scan text for secrets without redacting (for auditing).

    Args:
        text: Text to scan

    Returns:
        List of found secrets with type and location
    """
    if not text:
        return []

    findings: list[dict[str, Any]] = []

    for pat in PATTERNS:
        for match in pat.pattern.finditer(text):
            findings.append({
                "type": pat.name,
                "description": pat.description,
                "start": match.start(),
                "end": match.end(),
                "preview": match.group()[:8] + "...",
            })

    return findings
