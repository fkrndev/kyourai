"""Empty response guard — handle LLM responses that are empty or whitespace-only.

LLMs occasionally return empty outputs (especially with certain tool call
patterns or content filters). This guard detects empty responses and
either retries or returns a helpful fallback message.

Inspired by Hermes' empty_response_guard.py (10k LOC) but minimal.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_MAX_EMPTY_RETRIES = 2
_FALLBACK_MESSAGE = (
    "I apologize, but I wasn't able to generate a response. "
    "Could you rephrase your question or try again?"
)

# Patterns that indicate a "technically non-empty but useless" response
_USELESS_PATTERNS = (
    " ",
    "\n",
    "\t",
    "null",
    "none",
    "nil",
)


def is_empty_response(output: str | None) -> bool:
    """Check if an LLM response is empty or effectively empty.

    Args:
        output: The LLM response string

    Returns:
        True if the response is empty, whitespace-only, or a useless pattern
    """
    if output is None:
        return True

    stripped = output.strip()
    if not stripped:
        return True

    # Check for useless patterns
    lower = stripped.lower()
    if lower in _USELESS_PATTERNS:
        return True

    # Check if it's just punctuation
    if all(c in ".,;:!?-— \n\t" for c in stripped):
        return True

    return False


def guard_response(
    output: str | None,
    retry_count: int = 0,
    max_retries: int = _MAX_EMPTY_RETRIES,
) -> tuple[str, bool]:
    """Guard against empty responses.

    Args:
        output: The LLM response string
        retry_count: How many times we've retried
        max_retries: Max retries before giving up

    Returns:
        Tuple of (response_string, should_retry)
    """
    if not is_empty_response(output):
        return output, False  # type: ignore[return-value]

    if retry_count < max_retries:
        logger.warning(
            "Empty response detected (attempt %d/%d) — will retry",
            retry_count + 1,
            max_retries,
        )
        return "", True  # signal retry

    logger.warning(
        "Empty response after %d retries — returning fallback", max_retries
    )
    return _FALLBACK_MESSAGE, False
