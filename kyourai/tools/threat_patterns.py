"""Threat pattern scanning for memory content.

Ported from Hermes' tools/threat_patterns.py — heavily trimmed to the
essential injection/exfiltration patterns. This is a defensive layer:
memory entries enter the system prompt as a frozen snapshot, so a poisoned
entry persists for the entire session.

Kyourai's version starts with a minimal pattern set. Extend as needed.
"""

from __future__ import annotations

import re

# Patterns that indicate prompt injection or credential exfiltration attempts
# in memory content. Each pattern: (id, regex, description).
_THREAT_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "system_prompt_override",
        re.compile(r"ignore\s+(all\s+)?(previous|above|prior)\s+instructions", re.IGNORECASE),
        "Attempts to override system prompt instructions",
    ),
    (
        "role_injection",
        re.compile(r"^\s*(system|assistant)\s*:", re.IGNORECASE | re.MULTILINE),
        "Attempts to inject role-prefixed messages",
    ),
    (
        "credential_exfil_url",
        re.compile(
            r"https?://[^\s]+\.(?:ngrok|loophole|serveo|localtunnel|trycloudflare)\.[a-z]+",
            re.IGNORECASE,
        ),
        "Tunnel URLs that could exfiltrate credentials",
    ),
    (
        "api_key_pattern",
        re.compile(r"(?:sk-|pk-|rk-)[a-zA-Z0-9]{20,}"),
        "Hardcoded API key patterns",
    ),
    (
        "env_var_exfil",
        re.compile(r"\$\{?\s*(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|AWS_SECRET|GITHUB_TOKEN)", re.IGNORECASE),
        "Environment variable references that could exfiltrate secrets",
    ),
]


def first_threat_message(content: str, *, scope: str = "strict") -> str | None:
    """Return a human-readable error string if *content* matches a threat pattern.

    Returns None if the content is clean. The *scope* parameter is accepted
    for compatibility but currently all patterns apply at every scope.
    """
    for tid, pattern, desc in _THREAT_PATTERNS:
        if pattern.search(content):
            return f"Blocked: {desc} (pattern: {tid}). Remove this content and retry."
    return None


def scan_for_threats(content: str, *, scope: str = "strict") -> list[str]:
    """Return a list of threat IDs found in *content*. Empty list if clean."""
    findings: list[str] = []
    for tid, pattern, _desc in _THREAT_PATTERNS:
        if pattern.search(content):
            findings.append(tid)
    return findings
