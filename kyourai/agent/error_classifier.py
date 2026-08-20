"""Error classifier — categorize API errors as retryable or fatal.

Inspired by Hermes' error_classifier.py (88k LOC) but radically simpler.
The classifier's job: when an API call fails, decide whether to retry
(with backoff) or give up immediately.

Error categories:
  - RETRYABLE: transient failures (timeout, 429, 500, 502, 503, 504)
  - FATAL: permanent failures (401, 403, 400, content policy)
  - UNKNOWN: unexpected errors (treat as retryable with caution)

The classifier never raises — it always returns a classification so the
caller can decide what to do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ErrorCategory(str, Enum):
    """Classification of an API error."""
    RETRYABLE = "retryable"       # transient — retry with backoff
    RATE_LIMITED = "rate_limited"  # 429 — retry with longer backoff
    FATAL = "fatal"               # permanent — don't retry
    UNKNOWN = "unknown"           # unexpected — retry cautiously


@dataclass(slots=True)
class ErrorClassification:
    """Result of classifying an error."""
    category: ErrorCategory
    error_class: str
    message: str
    retry_after: float | None = None  # seconds to wait before retry (if known)
    max_retries: int = 3


# HTTP status codes that are retryable
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_FATAL_STATUS = {400, 401, 403, 404, 405, 422}

# Error class patterns that indicate transient failures
_TRANSIENT_PATTERNS = (
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "connection aborted",
    "temporarily unavailable",
    "service unavailable",
    "overloaded",
    "capacity",
    " EOF ",
    "remote end closed",
    "chunked encoding",
)

# Error class patterns that indicate permanent failures
_FATAL_PATTERNS = (
    "authentication",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "invalid_api_key",
    "permission denied",
    "content policy",
    "content_filter",
    "safety",
    "billing",
    "quota exceeded",
    "credit",
    "payment",
)


def classify_error(
    error: Exception,
    *,
    status_code: int | None = None,
    response_body: str | None = None,
) -> ErrorClassification:
    """Classify an error as retryable, rate-limited, or fatal.

    Args:
        error: The exception that was raised
        status_code: HTTP status code if available
        response_body: Response body if available (for parsing retry-after)

    Returns:
        ErrorClassification with category and retry guidance
    """
    error_class = type(error).__name__
    error_msg = str(error).lower()

    # Check HTTP status code first (most reliable)
    if status_code is not None:
        if status_code == 429:
            retry_after = _parse_retry_after(response_body)
            return ErrorClassification(
                category=ErrorCategory.RATE_LIMITED,
                error_class=error_class,
                message=f"Rate limited (429)",
                retry_after=retry_after,
                max_retries=5,
            )
        if status_code in _RETRYABLE_STATUS:
            return ErrorClassification(
                category=ErrorCategory.RETRYABLE,
                error_class=error_class,
                message=f"Server error ({status_code})",
                max_retries=3,
            )
        if status_code in _FATAL_STATUS:
            return ErrorClassification(
                category=ErrorCategory.FATAL,
                error_class=error_class,
                message=f"Client error ({status_code})",
                max_retries=0,
            )

    # Check error message patterns
    for pattern in _TRANSIENT_PATTERNS:
        if pattern in error_msg:
            return ErrorClassification(
                category=ErrorCategory.RETRYABLE,
                error_class=error_class,
                message=f"Transient error: {pattern}",
                max_retries=3,
            )

    for pattern in _FATAL_PATTERNS:
        if pattern in error_msg:
            return ErrorClassification(
                category=ErrorCategory.FATAL,
                error_class=error_class,
                message=f"Fatal error: {pattern}",
                max_retries=0,
            )

    # Unknown — retry cautiously
    return ErrorClassification(
        category=ErrorCategory.UNKNOWN,
        error_class=error_class,
        message=str(error)[:200],
        max_retries=1,
    )


def _parse_retry_after(body: str | None) -> float | None:
    """Try to extract retry-after value from response body."""
    if not body:
        return None
    import re

    # Look for "retry-after" or "retry_after" header value
    match = re.search(r"retry[-_]?after[:\s]+(\d+)", body, re.IGNORECASE)
    if match:
        return float(match.group(1))

    # Look for "try again in X seconds"
    match = re.search(r"try again in (\d+) second", body, re.IGNORECASE)
    if match:
        return float(match.group(1))

    return None
