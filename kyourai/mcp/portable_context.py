"""Portable context format — standardized memory export/import.

The Kyourai Portable Context (KPC) format is a JSON-based bundle that any
agent can produce or consume. It enables memory portability between
different AI coding agents (Kyourai, Claude, ChatGPT, Cursor, etc.).

Format version: 1.0

Structure:
  {
    "format": "kyourai-portable-context",
    "version": "1.0",
    "exported_at": "2024-01-01T00:00:00Z",
    "exported_by": "kyourai",
    "exported_by_version": "0.1.0",
    "profile": {
      "display_name": "Andi",
      "agent_identity": "coder"
    },
    "builtin_memory": {
      "memory_entries": ["fact 1", "fact 2"],
      "user_entries": ["user pref 1"]
    },
    "holographic_facts": [
      {
        "content": "...",
        "category": "user_pref",
        "tags": "...",
        "trust_score": 0.5,
        "helpful_count": 0
      }
    ],
    "metadata": {
      "total_entries": 5,
      "total_facts": 10,
      "checksum": "sha256:..."
    }
  }

Privacy: the export is user-curated. The user chooses what to include.
No automatic export of all memory — explicit selection only.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kyourai.constants import get_kyourai_home, get_memory_dir
from kyourai.utils import atomic_write_text

logger = logging.getLogger(__name__)

FORMAT_NAME = "kyourai-portable-context"
FORMAT_VERSION = "1.0"


@dataclass
class PortableContext:
    """A portable context bundle ready for export/import."""
    format: str = FORMAT_NAME
    version: str = FORMAT_VERSION
    exported_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    exported_by: str = "kyourai"
    exported_by_version: str = "0.1.0"
    profile: dict[str, Any] = field(default_factory=dict)
    builtin_memory: dict[str, list[str]] = field(default_factory=lambda: {"memory_entries": [], "user_entries": []})
    holographic_facts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # Populate all metadata fields FIRST, then compute checksum over
        # the complete data (minus the checksum field itself).
        total_entries = len(self.builtin_memory.get("memory_entries", [])) + len(self.builtin_memory.get("user_entries", []))
        total_facts = len(self.holographic_facts)
        metadata = dict(self.metadata)
        metadata["total_entries"] = total_entries
        metadata["total_facts"] = total_facts
        # Ensure no stale checksum from a previous round-trip
        metadata.pop("checksum", None)

        data = {
            "format": self.format,
            "version": self.version,
            "exported_at": self.exported_at,
            "exported_by": self.exported_by,
            "exported_by_version": self.exported_by_version,
            "profile": self.profile,
            "builtin_memory": self.builtin_memory,
            "holographic_facts": self.holographic_facts,
            "metadata": metadata,
        }
        # Compute checksum over the complete data (without the checksum field)
        content = json.dumps(data, sort_keys=True, ensure_ascii=False)
        checksum = hashlib.sha256(content.encode()).hexdigest()
        data["metadata"]["checksum"] = f"sha256:{checksum}"
        return data

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PortableContext":
        """Parse a portable context dict. Validates format and checksum."""
        if data.get("format") != FORMAT_NAME:
            raise ValueError(f"Unknown format: {data.get('format')}. Expected {FORMAT_NAME}.")
        if data.get("version") != FORMAT_VERSION:
            raise ValueError(f"Unsupported version: {data.get('version')}. Expected {FORMAT_VERSION}.")

        # Verify checksum (metadata.checksum is over the data WITHOUT the checksum field)
        metadata = data.get("metadata", {})
        expected_checksum = metadata.get("checksum", "")
        if expected_checksum.startswith("sha256:"):
            verify_data = dict(data)
            verify_metadata = dict(metadata)
            stored_checksum = verify_metadata.pop("checksum")
            verify_data["metadata"] = verify_metadata
            content = json.dumps(verify_data, sort_keys=True, ensure_ascii=False)
            actual_checksum = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
            if actual_checksum != stored_checksum:
                raise ValueError(f"Checksum mismatch: expected {stored_checksum}, got {actual_checksum}")

        return cls(
            format=data["format"],
            version=data["version"],
            exported_at=data.get("exported_at", ""),
            exported_by=data.get("exported_by", ""),
            exported_by_version=data.get("exported_by_version", ""),
            profile=data.get("profile", {}),
            builtin_memory=data.get("builtin_memory", {"memory_entries": [], "user_entries": []}),
            holographic_facts=data.get("holographic_facts", []),
            metadata=metadata,
        )

    @classmethod
    def from_json(cls, json_str: str) -> "PortableContext":
        return cls.from_dict(json.loads(json_str))


# ---------------------------------------------------------------------------
# Export — build a PortableContext from the current memory state
# ---------------------------------------------------------------------------

def export_memory(
    *,
    include_builtin: bool = True,
    include_holographic: bool = True,
    profile: dict[str, Any] | None = None,
    builtin_provider=None,
    holographic_store=None,
) -> PortableContext:
    """Export memory into a PortableContext bundle.

    Args:
      include_builtin: include MEMORY.md and USER.md entries
      include_holographic: include facts from the holographic store
      profile: optional profile metadata (display_name, agent_identity)
      builtin_provider: BuiltinMemoryProvider instance (for builtin memory)
      holographic_store: MemoryStore instance (for holographic facts)

    The caller should have already initialized the providers/stores.
    """
    ctx = PortableContext(profile=profile or {})

    if include_builtin and builtin_provider is not None:
        store = builtin_provider.store
        ctx.builtin_memory = {
            "memory_entries": list(store.memory_entries),
            "user_entries": list(store.user_entries),
        }

    if include_holographic and holographic_store is not None:
        try:
            rows = holographic_store._conn.execute(
                """SELECT content, category, tags, trust_score, helpful_count
                   FROM facts ORDER BY trust_score DESC"""
            ).fetchall()
            ctx.holographic_facts = [
                {
                    "content": row["content"],
                    "category": row["category"],
                    "tags": row["tags"],
                    "trust_score": row["trust_score"],
                    "helpful_count": row["helpful_count"],
                }
                for row in rows
            ]
        except Exception as e:
            logger.warning("Failed to export holographic facts: %s", e)

    return ctx


def export_to_file(
    path: str | Path,
    *,
    include_builtin: bool = True,
    include_holographic: bool = True,
    profile: dict[str, Any] | None = None,
    builtin_provider=None,
    holographic_store=None,
) -> Path:
    """Export memory to a .kpc.json file. Returns the path."""
    ctx = export_memory(
        include_builtin=include_builtin,
        include_holographic=include_holographic,
        profile=profile,
        builtin_provider=builtin_provider,
        holographic_store=holographic_store,
    )
    path = Path(path)
    atomic_write_text(path, ctx.to_json())
    logger.info("Exported portable context to %s (%d entries, %d facts)",
                path, len(ctx.builtin_memory.get("memory_entries", [])) + len(ctx.builtin_memory.get("user_entries", [])),
                len(ctx.holographic_facts))
    return path


# ---------------------------------------------------------------------------
# Import — merge a PortableContext into the current memory state
# ---------------------------------------------------------------------------

def import_memory(
    ctx: PortableContext,
    *,
    import_builtin: bool = True,
    import_holographic: bool = True,
    merge_strategy: str = "skip_duplicates",  # or "overwrite" or "append"
    builtin_provider=None,
    holographic_store=None,
) -> dict[str, int]:
    """Import a PortableContext into the current memory state.

    Args:
      ctx: the portable context to import
      import_builtin: import MEMORY.md and USER.md entries
      import_holographic: import holographic facts
      merge_strategy: how to handle existing entries
        - "skip_duplicates": skip entries that already exist (default)
        - "overwrite": replace existing entries with imported ones
        - "append": always add, even if duplicates exist
      builtin_provider: BuiltinMemoryProvider instance
      holographic_store: MemoryStore instance

    Returns a summary dict with counts of imported/skipped items.
    """
    summary = {"builtin_imported": 0, "builtin_skipped": 0, "facts_imported": 0, "facts_skipped": 0}

    if import_builtin and builtin_provider is not None:
        store = builtin_provider.store
        for entry in ctx.builtin_memory.get("memory_entries", []):
            if merge_strategy == "append" or entry not in store.memory_entries:
                result = store.add("memory", entry)
                if result.get("success"):
                    summary["builtin_imported"] += 1
                else:
                    summary["builtin_skipped"] += 1
            else:
                summary["builtin_skipped"] += 1

        for entry in ctx.builtin_memory.get("user_entries", []):
            if merge_strategy == "append" or entry not in store.user_entries:
                result = store.add("user", entry)
                if result.get("success"):
                    summary["builtin_imported"] += 1
                else:
                    summary["builtin_skipped"] += 1
            else:
                summary["builtin_skipped"] += 1

    if import_holographic and holographic_store is not None:
        for fact in ctx.holographic_facts:
            try:
                # Check if fact already exists (content is UNIQUE)
                existing = holographic_store._conn.execute(
                    "SELECT fact_id FROM facts WHERE content = ?", (fact["content"],)
                ).fetchone()
                if existing and merge_strategy == "skip_duplicates":
                    summary["facts_skipped"] += 1
                    continue
                if existing and merge_strategy == "overwrite":
                    holographic_store.update_fact(
                        existing["fact_id"],
                        content=fact["content"],
                        tags=fact.get("tags"),
                        category=fact.get("category"),
                    )
                    summary["facts_imported"] += 1
                    continue
                # Add new fact
                fact_id = holographic_store.add_fact(
                    fact["content"],
                    category=fact.get("category", "general"),
                    tags=fact.get("tags", ""),
                )
                # Apply trust score and helpful count if provided
                if fact.get("trust_score", 0.5) != 0.5 or fact.get("helpful_count", 0) > 0:
                    holographic_store._conn.execute(
                        "UPDATE facts SET trust_score = ?, helpful_count = ? WHERE fact_id = ?",
                        (fact.get("trust_score", 0.5), fact.get("helpful_count", 0), fact_id),
                    )
                    holographic_store._conn.commit()
                summary["facts_imported"] += 1
            except Exception as e:
                logger.warning("Failed to import fact: %s", e)
                summary["facts_skipped"] += 1

    logger.info("Imported portable context: %d imported, %d skipped",
                summary["builtin_imported"] + summary["facts_imported"],
                summary["builtin_skipped"] + summary["facts_skipped"])
    return summary


def import_from_file(
    path: str | Path,
    *,
    import_builtin: bool = True,
    import_holographic: bool = True,
    merge_strategy: str = "skip_duplicates",
    builtin_provider=None,
    holographic_store=None,
) -> dict[str, int]:
    """Import a .kpc.json file. Returns import summary."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Portable context file not found: {path}")
    json_str = path.read_text(encoding="utf-8")
    ctx = PortableContext.from_json(json_str)
    return import_memory(
        ctx,
        import_builtin=import_builtin,
        import_holographic=import_holographic,
        merge_strategy=merge_strategy,
        builtin_provider=builtin_provider,
        holographic_store=holographic_store,
    )
