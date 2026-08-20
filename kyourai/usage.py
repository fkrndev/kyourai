"""Usage and pricing tracker — track token usage and estimate costs per session.

Tracks prompt_tokens, completion_tokens for each model call and estimates
cost based on current model pricing. Data is persisted in the session DB
and surfaced via insights and CLI.

Usage:
  from kyourai.usage import UsageTracker
  tracker = UsageTracker()

  # Record usage
  tracker.record(
      session_id="my-session",
      model="openai:gpt-4o",
      prompt_tokens=1500,
      completion_tokens=500,
  )

  # Get session total
  total = tracker.get_session_total("my-session")

  # Get all sessions
  totals = tracker.get_totals(days=30)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from kyourai.constants import get_kyourai_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pricing table (per 1M tokens, in USD)
# Updated 2025-01. Source: provider pricing pages.
# ---------------------------------------------------------------------------

PRICING: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-4": {"input": 30.00, "output": 60.00},
    "gpt-3.5-turbo": {"input": 0.50, "output": 1.50},
    "o1": {"input": 15.00, "output": 60.00},
    "o1-mini": {"input": 3.00, "output": 12.00},
    "o1-pro": {"input": 150.00, "output": 600.00},
    "o3": {"input": 10.00, "output": 40.00},
    "o3-mini": {"input": 1.10, "output": 4.40},
    "o4-mini": {"input": 1.10, "output": 4.40},

    # Anthropic
    "claude-3-5-sonnet-latest": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku-latest": {"input": 0.80, "output": 4.00},
    "claude-3-opus-latest": {"input": 15.00, "output": 75.00},
    "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},

    # Google
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini-2.0-flash-lite": {"input": 0.075, "output": 0.30},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-1.5-flash-8b": {"input": 0.0375, "output": 0.15},

    # Groq
    "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
    "llama-3.1-8b-instant": {"input": 0.05, "output": 0.08},
    "mixtral-8x7b-32768": {"input": 0.24, "output": 0.24},

    # Mistral
    "mistral-large-latest": {"input": 2.00, "output": 6.00},
    "mistral-small-latest": {"input": 0.20, "output": 0.60},
    "codestral-latest": {"input": 0.30, "output": 0.90},

    # Ollama (local — free)
    "llama3.2": {"input": 0.0, "output": 0.0},
    "llama3.1": {"input": 0.0, "output": 0.0},
    "qwen2.5": {"input": 0.0, "output": 0.0},
    "deepseek-r1": {"input": 0.0, "output": 0.0},

    # Bedrock (per 1K tokens — converted)
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {"input": 3.00, "output": 15.00},
    "anthropic.claude-3-haiku-20240307-v1:0": {"input": 0.25, "output": 1.25},

    # Test / unknown
    "test": {"input": 0.0, "output": 0.0},
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UsageEntry:
    """A single usage record."""
    session_id: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    timestamp: float
    cost_usd: float = 0.0


@dataclass(slots=True)
class UsageTotal:
    """Aggregated usage for a session or period."""
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    by_model: dict[str, dict[str, float]] = field(default_factory=dict)
    entry_count: int = 0


# ---------------------------------------------------------------------------
# Pricing lookup
# ---------------------------------------------------------------------------


def get_pricing(model: str) -> dict[str, float]:
    """Get pricing for a model.

    Args:
        model: Model name (with or without provider prefix)

    Returns:
        Dict with 'input' and 'output' prices per 1M tokens
    """
    # Strip provider prefix
    if ":" in model:
        _, model_name = model.split(":", 1)
    else:
        model_name = model

    # Direct lookup
    if model_name in PRICING:
        return PRICING[model_name]

    # Try case-insensitive
    for key, pricing in PRICING.items():
        if key.lower() == model_name.lower():
            return pricing

    # Try prefix matching (e.g. "gpt-4o-2024-08-06" → "gpt-4o")
    for key, pricing in PRICING.items():
        if model_name.startswith(key):
            return pricing

    # Unknown model — return zeros
    logger.debug("Unknown model for pricing: %s", model)
    return {"input": 0.0, "output": 0.0}


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Estimate cost in USD for a model call.

    Args:
        model: Model name
        prompt_tokens: Number of input tokens
        completion_tokens: Number of output tokens

    Returns:
        Estimated cost in USD
    """
    pricing = get_pricing(model)
    cost = (
        (prompt_tokens / 1_000_000) * pricing["input"]
        + (completion_tokens / 1_000_000) * pricing["output"]
    )
    return round(cost, 6)


# ---------------------------------------------------------------------------
# Usage tracker
# ---------------------------------------------------------------------------


