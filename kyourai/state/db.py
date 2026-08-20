"""SessionDB — SQLite-backed session and message store with FTS5 search.

Stores conversation history so the agent can rewind, the user can search
past conversations, and the insights engine can compute analytics.

Design notes:
  - Shared connection registry (like MemoryStore) — all instances pointing
    to the same db_path share one connection + one lock. Prevents write
    contention.
  - WAL mode with fallback to default journal.
  - FTS5 virtual table over message content with triggers for auto-sync.
  - Schema versioned (SCHEMA_VERSION) for forward migration.
  - Thread-safe via RLock on all write operations.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from kyourai.constants import get_state_db_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'cli',
    model TEXT,
    team_id TEXT,
    user_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    title TEXT,
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT,
    tool_name TEXT,
    tool_call_id TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id',
    tokenize='porter unicode61'
);

-- FTS5 sync triggers
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_team ON sessions(team_id);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_role ON messages(session_id, role);
"""

# ---------------------------------------------------------------------------
# Shared connection registry (same pattern as MemoryStore)
# ---------------------------------------------------------------------------

_shared: dict[str, dict[str, Any]] = {}
_shared_guard = threading.Lock()


def _apply_wal_with_fallback(conn: sqlite3.Connection, db_label: str = "state.db") -> None:
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        logger.warning("WAL mode unavailable for %s; using default journal mode", db_label)


# ---------------------------------------------------------------------------
# SessionDB
# ---------------------------------------------------------------------------


