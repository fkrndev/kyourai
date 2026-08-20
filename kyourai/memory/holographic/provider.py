"""HolographicMemoryProvider — MemoryProvider implementation for the holographic store.

Wraps MemoryStore + FactRetriever as a MemoryProvider plugin, exposing
two tools to the model:
  - fact_store: add/search/probe/related/reason/contradict/update/remove/list
  - fact_feedback: rate facts (helpful/unhelpful) to train trust scores

Config (kyourai config.yaml under memory.holographic):
  db_path: path to SQLite database (default: $KYOURAI_HOME/memory_store.db)
  auto_extract: bool — auto-extract facts at session end (default: false)
  default_trust: float — trust score for new facts (default: 0.5)
  hrr_dim: int — HRR vector dimensions (default: 1024)
  min_trust_threshold: float — minimum trust for prefetch results (default: 0.3)
  temporal_decay_half_life: int — days, 0 = disabled (default: 0)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from kyourai.memory.holographic import hrr
from kyourai.memory.holographic.store import MemoryStore
from kyourai.memory.holographic.retrieval import FactRetriever
from kyourai.memory.provider import MemoryProvider, RecallStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

FACT_STORE_SCHEMA = {
    "name": "fact_store",
    "description": (
        "Deep structured memory with algebraic reasoning. "
        "Use alongside the memory tool — memory for always-on context, "
        "fact_store for deep recall and compositional queries.\n\n"
        "ACTIONS (simple → powerful):\n"
        "• add — Store a fact the user would expect you to remember.\n"
        "• search — Keyword lookup ('editor config', 'deploy process').\n"
        "• probe — Entity recall: ALL facts about a person/thing.\n"
        "• related — What connects to an entity? Structural adjacency.\n"
        "• reason — Compositional: facts connected to MULTIPLE entities simultaneously.\n"
        "• contradict — Memory hygiene: find facts making conflicting claims.\n"
        "• update/remove/list — CRUD operations.\n\n"
        "IMPORTANT: Before answering questions about the user, ALWAYS probe or reason first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "search", "probe", "related", "reason", "contradict", "update", "remove", "list"],
            },
            "content": {"type": "string", "description": "Fact content (required for 'add')."},
            "query": {"type": "string", "description": "Search query (required for 'search')."},
            "entity": {"type": "string", "description": "Entity name for 'probe'/'related'."},
            "entities": {"type": "array", "items": {"type": "string"}, "description": "Entity names for 'reason'."},
            "fact_id": {"type": "integer", "description": "Fact ID for 'update'/'remove'."},
            "category": {"type": "string", "enum": ["user_pref", "project", "tool", "general"]},
            "tags": {"type": "string", "description": "Comma-separated tags."},
            "trust_delta": {"type": "number", "description": "Trust adjustment for 'update'."},
            "min_trust": {"type": "number", "description": "Minimum trust filter (default: 0.3)."},
            "limit": {"type": "integer", "description": "Max results (default: 10)."},
        },
        "required": ["action"],
    },
}

FACT_FEEDBACK_SCHEMA = {
    "name": "fact_feedback",
    "description": (
        "Rate a fact after using it. Mark 'helpful' if accurate, 'unhelpful' if outdated. "
        "This trains the memory — good facts rise, bad facts sink."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["helpful", "unhelpful"]},
            "fact_id": {"type": "integer", "description": "The fact ID to rate."},
        },
        "required": ["action", "fact_id"],
    },
}


def _tool_error(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Heuristic auto-extraction (no LLM required)
# ---------------------------------------------------------------------------

# Patterns that signal the user wants something remembered.
# Each pattern: (regex, category). The captured group is the fact content.
_AUTO_EXTRACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Explicit memory requests: "remember that ...", "note that ...", "don't forget ..."
    (
        re.compile(
            r"(?:please\s+)?(?:remember(?:\s+that)?|note(?:\s+that)?|don'?t\s+forget(?:\s+that)?|keep\s+in\s+mind(?:\s+that)?)\s*[,:!]?\s*(.+)",
            re.IGNORECASE,
        ),
        "general",
    ),
    # User preferences: "I prefer ...", "I like ...", "I hate ...", "I always ..."
    (
        re.compile(
            r"I\s+(?:prefer|like|love|hate|always|never|usually|tend\s+to)\s+(.+)",
            re.IGNORECASE,
        ),
        "user_pref",
    ),
    # Identity statements: "My name is ...", "I am a ...", "I work at ..."
    (
        re.compile(
            r"(?:My\s+name\s+is|I\s+am|I'?m|I\s+work\s+(?:at|for|on)|I\s+use|I\s+live\s+in)\s+(.+)",
            re.IGNORECASE,
        ),
        "user_pref",
    ),
    # Factual declarations: "The project uses ...", "We deploy via ..."
    (
        re.compile(
            r"(?:The\s+(?:project|team|codebase|app|server)\s+(?:uses|uses|runs|deploys|is)|We\s+(?:use|deploy|run|build))\s+(.+)",
            re.IGNORECASE,
        ),
        "project",
    ),
]

# Minimum fact length to avoid extracting trivial fragments
_MIN_FACT_LEN = 10
# Maximum facts to extract per session end (prevent flooding)
_MAX_EXTRACT_PER_SESSION = 20


def _extract_facts_from_messages(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Heuristically extract (content, category) facts from conversation messages.

    Scans user messages for patterns like "remember that ...", "I prefer ...",
    and returns candidate facts. No LLM call — pure regex heuristics.
    """
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role != "user":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue

        # Try each pattern against the full message
        for pattern, category in _AUTO_EXTRACT_PATTERNS:
            match = pattern.search(content)
            if not match:
                continue
            fact_text = match.group(1).strip().rstrip(".")
            # Clean up: remove trailing punctuation, collapse whitespace
            fact_text = re.sub(r"\s+", " ", fact_text).strip()
            if len(fact_text) < _MIN_FACT_LEN:
                continue
            # Deduplicate case-insensitively
            key = fact_text.lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append((fact_text, category))

            if len(candidates) >= _MAX_EXTRACT_PER_SESSION:
                return candidates

    return candidates


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class HolographicMemoryProvider(MemoryProvider):
    """Holographic memory with structured facts, entity resolution, and HRR retrieval."""

    def __init__(self, config: dict | None = None):
        self._config = config or {}
        self._store: MemoryStore | None = None
        self._retriever: FactRetriever | None = None
        self._min_trust = float(self._config.get("min_trust_threshold", 0.3))
        self._session_id = ""
        self._last_prefetch_count = 0

    @property
    def name(self) -> str:
        return "holographic"

    def is_available(self) -> bool:
        return True  # SQLite always available; numpy is optional

    def unavailable_reason(self) -> str:
        return ""  # always available

    def initialize(self, session_id: str, **kwargs) -> None:
        from kyourai.constants import get_kyourai_home
        kyourai_home = str(get_kyourai_home())
        default_db = kyourai_home + "/memory_store.db"
        db_path = self._config.get("db_path", default_db)
        if isinstance(db_path, str):
            db_path = db_path.replace("$KYOURAI_HOME", kyourai_home)
            db_path = db_path.replace("${KYOURAI_HOME}", kyourai_home)
        default_trust = float(self._config.get("default_trust", 0.5))
        hrr_dim = int(self._config.get("hrr_dim", 1024))
        hrr_weight = float(self._config.get("hrr_weight", 0.3))
        temporal_decay = int(self._config.get("temporal_decay_half_life", 0))

        self._store = MemoryStore(db_path=db_path, default_trust=default_trust, hrr_dim=hrr_dim)
        self._retriever = FactRetriever(
            store=self._store,
            temporal_decay_half_life=temporal_decay,
            hrr_weight=hrr_weight,
            hrr_dim=hrr_dim,
        )
        self._session_id = session_id

    def system_prompt_block(self) -> str:
        if not self._store:
            return ""
        try:
            total = self._store._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        except Exception:
            total = 0
        if total == 0:
            return (
                "# Holographic Memory\n"
                "Active. Empty fact store — proactively add facts the user would expect you to remember.\n"
                "Use fact_store(action='add') to store durable structured facts about people, projects, tools.\n"
                "Use fact_feedback to rate facts after using them (trains trust scores)."
            )
        return (
            f"# Holographic Memory\n"
            f"Active. {total} facts stored with entity resolution and HRR compositional retrieval.\n"
            f"Use fact_store to search, probe entities, reason across entities, or add facts.\n"
            f"Use fact_feedback to rate facts after using them (trains trust scores)."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._retriever or not query:
            self._last_prefetch_count = 0
            return ""
        try:
            results = self._retriever.search(query, min_trust=self._min_trust, limit=5)
            self._last_prefetch_count = len(results)
            if not results:
                return ""
            lines = []
            for r in results:
                trust = r.get("trust_score", r.get("trust", 0))
                lines.append(f"- [{trust:.1f}] {r.get('content', '')}")
            return "## Holographic Memory\n" + "\n".join(lines)
        except Exception as e:
            logger.debug("Holographic prefetch failed: %s", e)
            self._last_prefetch_count = 0
            return ""

    def recall_status(self) -> RecallStatus | None:
        if self._last_prefetch_count == 0:
            return None
        return RecallStatus(provider_label="holographic", count=self._last_prefetch_count, glyph="🔮")

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", **kwargs) -> None:
        # Holographic memory stores explicit facts via tools, not auto-sync.
        # The on_session_end hook handles auto-extraction if configured.
        pass

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [FACT_STORE_SCHEMA, FACT_FEEDBACK_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        if tool_name == "fact_store":
            return self._handle_fact_store(args)
        if tool_name == "fact_feedback":
            return self._handle_fact_feedback(args)
        return _tool_error(f"Unknown tool: {tool_name}")

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """Auto-extract facts from conversation at session end.

        Uses heuristic pattern matching (no LLM call) to find statements like
        "remember that ...", "I prefer ...", "My name is ...". Only runs when
        config auto_extract is True. Extracted facts are deduplicated against
        existing store content (add_fact returns existing ID on duplicate).
        """
        if not self._config.get("auto_extract", False):
            return
        if not self._store or not messages:
            return

        candidates = _extract_facts_from_messages(messages)
        if not candidates:
            return

        added = 0
        for content, category in candidates:
            try:
                fact_id = self._store.add_fact(content, category=category)
                # add_fact returns existing fact_id on duplicate content — only
                # count genuinely new facts
                if fact_id:
                    added += 1
            except Exception as e:
                logger.debug("Auto-extract add_fact failed for '%s': %s", content[:50], e)

        if added:
            logger.info("Auto-extracted %d facts at session end", added)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Mirror built-in memory writes as facts."""
        if action == "add" and self._store and content:
            try:
                category = "user_pref" if target == "user" else "general"
                self._store.add_fact(content, category=category)
            except Exception as e:
                logger.debug("Holographic memory_write mirror failed: %s", e)

    def shutdown(self) -> None:
        if self._store is not None:
            try:
                self._store.close()
            except Exception as e:
                logger.debug("Holographic shutdown close() failed: %s", e)
        self._store = None
        self._retriever = None

    # -- Tool handlers -------------------------------------------------------

    def _handle_fact_store(self, args: dict) -> str:
        try:
            action = args["action"]
            store = self._store
            retriever = self._retriever
            if store is None or retriever is None:
                return _tool_error("Holographic memory not initialized")

            if action == "add":
                fact_id = store.add_fact(
                    args["content"],
                    category=args.get("category", "general"),
                    tags=args.get("tags", ""),
                )
                return json.dumps({"fact_id": fact_id, "status": "added"})

            if action == "search":
                results = retriever.search(
                    args["query"],
                    category=args.get("category"),
                    min_trust=float(args.get("min_trust", self._min_trust)),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results)}, ensure_ascii=False)

            if action == "probe":
                results = retriever.probe(
                    args["entity"],
                    category=args.get("category"),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results)}, ensure_ascii=False)

            if action == "related":
                results = retriever.related(
                    args["entity"],
                    category=args.get("category"),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results)}, ensure_ascii=False)

            if action == "reason":
                entities = args.get("entities", [])
                if not entities:
                    return _tool_error("reason requires 'entities' list")
                results = retriever.reason(
                    entities,
                    category=args.get("category"),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results)}, ensure_ascii=False)

            if action == "contradict":
                results = retriever.contradict(
                    category=args.get("category"),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"results": results, "count": len(results)}, ensure_ascii=False)

            if action == "update":
                updated = store.update_fact(
                    int(args["fact_id"]),
                    content=args.get("content"),
                    trust_delta=float(args["trust_delta"]) if "trust_delta" in args else None,
                    tags=args.get("tags"),
                    category=args.get("category"),
                )
                return json.dumps({"updated": updated})

            if action == "remove":
                removed = store.remove_fact(int(args["fact_id"]))
                return json.dumps({"removed": removed})

            if action == "list":
                facts = store.list_facts(
                    category=args.get("category"),
                    min_trust=float(args.get("min_trust", 0.0)),
                    limit=int(args.get("limit", 10)),
                )
                return json.dumps({"facts": facts, "count": len(facts)}, ensure_ascii=False)

            return _tool_error(f"Unknown action: {action}")

        except KeyError as exc:
            return _tool_error(f"Missing required argument: {exc}")
        except Exception as exc:
            return _tool_error(str(exc))

    def _handle_fact_feedback(self, args: dict) -> str:
        try:
            fact_id = int(args["fact_id"])
            helpful = args["action"] == "helpful"
            result = self._store.record_feedback(fact_id, helpful=helpful)
            return json.dumps(result)
        except KeyError as exc:
            return _tool_error(f"Missing required argument: {exc}")
        except Exception as exc:
            return _tool_error(str(exc))
