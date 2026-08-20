"""InsightsEngine — usage analytics over session history.

Computes analytics from SessionDB data:
  - Overview: total sessions, messages, tool calls, active sessions
  - Model breakdown: which models are used, how often
  - Source breakdown: CLI vs API vs MCP vs team
  - Tool usage: which tools are called most
  - Activity patterns: messages per day/week
  - Top sessions: most active sessions by message count

Inspired by Hermes' InsightsEngine (agent/insights.py) but trimmed to
kyourai's simpler schema — no billing, no platform gateway, no skill tracking.
"""

from __future__ import annotations

import time
from typing import Any

from kyourai.state.db import SessionDB


class InsightsEngine:
    """Analyze session history and produce usage insights.

    Usage:
        db = SessionDB()
        engine = InsightsEngine(db)
        report = engine.generate(days=30)
        print(report["overview"])
    """

    def __init__(self, db: SessionDB) -> None:
        self.db = db
        self._conn = db._conn

    def generate(self, days: int = 30) -> dict[str, Any]:
        """Generate a complete insights report for the last N days.

        Args:
            days: Number of days to look back (default: 30)

        Returns:
            Dict with overview, models, sources, tools, activity, top_sessions.
        """
        cutoff = time.time() - (days * 86400)

        sessions = self._get_sessions(cutoff)
        if not sessions:
            return {
                "days": days,
                "empty": True,
                "overview": {},
                "models": [],
                "sources": [],
                "tools": [],
                "activity": {},
                "top_sessions": [],
            }

        overview = self._compute_overview(sessions, cutoff)
        models = self._compute_model_breakdown(sessions)
        sources = self._compute_source_breakdown(sessions)
        tools = self._compute_tool_breakdown(cutoff)
        activity = self._compute_activity_patterns(sessions)
        top_sessions = self._compute_top_sessions(sessions)

        return {
            "days": days,
            "empty": False,
            "generated_at": time.time(),
            "overview": overview,
            "models": models,
            "sources": sources,
            "tools": tools,
            "activity": activity,
            "top_sessions": top_sessions,
        }

    # -- Data gathering ------------------------------------------------------

    def _get_sessions(self, cutoff: float) -> list[dict[str, Any]]:
        """Get sessions started after cutoff."""
        rows = self._conn.execute(
            """SELECT id, source, model, team_id, user_id,
                      started_at, ended_at, message_count, tool_call_count,
                      input_tokens, output_tokens, title, archived
               FROM sessions
               WHERE started_at >= ? AND archived = 0
               ORDER BY started_at DESC""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- Computations --------------------------------------------------------

    def _compute_overview(
        self, sessions: list[dict[str, Any]], cutoff: float
    ) -> dict[str, Any]:
        total_messages = sum(s.get("message_count", 0) for s in sessions)
        total_tool_calls = sum(s.get("tool_call_count", 0) for s in sessions)
        total_input = sum(s.get("input_tokens", 0) for s in sessions)
        total_output = sum(s.get("output_tokens", 0) for s in sessions)
        active = sum(1 for s in sessions if s.get("ended_at") is None)

        # Count facts from holographic store (best-effort — may not be available)
        fact_count = self._count_facts()

        return {
            "total_sessions": len(sessions),
            "active_sessions": active,
            "total_messages": total_messages,
            "total_tool_calls": total_tool_calls,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_facts": fact_count,
            "avg_messages_per_session": (
                round(total_messages / len(sessions), 1) if sessions else 0
            ),
        }

    def _compute_model_breakdown(
        self, sessions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Breakdown by model used."""
        counts: dict[str, dict[str, Any]] = {}
        for s in sessions:
            model = s.get("model") or "unknown"
            if model not in counts:
                counts[model] = {
                    "model": model,
                    "sessions": 0,
                    "messages": 0,
                    "tool_calls": 0,
                }
            counts[model]["sessions"] += 1
            counts[model]["messages"] += s.get("message_count", 0)
            counts[model]["tool_calls"] += s.get("tool_call_count", 0)

        result = sorted(counts.values(), key=lambda x: x["sessions"], reverse=True)
        return result

    def _compute_source_breakdown(
        self, sessions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Breakdown by source (cli, api, mcp, team)."""
        counts: dict[str, dict[str, Any]] = {}
        for s in sessions:
            source = s.get("source") or "unknown"
            if source not in counts:
                counts[source] = {
                    "source": source,
                    "sessions": 0,
                    "messages": 0,
                }
            counts[source]["sessions"] += 1
            counts[source]["messages"] += s.get("message_count", 0)

        result = sorted(counts.values(), key=lambda x: x["sessions"], reverse=True)
        return result

    def _compute_tool_breakdown(self, cutoff: float) -> list[dict[str, Any]]:
        """Breakdown of tool usage by tool_name from messages."""
        rows = self._conn.execute(
            """SELECT tool_name, COUNT(*) as call_count
               FROM messages
               WHERE tool_name IS NOT NULL AND tool_name != ''
                 AND timestamp >= ?
               GROUP BY tool_name
               ORDER BY call_count DESC""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _compute_activity_patterns(
        self, sessions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Messages per day for the last N days."""
        from datetime import datetime, timedelta, timezone

        # Group sessions by day (UTC)
        day_counts: dict[str, int] = {}
        for s in sessions:
            ts = s.get("started_at", 0)
            if ts:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                day = dt.strftime("%Y-%m-%d")
                day_counts[day] = day_counts.get(day, 0) + s.get("message_count", 0)

        # Fill in missing days with 0
        if day_counts:
            min_day = min(day_counts.keys())
            max_day = max(day_counts.keys())
            current = datetime.strptime(min_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end = datetime.strptime(max_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            while current <= end:
                key = current.strftime("%Y-%m-%d")
                if key not in day_counts:
                    day_counts[key] = 0
                current += timedelta(days=1)

        sorted_days = sorted(day_counts.items())
        return {
            "by_day": sorted_days,
            "peak_day": max(sorted_days, key=lambda x: x[1]) if sorted_days else None,
            "avg_per_day": (
                round(sum(v for _, v in sorted_days) / len(sorted_days), 1)
                if sorted_days
                else 0
            ),
        }

    def _compute_top_sessions(
        self, sessions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Top 10 sessions by message count."""
        sorted_sessions = sorted(
            sessions, key=lambda x: x.get("message_count", 0), reverse=True
        )
        top = sorted_sessions[:10]
        return [
            {
                "id": s["id"],
                "title": s.get("title") or s["id"],
                "source": s.get("source", ""),
                "model": s.get("model", ""),
                "messages": s.get("message_count", 0),
                "tool_calls": s.get("tool_call_count", 0),
                "started_at": s.get("started_at"),
            }
            for s in top
        ]

    def _count_facts(self) -> int:
        """Best-effort count of holographic facts. Returns 0 if unavailable."""
        try:
            from kyourai.constants import get_holographic_db_path

            db_path = str(get_holographic_db_path())
            import sqlite3

            conn = sqlite3.connect(db_path, check_same_thread=False)
            row = conn.execute("SELECT COUNT(*) FROM facts").fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception:
            return 0
