"""Builtin memory provider — file-backed curated memory with frozen snapshot.

Ported from Hermes Agent (tools/memory_tool.py), adapted for Kyourai.

Two stores:
  - MEMORY.md: agent's personal notes (environment facts, conventions, quirks)
  - USER.md: what the agent knows about the user (preferences, style, workflow)

Frozen snapshot pattern (the key innovation):
  - Snapshot loaded at session start → injected into system prompt
  - Mid-session writes update files on disk immediately (durable) but do NOT
    change the system prompt → preserves the prefix cache for the entire session
  - Snapshot refreshes on the next session start

Entry delimiter: § (section sign). Entries can be multiline.
Character limits (not tokens) because char counts are model-independent.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from kyourai.constants import get_memory_dir
from kyourai.memory.provider import MemoryProvider, RecallStatus
from kyourai.utils import atomic_write_text

# fcntl is Unix-only; on Windows use msvcrt for file locking
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Stable header prefixes for system-prompt memory blocks. Exported so
# compression can detect leftover blocks.
MEMORY_BLOCK_HEADERS = {
    "memory": "MEMORY (your personal notes)",
    "user": "USER PROFILE (who the user is)",
}

ENTRY_DELIMITER = "\n§\n"

# Sentinel for "file exists but could not be read" — distinct from clean reload
_READ_FAILED = object()


def _scan_memory_content(content: str) -> str | None:
    """Scan memory content for injection/exfil patterns. Returns error string if blocked."""
    from kyourai.tools.threat_patterns import first_threat_message
    return first_threat_message(content, scope="strict")


def _drift_error(path: Path, bak_path: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": (
            f"Refusing to write {path.name}: file on disk has content that "
            f"wouldn't round-trip through the memory tool (likely added by "
            f"a patch, shell append, manual edit, or concurrent session). "
            f"A snapshot was saved to {bak_path}. Resolve the drift first."
        ),
        "drift_backup": bak_path,
    }


def _read_failed_error(path: Path) -> dict[str, Any]:
    return {
        "success": False,
        "error": (
            f"Refusing to write {path.name}: the file exists but could not "
            f"be read right now. Treating an unreadable file as empty and "
            f"saving would wipe existing memory. Nothing was changed."
        ),
    }


class MemoryStore:
    """Bounded curated memory with file persistence. One instance per agent.

    Maintains two parallel states:
      - _system_prompt_snapshot: frozen at load time, used for system prompt.
        Never mutated mid-session. Keeps prefix cache stable.
      - memory_entries / user_entries: live state, mutated by tool calls,
        persisted to disk. Tool responses always reflect this live state.
    """

    _MAX_CONSOLIDATION_FAILURES_PER_TURN = 3

    def __init__(self, memory_char_limit: int = 2200, user_char_limit: int = 1375):
        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        self._system_prompt_snapshot: dict[str, str] = {"memory": "", "user": ""}
        self._consolidation_failures = 0

    def reset_consolidation_failures(self) -> None:
        self._consolidation_failures = 0

    def _consolidation_failure(self, response: dict[str, Any]) -> dict[str, Any]:
        self._consolidation_failures += 1
        if self._consolidation_failures <= self._MAX_CONSOLIDATION_FAILURES_PER_TURN:
            return response
        return {"success": False, "done": True, "error": "Memory consolidation limit reached for this turn."}

    def load_from_disk(self) -> None:
        """Load entries from MEMORY.md and USER.md, capture system prompt snapshot.

        Scans each entry for injection/promptware patterns at snapshot-build time.
        ANY hit replaces the entry text in the snapshot with a placeholder, so a
        poisoned-on-disk memory file cannot inject into the system prompt.

        The live entries keep the raw text so the user can still see and remove
        poisoned entries. Scanning is deterministic from disk bytes, so the
        snapshot remains stable for the entire session (prefix-cache invariant).
        """
        mem_dir = get_memory_dir()
        mem_dir.mkdir(parents=True, exist_ok=True)

        self.memory_entries = self._read_file(mem_dir / "MEMORY.md")
        self.user_entries = self._read_file(mem_dir / "USER.md")

        # Deduplicate (preserves order, keeps first occurrence)
        self.memory_entries = list(dict.fromkeys(self.memory_entries))
        self.user_entries = list(dict.fromkeys(self.user_entries))

        sanitized_memory = self._sanitize_entries_for_snapshot(self.memory_entries, "MEMORY.md")
        sanitized_user = self._sanitize_entries_for_snapshot(self.user_entries, "USER.md")

        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", sanitized_memory),
            "user": self._render_block("user", sanitized_user),
        }

    @staticmethod
    def _sanitize_entries_for_snapshot(entries: list[str], filename: str) -> list[str]:
        from kyourai.tools.threat_patterns import scan_for_threats

        sanitized: list[str] = []
        for entry in entries:
            if not entry or entry.startswith("[BLOCKED:"):
                sanitized.append(entry)
                continue
            findings = scan_for_threats(entry, scope="strict")
            if findings:
                logger.warning("Memory entry from %s blocked at load time: %s", filename, ", ".join(findings))
                sanitized.append(
                    f"[BLOCKED: {filename} entry contained threat pattern(s): "
                    f"{', '.join(findings)}. Removed from system prompt; "
                    f"use memory(action=remove) to delete the original.]"
                )
            else:
                sanitized.append(entry)
        return sanitized

    @staticmethod
    @contextmanager
    def _file_lock(path: Path):
        """Acquire an exclusive file lock for read-modify-write safety."""
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        if fcntl is None and msvcrt is None:
            yield
            return

        fd = open(lock_path, "a+", encoding="utf-8")
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except (OSError, IOError):
                    pass
            elif msvcrt:
                try:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass
            fd.close()

    @staticmethod
    def _path_for(target: str) -> Path:
        mem_dir = get_memory_dir()
        if target == "user":
            return mem_dir / "USER.md"
        return mem_dir / "MEMORY.md"

    def _reload_target(self, target: str, *, skip_drift: bool = False):
        """Re-read entries from disk under file lock. Returns backup path if drift detected."""
        path = self._path_for(target)
        raw, read_ok = self._read_raw_checked(path)
        if not read_ok:
            return _READ_FAILED
        bak = None if skip_drift else self._detect_external_drift(target, raw)
        fresh = self._parse_entries(raw)
        fresh = list(dict.fromkeys(fresh))
        self._set_entries(target, fresh)
        return bak

    def save_to_disk(self, target: str) -> None:
        """Persist entries to the appropriate file. Called after every mutation."""
        get_memory_dir().mkdir(parents=True, exist_ok=True)
        self._write_file(self._path_for(target), self._entries_for(target))

    def _entries_for(self, target: str) -> list[str]:
        return self.user_entries if target == "user" else self.memory_entries

    def _set_entries(self, target: str, entries: list[str]) -> None:
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        return len(ENTRY_DELIMITER.join(entries)) if entries else 0

    def _char_limit(self, target: str) -> int:
        return self.user_char_limit if target == "user" else self.memory_char_limit

    # -- Mutations -----------------------------------------------------------

    def add(self, target: str, content: str) -> dict[str, Any]:
        """Append a new entry. Returns error if it would exceed the char limit."""
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        scan_error = _scan_memory_content(content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            if self._reload_target(target, skip_drift=True) is _READ_FAILED:
                return _read_failed_error(self._path_for(target))

            entries = self._entries_for(target)
            limit = self._char_limit(target)

            if content in entries:
                return self._success_response(target, "Entry already exists (no duplicate added).")

            new_entries = entries + [content]
            new_total = len(ENTRY_DELIMITER.join(new_entries))

            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": (
                        f"Memory at {current:,}/{limit:,} chars. "
                        f"Adding this entry ({len(content)} chars) would exceed the limit. "
                        f"Consolidate: use 'replace' to merge or 'remove' stale entries, then retry."
                    ),
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                })

            entries.append(content)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> dict[str, Any]:
        """Find entry containing old_text substring, replace it with new_content."""
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}
        if not new_content:
            return {"success": False, "error": "new_content cannot be empty. Use 'remove' to delete."}

        scan_error = _scan_memory_content(new_content)
        if scan_error:
            return {"success": False, "error": scan_error}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak is _READ_FAILED:
                return _read_failed_error(self._path_for(target))
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return self._consolidation_failure({
                    "success": False,
                    "error": f"No entry matched '{old_text}'. Check current_entries and retry.",
                    "current_entries": entries,
                })

            if len(matches) > 1:
                unique_texts = {e for _, e in matches}
                if len(unique_texts) > 1:
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": self._previews([e for _, e in matches]),
                    }

            idx = matches[0][0]
            limit = self._char_limit(target)

            test_entries = entries.copy()
            test_entries[idx] = new_content
            new_total = len(ENTRY_DELIMITER.join(test_entries))

            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": f"Replacement would put memory at {new_total:,}/{limit:,} chars. Shorten or remove other entries.",
                    "current_entries": entries,
                    "usage": f"{current:,}/{limit:,}",
                })

            entries[idx] = new_content
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> dict[str, Any]:
        """Remove the entry containing old_text substring."""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text cannot be empty."}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak is _READ_FAILED:
                return _read_failed_error(self._path_for(target))
            if bak:
                return _drift_error(self._path_for(target), bak)

            entries = self._entries_for(target)
            matches = [(i, e) for i, e in enumerate(entries) if old_text in e]

            if not matches:
                return self._consolidation_failure({
                    "success": False,
                    "error": f"No entry matched '{old_text}'. Check current_entries and retry.",
                    "current_entries": entries,
                })

            if len(matches) > 1:
                unique_texts = {e for _, e in matches}
                if len(unique_texts) > 1:
                    return {
                        "success": False,
                        "error": f"Multiple entries matched '{old_text}'. Be more specific.",
                        "matches": self._previews([e for _, e in matches]),
                    }

            idx = matches[0][0]
            entries.pop(idx)
            self._set_entries(target, entries)
            self.save_to_disk(target)

        return self._success_response(target, "Entry removed.")

    def apply_batch(self, target: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """Apply a sequence of add/replace/remove ops atomically.

        All operations validated and applied against the FINAL budget.
        All-or-nothing: if any op fails, nothing is written.
        """
        if not operations:
            return {"success": False, "error": "operations list is empty."}

        for i, op in enumerate(operations):
            act = (op or {}).get("action")
            new_content = (op or {}).get("content")
            if act in {"add", "replace"} and new_content:
                scan_error = _scan_memory_content(new_content)
                if scan_error:
                    return {"success": False, "error": f"Operation {i + 1}: {scan_error}"}

        with self._file_lock(self._path_for(target)):
            bak = self._reload_target(target)
            if bak is _READ_FAILED:
                return _read_failed_error(self._path_for(target))
            if bak:
                return _drift_error(self._path_for(target), bak)

            working: list[str] = list(self._entries_for(target))
            limit = self._char_limit(target)

            for i, op in enumerate(operations):
                op = op or {}
                act = op.get("action")
                content = (op.get("content") or op.get("new_text") or "").strip()
                old_text = (op.get("old_text") or "").strip()
                pos = f"Operation {i + 1} ({act or 'unknown'})"

                if act == "add":
                    if not content:
                        return self._batch_error(target, f"{pos}: content is required.")
                    if content in working:
                        continue
                    working.append(content)
                elif act == "replace":
                    if not old_text:
                        return self._batch_error(target, f"{pos}: old_text is required.")
                    if not content:
                        return self._batch_error(target, f"{pos}: content is required.")
                    matches = [j for j, e in enumerate(working) if old_text in e]
                    if not matches:
                        return self._batch_error(target, f"{pos}: no entry matched '{old_text}'.")
                    if len({working[j] for j in matches}) > 1:
                        return self._batch_error(target, f"{pos}: '{old_text}' matched multiple entries.")
                    working[matches[0]] = content
                elif act == "remove":
                    if not old_text:
                        return self._batch_error(target, f"{pos}: old_text is required.")
                    matches = [j for j, e in enumerate(working) if old_text in e]
                    if not matches:
                        return self._batch_error(target, f"{pos}: no entry matched '{old_text}'.")
                    if len({working[j] for j in matches}) > 1:
                        return self._batch_error(target, f"{pos}: '{old_text}' matched multiple entries.")
                    working.pop(matches[0])
                else:
                    return self._batch_error(target, f"{pos}: unknown action. Use add, replace, or remove.")

            new_total = len(ENTRY_DELIMITER.join(working)) if working else 0
            if new_total > limit:
                current = self._char_count(target)
                return self._consolidation_failure({
                    "success": False,
                    "error": f"After all {len(operations)} ops, memory would be at {new_total:,}/{limit:,} chars — over limit.",
                    "current_entries": self._entries_for(target),
                    "usage": f"{current:,}/{limit:,}",
                })

            self._set_entries(target, working)
            self.save_to_disk(target)

        return self._success_response(target, f"Applied {len(operations)} operation(s).")

    def _batch_error(self, target: str, message: str) -> dict[str, Any]:
        return {
            "success": False,
            "error": message,
            "current_entries": self._entries_for(target),
            "usage": f"{self._char_count(target):,}/{self._char_limit(target):,}",
        }

    @staticmethod
    def _previews(entries: list[str], width: int = 80) -> list[str]:
        return [e[:width] + ("..." if len(e) > width else "") for e in entries]

    def _success_response(self, target: str, message: str | None = None) -> dict[str, Any]:
        self._consolidation_failures = 0
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        resp = {
            "success": True,
            "done": True,
            "target": target,
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        resp["note"] = "Write saved. This update is complete — do not repeat it."
        return resp

    def _render_block(self, target: str, entries: list[str]) -> str:
        """Render a system prompt block with header and usage indicator."""
        if not entries:
            return ""
        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        header_key = "user" if target == "user" else "memory"
        header = f"{MEMORY_BLOCK_HEADERS[header_key]} [{pct}% — {current:,}/{limit:,} chars]"
        separator = "═" * 46
        return f"{separator}\n{header}\n{separator}\n{content}"

    # -- File I/O ------------------------------------------------------------

    @staticmethod
    def _read_raw_checked(path: Path) -> tuple[str, bool]:
        """Read raw text, distinguishing unreadable from empty.

        Returns (raw, read_ok). read_ok is False ONLY when file exists but
        could not be read. Absent file is clean ("", True).
        """
        if not path.exists():
            return "", True
        try:
            return path.read_text(encoding="utf-8-sig"), True
        except (OSError, IOError, UnicodeDecodeError):
            return "", False

    @staticmethod
    def _parse_entries(raw: str) -> list[str]:
        if not raw.strip():
            return []
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    @staticmethod
    def _read_file(path: Path) -> list[str]:
        """Read + parse. Empty list on any error (read-only callers)."""
        raw, read_ok = MemoryStore._read_raw_checked(path)
        if not read_ok:
            return []
        return MemoryStore._parse_entries(raw)

    def _detect_external_drift(self, target: str, raw: str) -> str | None:
        """Return backup-path string if on-disk content shows external drift."""
        path = self._path_for(target)
        if not raw.strip():
            return None

        parsed = [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
        roundtrip = ENTRY_DELIMITER.join(parsed)
        char_limit = self._char_limit(target)
        max_entry_len = max((len(e) for e in parsed), default=0)

        drift_detected = (raw.strip() != roundtrip) or (max_entry_len > char_limit)
        if not drift_detected:
            return None

        ts = int(time.time())
        bak_path = path.with_suffix(path.suffix + f".bak.{ts}")
        try:
            bak_path.write_text(raw, encoding="utf-8")
        except (OSError, IOError):
            return str(bak_path) + " (BACKUP FAILED — file unchanged on disk)"
        return str(bak_path)

    @staticmethod
    def _write_file(path: Path, entries: list[str]) -> None:
        """Write entries atomically via temp-file + rename."""
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        try:
            atomic_write_text(path, content, tmp_prefix=".mem_")
        except (OSError, IOError) as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}") from e

    # -- Snapshot access -----------------------------------------------------

    def get_snapshot(self) -> dict[str, str]:
        """Return the frozen system-prompt snapshot."""
        return self._system_prompt_snapshot

    def handle_tool_call(self, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a memory tool call. Returns result dict."""
        action = args.get("action", "")
        target = args.get("target", "memory")
        content = args.get("content") or args.get("new_text")
        old_text = args.get("old_text")
        operations = args.get("operations")

        if operations:
            return self.apply_batch(target, operations)
        if action == "add":
            return self.add(target, content or "")
        if action == "replace":
            return self.replace(target, old_text or "", content or "")
        if action == "remove":
            return self.remove(target, old_text or "")
        return {"success": False, "error": f"Unknown action '{action}'. Use add, replace, or remove."}


