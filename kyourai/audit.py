"""Audit event system — non-blocking audit trail with execution identity.

Inspired by OpenClaw's audit module. Provides:
  - AuditEvent: structured audit event with execution identity
  - AuditWriter: non-blocking queue for audit persistence
  - SQLite-backed storage with automatic pruning
  - Execution identity context for run attribution
  - Decision fact recording

Usage:
    from kyourai.audit import AuditWriter, AuditEvent, set_execution_context

    writer = AuditWriter()
    writer.start()

    # Set execution context for attribution
    set_execution_context("session-123", "run-456", "user-789")

    # Record events
    writer.record(AuditEvent(
        event_type="tool_call",
        action="terminal.execute",
        detail="ls -la",
    ))

    writer.stop()
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

from kyourai.constants import get_kyourai_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AuditEvent:
    """A single audit event."""
    event_id: str = ""
    event_type: str = ""  # "tool_call", "agent_run", "config_change", etc.
    action: str = ""  # Specific action (e.g. "terminal.execute", "file.write")
    detail: str = ""  # Human-readable detail
    timestamp: float = 0.0
    session_id: str = ""
    run_id: str = ""
    user_id: str = ""
    agent_id: str = ""
    workspace_dir: str = ""
    provider: str = ""
    model_id: str = ""
    severity: str = "info"  # "info", "warning", "error", "critical"
    metadata_json: str = "{}"
    outcome: str = ""  # "success", "failure", "blocked"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["metadata"] = json.loads(self.metadata_json)
        return d


@dataclass(slots=True)
class ExecutionIdentity:
    """Execution identity context for run attribution."""
    session_id: str = ""
    run_id: str = ""
    user_id: str = ""
    agent_id: str = ""
    workspace_dir: str = ""
    provider: str = ""
    model_id: str = ""


# ---------------------------------------------------------------------------
# Execution context (thread-local)
# ---------------------------------------------------------------------------


_context = threading.local()


def set_execution_context(
    session_id: str = "",
    run_id: str = "",
    user_id: str = "",
    agent_id: str = "",
    workspace_dir: str = "",
    provider: str = "",
    model_id: str = "",
) -> None:
    """Set the execution context for the current thread.

    All subsequent audit events will be attributed to this context.
    """
    _context.identity = ExecutionIdentity(
        session_id=session_id,
        run_id=run_id,
        user_id=user_id,
        agent_id=agent_id,
        workspace_dir=workspace_dir,
        provider=provider,
        model_id=model_id,
    )


def get_execution_context() -> ExecutionIdentity:
    """Get the current execution context."""
    return getattr(_context, "identity", ExecutionIdentity())


def clear_execution_context() -> None:
    """Clear the execution context."""
    if hasattr(_context, "identity"):
        del _context.identity


# ---------------------------------------------------------------------------
# Audit writer
# ---------------------------------------------------------------------------


class AuditWriter:
    """Non-blocking audit event writer with SQLite persistence.

    Events are queued and written by a background thread to avoid
    blocking the main execution. Uses a bounded queue with backpressure.
    """

    MAX_QUEUE_SIZE = 10_000
    PRUNE_AFTER_DAYS = 30
    PRUNE_MAX_ROWS = 100_000
    BUSY_TIMEOUT_MS = 5000

    def __init__(self) -> None:
        self._queue: queue.Queue[AuditEvent | None] = queue.Queue(maxsize=self.MAX_QUEUE_SIZE)
        self._thread: threading.Thread | None = None
        self._running = False
        self._db_path = get_kyourai_home() / "audit.db"
        self._init_db()
        self._contention_count = 0

    def _init_db(self) -> None:
        """Initialize audit database."""
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=self.BUSY_TIMEOUT_MS / 1000)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    action TEXT DEFAULT '',
                    detail TEXT DEFAULT '',
                    timestamp REAL NOT NULL,
                    session_id TEXT DEFAULT '',
                    run_id TEXT DEFAULT '',
                    user_id TEXT DEFAULT '',
                    agent_id TEXT DEFAULT '',
                    workspace_dir TEXT DEFAULT '',
                    provider TEXT DEFAULT '',
                    model_id TEXT DEFAULT '',
                    severity TEXT DEFAULT 'info',
                    metadata_json TEXT DEFAULT '{}',
                    outcome TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_events(timestamp);
                CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_events(session_id);
                CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_events(event_type);
                CREATE INDEX IF NOT EXISTS idx_audit_severity ON audit_events(severity);
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("Audit DB init failed: %s", e)

    def start(self) -> None:
        """Start the background writer thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._write_loop, daemon=True)
        self._thread.start()
        logger.debug("Audit writer started")

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the background writer thread."""
        if not self._running:
            return
        self._running = False
        # Send sentinel to unblock queue
        try:
            self._queue.put(None, timeout=1.0)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=timeout)
        self._thread = None
        logger.debug("Audit writer stopped")

    def record(self, event: AuditEvent) -> bool:
        """Record an audit event (non-blocking).

        Args:
            event: The audit event to record

        Returns:
            True if queued successfully, False if queue is full
        """
        # Fill in execution context if not set
        ctx = get_execution_context()
        if not event.session_id:
            event.session_id = ctx.session_id
        if not event.run_id:
            event.run_id = ctx.run_id
        if not event.user_id:
            event.user_id = ctx.user_id
        if not event.agent_id:
            event.agent_id = ctx.agent_id
        if not event.workspace_dir:
            event.workspace_dir = ctx.workspace_dir
        if not event.provider:
            event.provider = ctx.provider
        if not event.model_id:
            event.model_id = ctx.model_id

        # Fill in defaults
        if not event.event_id:
            event.event_id = f"audit-{uuid.uuid4().hex[:12]}"
        if not event.timestamp:
            event.timestamp = time.time()

        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            logger.warning("Audit queue full — event dropped")
            return False

    def record_simple(
        self,
        event_type: str,
        action: str = "",
        detail: str = "",
        severity: str = "info",
        outcome: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Record a simple audit event (convenience method)."""
        event = AuditEvent(
            event_type=event_type,
            action=action,
            detail=detail,
            severity=severity,
            outcome=outcome,
            metadata_json=json.dumps(metadata or {}),
        )
        return self.record(event)

    def query(
        self,
        event_type: str | None = None,
        session_id: str | None = None,
        severity: str | None = None,
        since: float | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Query audit events."""
        try:
            conn = sqlite3.connect(str(self._db_path))
            conditions = []
            params: list[Any] = []

            if event_type:
                conditions.append("event_type = ?")
                params.append(event_type)
            if session_id:
                conditions.append("session_id = ?")
                params.append(session_id)
            if severity:
                conditions.append("severity = ?")
                params.append(severity)
            if since:
                conditions.append("timestamp >= ?")
                params.append(since)

            query = "SELECT * FROM audit_events"
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            conn.close()

            return [
                AuditEvent(
                    event_id=row[0], event_type=row[1], action=row[2],
                    detail=row[3], timestamp=row[4], session_id=row[5],
                    run_id=row[6], user_id=row[7], agent_id=row[8],
                    workspace_dir=row[9], provider=row[10], model_id=row[11],
                    severity=row[12], metadata_json=row[13], outcome=row[14],
                )
                for row in rows
            ]
        except Exception as e:
            logger.warning("Audit query failed: %s", e)
            return []

    def get_stats(self, days: int = 7) -> dict[str, Any]:
        """Get audit statistics for the last N days."""
        since = time.time() - (days * 86400)
        events = self.query(since=since, limit=10000)

        by_type: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        by_outcome: dict[str, int] = {}

        for event in events:
            by_type[event.event_type] = by_type.get(event.event_type, 0) + 1
            by_severity[event.severity] = by_severity.get(event.severity, 0) + 1
            by_outcome[event.outcome] = by_outcome.get(event.outcome, 0) + 1

        return {
            "total_events": len(events),
            "by_type": by_type,
            "by_severity": by_severity,
            "by_outcome": by_outcome,
            "days": days,
        }

    # -- Internal -----------------------------------------------------------

    def _write_loop(self) -> None:
        """Background thread that writes events to SQLite."""
        while self._running:
            try:
                event = self._queue.get(timeout=1.0)
                if event is None:  # Sentinel
                    break
                self._write_event(event)
            except queue.Empty:
                continue
            except Exception as e:
                logger.error("Audit write loop error: %s", e)

        # Drain remaining events
        while True:
            try:
                event = self._queue.get_nowait()
                if event is None:
                    break
                self._write_event(event)
            except queue.Empty:
                break

    def _write_event(self, event: AuditEvent) -> None:
        """Write a single event to SQLite."""
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=self.BUSY_TIMEOUT_MS / 1000)
            conn.execute("""
                INSERT INTO audit_events
                (event_id, event_type, action, detail, timestamp, session_id,
                 run_id, user_id, agent_id, workspace_dir, provider, model_id,
                 severity, metadata_json, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event.event_id, event.event_type, event.action, event.detail,
                event.timestamp, event.session_id, event.run_id, event.user_id,
                event.agent_id, event.workspace_dir, event.provider,
                event.model_id, event.severity, event.metadata_json, event.outcome,
            ))
            conn.commit()
            conn.close()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                self._contention_count += 1
                if self._contention_count % 100 == 0:
                    logger.warning("Audit DB contention: %d occurrences", self._contention_count)
            else:
                logger.warning("Audit write failed: %s", e)
        except Exception as e:
            logger.warning("Audit write failed: %s", e)

    def _prune_old(self) -> int:
        """Prune old audit events. Returns count removed."""
        try:
            conn = sqlite3.connect(str(self._db_path))
            cutoff = time.time() - (self.PRUNE_AFTER_DAYS * 86400)

            # Delete by age
            cursor = conn.execute(
                "DELETE FROM audit_events WHERE timestamp < ?",
                (cutoff,),
            )
            deleted = cursor.rowcount

            # If still too many, delete oldest
            cursor = conn.execute("SELECT COUNT(*) FROM audit_events")
            count = cursor.fetchone()[0]
            if count > self.PRUNE_MAX_ROWS:
                excess = count - self.PRUNE_MAX_ROWS
                conn.execute(
                    "DELETE FROM audit_events WHERE event_id IN "
                    "(SELECT event_id FROM audit_events ORDER BY timestamp ASC LIMIT ?)",
                    (excess,),
                )
                deleted += excess

            conn.commit()
            conn.close()
            return deleted
        except Exception as e:
            logger.warning("Audit prune failed: %s", e)
            return 0


# ---------------------------------------------------------------------------
# Global audit writer (singleton)
# ---------------------------------------------------------------------------

_global_writer: AuditWriter | None = None


def get_audit_writer() -> AuditWriter:
    """Get the global audit writer instance."""
    global _global_writer
    if _global_writer is None:
        _global_writer = AuditWriter()
        _global_writer.start()
    return _global_writer


def audit(
    event_type: str,
    action: str = "",
    detail: str = "",
    severity: str = "info",
    outcome: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record an audit event using the global writer."""
    get_audit_writer().record_simple(
        event_type=event_type,
        action=action,
        detail=detail,
        severity=severity,
        outcome=outcome,
        metadata=metadata,
    )
