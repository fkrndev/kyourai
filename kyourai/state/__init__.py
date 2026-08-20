"""Session state — SQLite store with FTS5 search.

Persists conversation sessions and messages, enabling:
  - Session history browsing (``kyourai sessions list``)
  - Full-text search across past conversations (``kyourai sessions search``)
  - Usage insights and analytics (``kyourai insights``)
  - Session rewind for the agent (message_history reconstruction)

Schema is intentionally simpler than Hermes' (no gateway routing, no
compression locks, no billing) — kyourai is a single-user agent, not a
multi-platform gateway. The schema is versioned for forward migration.
"""

from kyourai.state.db import SessionDB
from kyourai.state.insights import InsightsEngine

__all__ = ["SessionDB", "InsightsEngine"]