# ---------------------------------------------------------------------------
# BuiltinMemoryProvider — wraps MemoryStore as a MemoryProvider
# ---------------------------------------------------------------------------

MEMORY_TOOL_SCHEMA = {
    "name": "memory",
    "description": (
        "Save durable facts to persistent memory that survive across sessions. Memory is "
        "injected into every future turn, so keep entries compact and high-signal.\n\n"
        "HOW: make ALL changes in ONE call via an 'operations' array (each item: "
        "{action, content?, old_text?}). The batch applies atomically and the char limit "
        "is checked only on the FINAL result. Use bare action/content/old_text only for a "
        "single lone change.\n\n"
        "WHEN: save proactively when the user states a preference, correction, or personal "
        "detail, or you learn a stable fact about their environment, conventions, or workflow.\n\n"
        "TARGETS: 'user' = who the user is (name, role, preferences, style). 'memory' = your "
        "notes (environment, conventions, tool quirks, lessons).\n\n"
        "SKIP: trivial/obvious info, easily re-discovered facts, raw data dumps, task progress."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "replace", "remove"],
                "description": "The action to perform (single-op shape). Omit when using 'operations'.",
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "Which memory store: 'memory' for personal notes, 'user' for user profile.",
            },
            "content": {
                "type": "string",
                "description": "The entry content. Required for 'add' and 'replace'.",
            },
            "old_text": {
                "type": "string",
                "description": "REQUIRED for 'replace' and 'remove': a short unique substring identifying the existing entry.",
            },
            "operations": {
                "type": "array",
                "description": "Batch shape: list of operations applied atomically. Each item: {action, content?, old_text?}.",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                        "content": {"type": "string"},
                        "old_text": {"type": "string"},
                    },
                    "required": ["action"],
                },
            },
        },
        "required": ["target"],
    },
}


