"""Constants and path resolution for Kyourai.

Mirrors Hermes' hermes_constants.py but trimmed to what we actually need.
All profile-scoped storage resolves under KYOURAI_HOME (env override) or
~/.kyourai by default.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HOME = Path.home() / ".kyourai"


def get_kyourai_home() -> Path:
    """Return the active KYOURAI_HOME directory.

    Resolved dynamically (not cached at import) so profile switches
    mid-process are respected, matching Hermes' get_hermes_home() behavior.
    """
    env = os.environ.get("KYOURAI_HOME")
    if env:
        return Path(env).expanduser()
    return DEFAULT_HOME


def get_memory_dir() -> Path:
    """Directory for file-based memory (MEMORY.md, USER.md)."""
    return get_kyourai_home() / "memories"


def get_state_db_path() -> Path:
    """SQLite session database path."""
    return get_kyourai_home() / "state.db"


def get_holographic_db_path() -> Path:
    """Holographic memory SQLite database path."""
    return get_kyourai_home() / "memory_store.db"


def get_skills_dir() -> Path:
    """Directory for learned/created skills."""
    return get_kyourai_home() / "skills"


def get_curator_state_path() -> Path:
    """Curator scheduler state file."""
    return get_kyourai_home() / "skills" / ".curator_state"


def ensure_home() -> Path:
    """Create the KYOURAI_HOME directory tree if missing. Returns the root."""
    home = get_kyourai_home()
    for sub in ("memories", "skills", "plugins"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    return home
