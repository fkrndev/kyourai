"""Rate limit tracker — track and throttle API requests per provider.

Prevents hitting rate limits by tracking request timestamps and
enforcing a minimum interval between calls. Simpler than Hermes'
nous_rate_guard.py (11k LOC) — just a sliding window counter.

Usage:
    tracker = RateLimitTracker()
    tracker.wait_if_needed("openai")  # blocks if too many recent requests
    tracker.record("openai")          # record a successful request
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from threading import Lock

logger = logging.getLogger(__name__)

# Default limits per provider (requests per minute)
_DEFAULT_LIMITS = {
    "openai": 60,       # 60 rpm (tier 1)
    "anthropic": 50,    # 50 rpm
    "gemini": 60,       # 60 rpm
    "default": 30,      # conservative default
}

_WINDOW_SECONDS = 60  # sliding window: 1 minute
_SAFETY_MARGIN = 0.8  # only use 80% of limit to avoid edge cases


class RateLimitTracker:
    """Sliding window rate limiter per provider.

    Thread-safe. Tracks request timestamps in a deque and blocks
    when the window is full.
    """

    def __init__(self, limits: dict[str, int] | None = None) -> None:
        self._limits = limits or _DEFAULT_LIMITS
        self._windows: dict[str, deque[float]] = {}
        self._lock = Lock()

    def _get_limit(self, provider: str) -> int:
        """Get RPM limit for a provider."""
        return int(self._limits.get(provider, self._limits["default"]) * _SAFETY_MARGIN)

    def _clean_window(self, provider: str, now: float) -> None:
        """Remove timestamps older than the window."""
        window = self._windows.get(provider)
        if window is None:
            return
        cutoff = now - _WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()

    def _count_recent(self, provider: str) -> int:
        """Count requests in the current window."""
        now = time.time()
        self._clean_window(provider, now)
        window = self._windows.get(provider)
        return len(window) if window else 0

    def can_request(self, provider: str = "default") -> bool:
        """Check if a request can be made without exceeding the limit."""
        with self._lock:
            count = self._count_recent(provider)
            limit = self._get_limit(provider)
            return count < limit

    def wait_if_needed(self, provider: str = "default") -> float:
        """Block until a request can be made. Returns wait time in seconds.

        Synchronous — use wait_if_needed_async for async contexts.
        """
        with self._lock:
            count = self._count_recent(provider)
            limit = self._get_limit(provider)

            if count < limit:
                return 0.0

            # Calculate how long to wait until the oldest request expires
            window = self._windows.get(provider, deque())
            if window:
                oldest = window[0]
                wait = max(0.1, oldest + _WINDOW_SECONDS - time.time())
            else:
                wait = 0.1

        logger.debug(
            "Rate limit for %s: %d/%d requests, waiting %.1fs",
            provider, count, limit, wait,
        )
        time.sleep(wait)
        return wait

    async def wait_if_needed_async(self, provider: str = "default") -> float:
        """Async version of wait_if_needed."""
        with self._lock:
            count = self._count_recent(provider)
            limit = self._get_limit(provider)

            if count < limit:
                return 0.0

            window = self._windows.get(provider, deque())
            if window:
                oldest = window[0]
                wait = max(0.1, oldest + _WINDOW_SECONDS - time.time())
            else:
                wait = 0.1

        logger.debug(
            "Rate limit for %s: %d/%d requests, waiting %.1fs",
            provider, count, limit, wait,
        )
        await asyncio.sleep(wait)
        return wait

    def record(self, provider: str = "default") -> None:
        """Record a successful request."""
        with self._lock:
            now = time.time()
            if provider not in self._windows:
                self._windows[provider] = deque()
            self._windows[provider].append(now)

    def get_status(self, provider: str = "default") -> dict[str, int]:
        """Get current rate limit status for a provider."""
        with self._lock:
            count = self._count_recent(provider)
            limit = self._get_limit(provider)
            return {
                "provider": provider,
                "requests_in_window": count,
                "limit": limit,
                "remaining": max(0, limit - count),
            }

    def reset(self, provider: str | None = None) -> None:
        """Reset tracking for a provider (or all if None)."""
        with self._lock:
            if provider:
                self._windows.pop(provider, None)
            else:
                self._windows.clear()