class BuiltinMemoryProvider(MemoryProvider):
    """The built-in file-backed memory provider. Always registered first."""

    def __init__(self, memory_char_limit: int = 2200, user_char_limit: int = 1375):
        self._store = MemoryStore(memory_char_limit, user_char_limit)
        self._initialized = False

    @property
    def name(self) -> str:
        return "builtin"

    def is_available(self) -> bool:
        return True  # always available — just needs filesystem

    def initialize(self, session_id: str, **kwargs) -> None:
        self._store.load_from_disk()
        self._initialized = True

    def system_prompt_block(self) -> str:
        if not self._initialized:
            self._store.load_from_disk()
            self._initialized = True
        snapshot = self._store.get_snapshot()
        parts = [snapshot["memory"], snapshot["user"]]
        return "\n\n".join(p for p in parts if p)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        # Builtin memory is already in the system prompt as a frozen snapshot.
        # No additional prefetch needed.
        return ""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        # Builtin memory is explicitly curated by the model via the memory tool.
        # No automatic sync — the model decides what to remember.
        pass

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [MEMORY_TOOL_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        if tool_name != "memory":
            raise NotImplementedError(f"BuiltinMemoryProvider does not handle tool {tool_name}")
        result = self._store.handle_tool_call(args)
        return json.dumps(result, ensure_ascii=False)

    def recall_status(self) -> RecallStatus | None:
        snapshot = self._store.get_snapshot()
        total = len(snapshot.get("memory", "")) + len(snapshot.get("user", ""))
        if total == 0:
            return None
        return RecallStatus(provider_label="builtin", count=0, glyph="🧠")

    def shutdown(self) -> None:
        pass

    @property
    def store(self) -> MemoryStore:
        """Direct access to the underlying MemoryStore (for tests, CLI, etc.)."""
        return self._store