class SessionDB:
    """SQLite-backed session and message store with FTS5 full-text search.

    Usage:
        db = SessionDB()  # uses default path from constants
        sid = db.create_session("my-session", model="openai:gpt-4o")
        db.add_message(sid, role="user", content="Hello")
        db.add_message(sid, role="assistant", content="Hi there!")
        db.end_session(sid)

        # Search past conversations
        results = db.search_messages("deploy kubernetes")
    """

    def __init__(
        self,
        db_path: "str | Path | None" = None,
    ) -> None:
        if db_path is None:
            db_path = str(get_state_db_path())
        self.db_path = str(db_path)
        self._key = str(Path(self.db_path).resolve())

        # Ensure parent directory exists
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        with _shared_guard:
            entry = _shared.get(self._key)
            if entry is None:
                conn = sqlite3.connect(
                    self.db_path,
                    check_same_thread=False,
                    isolation_level=None,  # autocommit
                )
                conn.row_factory = sqlite3.Row
                _apply_wal_with_fallback(conn)
                conn.execute("PRAGMA foreign_keys=ON")
                entry = {"conn": conn, "lock": threading.RLock(), "refs": 0}
                _shared[self._key] = entry
            entry["refs"] += 1
            self._conn = entry["conn"]
            self._lock = entry["lock"]

        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA_SQL)
            row = self._conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] < SCHEMA_VERSION:
                # Future: migration logic here
                self._conn.execute(
                    "UPDATE schema_version SET version = ?", (SCHEMA_VERSION,)
                )

    # -- Session lifecycle ---------------------------------------------------

    def create_session(
        self,
        session_id: str,
        *,
        source: str = "cli",
        model: str = "",
        team_id: str = "",
        user_id: str = "",
        title: str = "",
    ) -> str:
        """Create a new session record. Returns session_id."""
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO sessions
                   (id, source, model, team_id, user_id, started_at, title)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, source, model, team_id, user_id, time.time(), title),
            )
            return session_id

    def end_session(self, session_id: str, *, end_reason: str = "") -> bool:
        """Mark a session as ended. Returns True if session existed."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                (time.time(), session_id),
            )
            return cur.rowcount > 0

    def update_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        model: str | None = None,
        archived: int | None = None,
    ) -> bool:
        """Update session metadata. Returns True if updated."""
        sets: list[str] = []
        params: list[Any] = []
        if title is not None:
            sets.append("title = ?")
            params.append(title)
        if model is not None:
            sets.append("model = ?")
            params.append(model)
        if archived is not None:
            sets.append("archived = ?")
            params.append(archived)
        if not sets:
            return False
        params.append(session_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            return cur.rowcount > 0

    # -- Messages ------------------------------------------------------------

    def add_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str = "",
        tool_name: str = "",
        tool_call_id: str = "",
        token_count: int | None = None,
    ) -> int:
        """Add a message to a session. Returns message id.

        Auto-creates the session if it doesn't exist (defensive).
        """
        ts = time.time()
        with self._lock:
            # Ensure session exists (auto-create if needed)
            row = self._conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO sessions (id, source, started_at)
                       VALUES (?, 'auto', ?)""",
                    (session_id, ts),
                )

            cur = self._conn.execute(
                """INSERT INTO messages
                   (session_id, role, content, tool_name, tool_call_id, timestamp, token_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, role, content, tool_name, tool_call_id, ts, token_count),
            )
            msg_id = cur.lastrowid

            # Update session aggregates (don't touch ended_at — only end_session sets that)
            is_tool = 1 if role == "tool" or tool_name else 0
            self._conn.execute(
                """UPDATE sessions
                   SET message_count = message_count + 1,
                       tool_call_count = tool_call_count + ?
                   WHERE id = ?""",
                (is_tool, session_id),
            )
            return msg_id

    def add_turn(
        self,
        session_id: str,
        user_content: str,
        assistant_content: str,
        *,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        """Convenience: persist a complete user→assistant turn.

        Optionally records tool call messages between user and assistant.
        """
        self.add_message(session_id, role="user", content=user_content)
        if tool_calls:
            for tc in tool_calls:
                self.add_message(
                    session_id,
                    role="tool",
                    content=str(tc.get("result", "")),
                    tool_name=tc.get("name", ""),
                    tool_call_id=tc.get("id", ""),
                )
        self.add_message(session_id, role="assistant", content=assistant_content)

    # -- Queries -------------------------------------------------------------

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Return session row as dict, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_sessions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        source: str | None = None,
        team_id: str | None = None,
        archived: bool = False,
    ) -> list[dict[str, Any]]:
        """List sessions, newest first."""
        clauses = ["archived = ?"]
        params: list[Any] = [1 if archived else 0]
        if source:
            clauses.append("source = ?")
            params.append(source)
        if team_id:
            clauses.append("team_id = ?")
            params.append(team_id)
        where = " AND ".join(clauses)
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT * FROM sessions WHERE {where}
                    ORDER BY started_at DESC LIMIT ? OFFSET ?""",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def get_messages(
        self,
        session_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get messages for a session, chronological order."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM messages
                   WHERE session_id = ?
                   ORDER BY timestamp ASC, id ASC
                   LIMIT ? OFFSET ?""",
                (session_id, limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_message_history(
        self,
        session_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, str]]:
        """Get messages formatted for Pydantic AI message_history.

        Returns list of {'role': ..., 'content': ...} dicts, newest first
        (for message_history parameter).
        """
        msgs = self.get_messages(session_id, limit=limit)
        history: list[dict[str, str]] = []
        for m in msgs:
            if m["role"] in ("user", "assistant") and m.get("content"):
                history.append({"role": m["role"], "content": m["content"]})
        return history

    def search_messages(
        self,
        query: str,
        *,
        limit: int = 20,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Full-text search across message content via FTS5.

        Returns results with session_id, role, content, timestamp, and
        a snippet of the matching text.
        """
        if not query.strip():
            return []
        # Sanitize FTS5 query — quote tokens to prevent syntax errors
        tokens = query.strip().split()
        fts_query = " OR ".join(f'"{t}"' for t in tokens if t)

        clauses: list[str] = []
        params: list[Any] = []
        base_sql = f"""
            SELECT m.id, m.session_id, m.role, m.content, m.timestamp,
                   snippet(messages_fts, 0, '>>>', '<<<', '...', 20) as snippet,
                   s.model, s.title
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE messages_fts MATCH ?
        """
        params.append(fts_query)
        if session_id:
            base_sql += " AND m.session_id = ?"
            params.append(session_id)
        base_sql += " ORDER BY m.timestamp DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            try:
                rows = self._conn.execute(base_sql, params).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError as e:
                logger.warning("FTS search failed for query '%s': %s", query, e)
                return []

    def count_sessions(self, *, source: str | None = None) -> int:
        """Count total sessions."""
        if source:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE source = ?", (source,)
                ).fetchone()
        else:
            with self._lock:
                row = self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        return row[0] if row else 0

    def count_messages(self) -> int:
        """Count total messages across all sessions."""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()
            return row[0] if row else 0

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all its messages. Returns True if existed."""
        with self._lock:
            # Delete messages first (FTS triggers handle FTS cleanup)
            self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            cur = self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return cur.rowcount > 0

    # -- Lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Release the shared connection. Only closes when last ref releases."""
        with _shared_guard:
            entry = _shared.get(self._key)
            if entry is None:
                return
            entry["refs"] -= 1
            if entry["refs"] <= 0:
                try:
                    entry["conn"].close()
                except Exception as e:
                    logger.debug("SessionDB close failed: %s", e)
                del _shared[self._key]
        self._conn = None  # type: ignore[assignment]
        self._lock = None  # type: ignore[assignment]
