"""Security module — credential redaction, content security, PII detection."""

from kyourai.security.redaction import (
    redact_text,
    redact_dict,
    redact_messages,
    scan_for_secrets,
    RedactionResult,
    RedactionPattern,
    PATTERNS,
)
from kyourai.security.content import (
    wrap_external_content,
    detect_injection,
    sanitize_special_tokens,
    detect_homoglyphs,
    normalize_homoglyphs,
    analyze_content,
    sanitize_external_content,
    InjectionFinding,
    ContentSecurityReport,
)

__all__ = [
    # Redaction
    "redact_text",
    "redact_dict",
    "redact_messages",
    "scan_for_secrets",
    "RedactionResult",
    "RedactionPattern",
    "PATTERNS",
    # Content security
    "wrap_external_content",
    "detect_injection",
    "sanitize_special_tokens",
    "detect_homoglyphs",
    "normalize_homoglyphs",
    "analyze_content",
    "sanitize_external_content",
    "InjectionFinding",
    "ContentSecurityReport",
]