class UsageTracker:
    """Track token usage and costs, persisted in session DB.

    Stores usage entries in a separate SQLite table for efficient querying.
    Falls back to in-memory tracking if DB is not available.
    """

    def __init__(self) -> None:
        self._entries: list[UsageEntry] = []
        self._db_available = self._check_db()

    def _check_db(self) -> bool:
        """Check if the usage table exists in session DB."""
        try:
            import sqlite3
            db_path = get_kyourai_home() / "sessions.db"
            if not db_path.exists():
                return False
            conn = sqlite3.connect(str(db_path))
            # Check if usage table exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='usage'"
            )
            exists = cursor.fetchone() is not None
            if not exists:
                # Create table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        model TEXT NOT NULL,
                        prompt_tokens INTEGER DEFAULT 0,
                        completion_tokens INTEGER DEFAULT 0,
                        cost_usd REAL DEFAULT 0.0,
                        timestamp REAL NOT NULL
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_usage_session ON usage(session_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage(timestamp)"
                )
                conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.warning("Usage DB not available: %s", e)
            return False

    def record(
        self,
        session_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        timestamp: float | None = None,
    ) -> UsageEntry:
        """Record a usage entry.

        Args:
            session_id: Session ID
            model: Model name
            prompt_tokens: Input tokens
            completion_tokens: Output tokens
            timestamp: Optional timestamp (default: now)

        Returns:
            The recorded UsageEntry
        """
        cost = estimate_cost(model, prompt_tokens, completion_tokens)
        entry = UsageEntry(
            session_id=session_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            timestamp=timestamp or time.time(),
            cost_usd=cost,
        )

        # Store in memory
        self._entries.append(entry)

        # Persist to DB if available
        if self._db_available:
            try:
                import sqlite3
                db_path = get_kyourai_home() / "sessions.db"
                conn = sqlite3.connect(str(db_path))
                conn.execute(
                    "INSERT INTO usage (session_id, model, prompt_tokens, completion_tokens, cost_usd, timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (entry.session_id, entry.model, entry.prompt_tokens,
                     entry.completion_tokens, entry.cost_usd, entry.timestamp),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                logger.warning("Failed to persist usage entry: %s", e)

        return entry

    def get_session_total(self, session_id: str) -> UsageTotal:
        """Get total usage for a specific session."""
        entries = self._query_entries(session_id=session_id)
        return self._aggregate(entries)

    def get_totals(self, days: int = 30) -> UsageTotal:
        """Get total usage across all sessions for the last N days."""
        cutoff = time.time() - (days * 86400)
        entries = self._query_entries(since=cutoff)
        return self._aggregate(entries)

    def get_by_model(self, days: int = 30) -> dict[str, UsageTotal]:
        """Get usage broken down by model."""
        cutoff = time.time() - (days * 86400)
        entries = self._query_entries(since=cutoff)

        by_model: dict[str, list[UsageEntry]] = {}
        for entry in entries:
            by_model.setdefault(entry.model, []).append(entry)

        return {
            model: self._aggregate(model_entries)
            for model, model_entries in by_model.items()
        }

    # -- Internal helpers ---------------------------------------------------

    def _query_entries(
        self,
        session_id: str | None = None,
        since: float | None = None,
    ) -> list[UsageEntry]:
        """Query usage entries from DB or memory."""
        if self._db_available:
            try:
                import sqlite3
                db_path = get_kyourai_home() / "sessions.db"
                conn = sqlite3.connect(str(db_path))

                query = "SELECT session_id, model, prompt_tokens, completion_tokens, cost_usd, timestamp FROM usage"
                conditions = []
                params: list[Any] = []

                if session_id:
                    conditions.append("session_id = ?")
                    params.append(session_id)
                if since:
                    conditions.append("timestamp >= ?")
                    params.append(since)

                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                query += " ORDER BY timestamp DESC"

                cursor = conn.execute(query, params)
                rows = cursor.fetchall()
                conn.close()

                return [
                    UsageEntry(
                        session_id=row[0],
                        model=row[1],
                        prompt_tokens=row[2],
                        completion_tokens=row[3],
                        cost_usd=row[4],
                        timestamp=row[5],
                    )
                    for row in rows
                ]
            except Exception as e:
                logger.warning("Failed to query usage from DB: %s", e)

        # Fall back to memory
        entries = self._entries
        if session_id:
            entries = [e for e in entries if e.session_id == session_id]
        if since:
            entries = [e for e in entries if e.timestamp >= since]
        return entries

    def _aggregate(self, entries: list[UsageEntry]) -> UsageTotal:
        """Aggregate usage entries into a total."""
        total = UsageTotal(entry_count=len(entries))

        for entry in entries:
            total.total_prompt_tokens += entry.prompt_tokens
            total.total_completion_tokens += entry.completion_tokens
            total.total_cost_usd += entry.cost_usd

            model_stats = total.by_model.setdefault(entry.model, {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.0,
                "calls": 0,
            })
            model_stats["prompt_tokens"] += entry.prompt_tokens
            model_stats["completion_tokens"] += entry.completion_tokens
            model_stats["cost_usd"] += entry.cost_usd
            model_stats["calls"] += 1

        total.total_tokens = total.total_prompt_tokens + total.total_completion_tokens
        total.total_cost_usd = round(total.total_cost_usd, 4)
        return total
