"""Retry utilities — exponential backoff with jitter for retryable errors.

Works with the error classifier to retry transient failures gracefully.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, Callable, TypeVar

from kyourai.agent.error_classifier import (
    ErrorCategory,
    ErrorClassification,
    classify_error,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Default backoff parameters
_BASE_DELAY = 1.0  # seconds
_MAX_DELAY = 60.0  # seconds
_JITTER = 0.1  # 10% jitter


async def retry_with_backoff(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = _BASE_DELAY,
    max_delay: float = _MAX_DELAY,
    **kwargs: Any,
) -> T:
    """Call an async function with retry and exponential backoff.

    Args:
        fn: Async function to call
        *args: Positional arguments for fn
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay between retries (seconds)
        max_delay: Maximum delay between retries (seconds)
        **kwargs: Keyword arguments for fn

    Returns:
        Result of fn(*args, **kwargs)

    Raises:
        The last exception if all retries are exhausted or error is fatal
    """
    last_error: Exception | None = None
    last_classification: ErrorClassification | None = None

    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            last_error = e
            classification = classify_error(e)

            # Don't retry fatal errors
            if classification.category == ErrorCategory.FATAL:
                logger.warning(
                    "Fatal error (no retry): %s — %s",
                    classification.error_class,
                    classification.message,
                )
                raise

            # Don't retry on last attempt
            if attempt >= max_retries:
                logger.warning(
                    "Retries exhausted after %d attempts: %s",
                    attempt + 1,
                    classification.message,
                )
                raise

            # Use classification's max_retries if it's lower than ours
            effective_max = min(classification.max_retries, max_retries)
            if attempt >= effective_max:
                raise

            last_classification = classification

            # Calculate delay
            if classification.retry_after:
                delay = classification.retry_after
            else:
                delay = min(base_delay * (2 ** attempt), max_delay)
                # Add jitter
                delay += random.uniform(0, delay * _JITTER)

            logger.info(
                "Retry %d/%d after %.1fs: %s",
                attempt + 1,
                effective_max,
                delay,
                classification.message,
            )

            await asyncio.sleep(delay)

    # Should never reach here, but just in case
    if last_error:
        raise last_error
    raise RuntimeError("retry_with_backoff: unreachable state")


def retry_sync(
    fn: Callable[..., T],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = _BASE_DELAY,
    max_delay: float = _MAX_DELAY,
    **kwargs: Any,
) -> T:
    """Synchronous version of retry_with_backoff.

    Uses time.sleep instead of asyncio.sleep.
    """
    import time

    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_error = e
            classification = classify_error(e)

            if classification.category == ErrorCategory.FATAL:
                raise

            if attempt >= max_retries or attempt >= classification.max_retries:
                raise

            if classification.retry_after:
                delay = classification.retry_after
            else:
                delay = min(base_delay * (2 ** attempt), max_delay)
                delay += random.uniform(0, delay * _JITTER)

            logger.info(
                "Retry %d/%d after %.1fs: %s",
                attempt + 1,
                max_retries,
                delay,
                classification.message,
            )

            time.sleep(delay)

    if last_error:
        raise last_error
    raise RuntimeError("retry_sync: unreachable state")
