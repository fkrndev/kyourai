"""Holographic HRR memory provider — self-hosted vector memory via SQLite + FTS5.

Exposes:
  - hrr: HRR vector algebra (bind/unbind/bundle/encode/similarity)
  - MemoryStore: SQLite-backed fact store with entity resolution + trust scoring
  - FactRetriever: Hybrid FTS5/Jaccard/HRR retrieval + compositional queries
  - HolographicMemoryProvider: MemoryProvider implementation for the manager
"""

from kyourai.memory.holographic import hrr
from kyourai.memory.holographic.store import MemoryStore
from kyourai.memory.holographic.retrieval import FactRetriever
from kyourai.memory.holographic.provider import HolographicMemoryProvider

__all__ = ["hrr", "MemoryStore", "FactRetriever", "HolographicMemoryProvider"]
