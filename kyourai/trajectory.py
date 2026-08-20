"""Trajectory recording — bounded session event recording for debugging.

Inspired by OpenClaw's trajectory system. Records session events to SQLite
with size limits, payload sanitization, and secret redaction. Useful for:
  - Debugging agent behavior
  - Post-mortem analysis of failed runs
  - Performance profiling
  - Audit trail of tool calls

Usage:
    from kyourai.trajectory import TrajectoryRecorder

    recorder = TrajectoryRecorder(session_id="my-session")
    recorder.record_event("turn_start", {"prompt": "hello"})
    recorder.record_event("tool_call", {"tool": "terminal", "args": {...}})
    recorder.record_event("turn_end", {"output": "..."})
    # Export for debugging:
    recorder.export("trajectory.json")
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from kyourai.constants import get_kyourai_home
from kyourai.security.redaction import redact_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MAX_EVENT_SIZE = 65_536  # 64KB per event
MAX_STRING_LENGTH = 32_000  # Truncate strings in payloads
MAX_ARRAY_ITEMS = 64
MAX_OBJECT_KEYS = 64
MAX_DEPTH = 6
MAX_EVENTS = 200_000  # Max events per session
MAX_DB_SIZE = 100 * 1024 * 1024  # 100MB


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TrajectoryEvent:
    """A single trajectory event."""
    event_id: str
    session_id: str
    trace_id: str
    source: str  # "runtime", "transcript", "export"
    event_type: str
    timestamp: float
    sequence: int
    run_id: str = ""
    entry_id: str = ""
    parent_entry_id: str = ""
    workspace_dir: str = ""
    provider: str = ""
    model_id: str = ""
    model_api: str = ""
    payload_json: str = "{}"
    tool_definitions_json: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["payload"] = json.loads(self.payload_json)
        if self.tool_definitions_json:
            d["tool_definitions"] = json.loads(self.tool_definitions_json)
        else:
            d["tool_definitions"] = []
        return d


# ---------------------------------------------------------------------------
# Payload sanitization
# ---------------------------------------------------------------------------


def sanitize_payload(obj: Any, depth: int = 0) -> Any:
    """Sanitize a payload for safe storage.

    - Truncates long strings
    - Limits array/object sizes
    - Redacts secrets
    - Detects circular references
    """
    if depth > MAX_DEPTH:
        return "[max depth reached]"

    if obj is None:
        return None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return obj
    if isinstance(obj, str):
        # Redact secrets
        redacted = redact_text(obj).text
        if len(redacted) > MAX_STRING_LENGTH:
            return redacted[:MAX_STRING_LENGTH] + f"...[truncated {len(redacted) - MAX_STRING_LENGTH} chars]"
        return redacted
    if isinstance(obj, (list, tuple)):
        if len(obj) > MAX_ARRAY_ITEMS:
            return [sanitize_payload(item, depth + 1) for item in obj[:MAX_ARRAY_ITEMS]] + [f"...[{len(obj) - MAX_ARRAY_ITEMS} more items]"]
        return [sanitize_payload(item, depth + 1) for item in obj]
    if isinstance(obj, dict):
        if len(obj) > MAX_OBJECT_KEYS:
            truncated = dict(list(obj.items())[:MAX_OBJECT_KEYS])
            truncated["..."] = f"[{len(obj) - MAX_OBJECT_KEYS} more keys]"
            obj = truncated
        return {
            str(k): sanitize_payload(v, depth + 1)
            for k, v in obj.items()
        }
    # Unknown type — stringify
    try:
        return str(obj)[:MAX_STRING_LENGTH]
    except Exception:
        return "[unserializable]"


# ---------------------------------------------------------------------------
# Trajectory recorder
# ---------------------------------------------------------------------------


class TrajectoryRecorder:
    """Records session events to SQLite with bounded size and sanitization.

    Each session gets its own trajectory database for isolation.
    Events are sanitized (secrets redacted, sizes truncated) before storage.
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        session_id: str,
        workspace_dir: str = "",
        provider: str = "",
        model_id: str = "",
    ) -> None:
        self.session_id = session_id
        self.trace_id = f"trace-{uuid.uuid4().hex[:12]}"
        self.workspace_dir = workspace_dir
        self.provider = provider
        self.model_id = model_id
        self._sequence = 0
        self._event_count = 0
        self._db_path = get_kyourai_home() / "trajectory" / f"{session_id}.db"
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the trajectory database."""
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path))
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS trajectory_events (
                    event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    sequence INTEGER NOT NULL,
                    run_id TEXT DEFAULT '',
                    entry_id TEXT DEFAULT '',
                    parent_entry_id TEXT DEFAULT '',
                    workspace_dir TEXT DEFAULT '',
                    provider TEXT DEFAULT '',
                    model_id TEXT DEFAULT '',
                    model_api TEXT DEFAULT '',
                    payload_json TEXT DEFAULT '{}',
                    tool_definitions_json TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_traj_session ON trajectory_events(session_id);
                CREATE INDEX IF NOT EXISTS idx_traj_sequence ON trajectory_events(sequence);
                CREATE INDEX IF NOT EXISTS idx_traj_type ON trajectory_events(event_type);
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("Trajectory DB init failed: %s", e)

    def record_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        source: str = "runtime",
        run_id: str = "",
        entry_id: str = "",
        parent_entry_id: str = "",
        model_api: str = "",
        tool_definitions: list[dict] | None = None,
    ) -> TrajectoryEvent | None:
        """Record a trajectory event.

        Args:
            event_type: Type of event (turn_start, tool_call, turn_end, etc.)
            payload: Event payload (will be sanitized)
            source: Event source (runtime, transcript, export)
            run_id: Run ID for correlation
            entry_id: Entry ID for transcript correlation
            parent_entry_id: Parent entry for nested events
            model_api: API version
            tool_definitions: Tool definitions snapshot

        Returns:
            TrajectoryEvent if recorded, None if limit reached
        """
        if self._event_count >= MAX_EVENTS:
            logger.warning("Trajectory event limit reached for session %s", self.session_id)
            return None

        # Check DB size
        try:
            if self._db_path.exists() and self._db_path.stat().st_size > MAX_DB_SIZE:
                logger.warning("Trajectory DB size limit reached for session %s", self.session_id)
                return None
        except OSError:
            pass

        # Sanitize payload
        sanitized = sanitize_payload(payload or {})
        payload_json = json.dumps(sanitized, ensure_ascii=False, default=str)

        # Truncate if still too large
        if len(payload_json) > MAX_EVENT_SIZE:
            payload_json = payload_json[:MAX_EVENT_SIZE] + "...[truncated]"

        tool_defs_json = json.dumps(tool_definitions or [], ensure_ascii=False, default=str)
        if len(tool_defs_json) > MAX_EVENT_SIZE:
            tool_defs_json = tool_defs_json[:MAX_EVENT_SIZE] + "...[truncated]"

        self._sequence += 1
        self._event_count += 1

        event = TrajectoryEvent(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            session_id=self.session_id,
            trace_id=self.trace_id,
            source=source,
            event_type=event_type,
            timestamp=time.time(),
            sequence=self._sequence,
            run_id=run_id,
            entry_id=entry_id,
            parent_entry_id=parent_entry_id,
            workspace_dir=self.workspace_dir,
            provider=self.provider,
            model_id=self.model_id,
            model_api=model_api,
            payload_json=payload_json,
            tool_definitions_json=tool_defs_json,
        )

        self._persist(event)
        return event

    def get_events(
        self,
        event_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TrajectoryEvent]:
        """Query events from this session's trajectory."""
        try:
            conn = sqlite3.connect(str(self._db_path))
            if event_type:
                cursor = conn.execute(
                    "SELECT * FROM trajectory_events WHERE event_type = ? "
                    "ORDER BY sequence DESC LIMIT ? OFFSET ?",
                    (event_type, limit, offset),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM trajectory_events "
                    "ORDER BY sequence DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            rows = cursor.fetchall()
            conn.close()

            return [
                TrajectoryEvent(
                    event_id=row[0], session_id=row[1], trace_id=row[2],
                    source=row[3], event_type=row[4], timestamp=row[5],
                    sequence=row[6], run_id=row[7], entry_id=row[8],
                    parent_entry_id=row[9], workspace_dir=row[10],
                    provider=row[11], model_id=row[12], model_api=row[13],
                    payload_json=row[14], tool_definitions_json=row[15],
                )
                for row in rows
            ]
        except Exception as e:
            logger.warning("Failed to query trajectory: %s", e)
            return []

    def export(self, output_path: str | Path) -> bool:
        """Export trajectory to a JSON file for debugging."""
        try:
            events = self.get_events(limit=MAX_EVENTS)
            bundle = {
                "schema": "kyourai-trajectory",
                "schemaVersion": self.SCHEMA_VERSION,
                "generatedAt": time.time(),
                "traceId": self.trace_id,
                "sessionId": self.session_id,
                "workspaceDir": self.workspace_dir,
                "provider": self.provider,
                "modelId": self.model_id,
                "eventCount": len(events),
                "events": [e.to_dict() for e in events],
            }

            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(bundle, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            logger.info("Exported %d events to %s", len(events), output)
            return True
        except Exception as e:
            logger.warning("Trajectory export failed: %s", e)
            return False

    def cleanup(self) -> None:
        """Clean up the trajectory database."""
        try:
            if self._db_path.exists():
                self._db_path.unlink()
        except Exception:
            pass

    @property
    def event_count(self) -> int:
        return self._event_count

    def _persist(self, event: TrajectoryEvent) -> None:
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute("""
                INSERT INTO trajectory_events
                (event_id, session_id, trace_id, source, event_type, timestamp,
                 sequence, run_id, entry_id, parent_entry_id, workspace_dir,
                 provider, model_id, model_api, payload_json, tool_definitions_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event.event_id, event.session_id, event.trace_id,
                event.source, event.event_type, event.timestamp,
                event.sequence, event.run_id, event.entry_id,
                event.parent_entry_id, event.workspace_dir,
                event.provider, event.model_id, event.model_api,
                event.payload_json, event.tool_definitions_json,
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug("Trajectory persist failed: %s", e)


# ---------------------------------------------------------------------------
# Trajectory cleanup
# ---------------------------------------------------------------------------


def cleanup_old_trajectories(max_age_days: int = 7) -> int:
    """Remove trajectory databases older than max_age_days.

    Returns count of files removed.
    """
    traj_dir = get_kyourai_home() / "trajectory"
    if not traj_dir.exists():
        return 0

    cutoff = time.time() - (max_age_days * 86400)
    removed = 0

    for db_file in traj_dir.glob("*.db"):
        try:
            if db_file.stat().st_mtime < cutoff:
                db_file.unlink()
                removed += 1
        except OSError:
            pass

    return removed
