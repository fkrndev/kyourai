"""Team-native layer — shared + private memory with lightweight RBAC.

Kyourai's key differentiator: designed for small teams (2-20 people), not
just single operators. Each team member has:
  - Private memory: personal notes, preferences (invisible to others)
  - Shared memory: team-wide facts, project knowledge, conventions
  - Per-skill permissions: who can create/modify/archive skills

RBAC is intentionally lightweight — three roles (member, editor, admin),
no complex policy engine. Teams that need more should use their existing
identity provider and map to these roles.

Storage layout:
  $KYOURAI_HOME/
    teams/
      <team_id>/
        shared/          # shared MEMORY.md, USER.md, memory_store.db
        members/
          <user_id>/     # private MEMORY.md, USER.md, memory_store.db
        team.json        # team metadata + member roster + roles
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from kyourai.constants import get_kyourai_home
from kyourai.utils import atomic_json_write, atomic_write_text

logger = logging.getLogger(__name__)


class Role(str, Enum):
    """Team roles, ordered by privilege level."""
    MEMBER = "member"    # read shared, write own private, use tools
    EDITOR = "editor"    # + write shared memory, create/modify skills
    ADMIN = "admin"      # + manage members, archive skills, delete facts

    @property
    def level(self) -> int:
        return {"member": 0, "editor": 1, "admin": 2}[self.value]


@dataclass
class TeamMember:
    """A team member with identity and role."""
    user_id: str           # stable platform identifier
    display_name: str
    role: Role = Role.MEMBER
    joined_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_active_at: str | None = None


@dataclass
class Team:
    """A team with shared and private memory spaces."""
    team_id: str
    team_name: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    members: dict[str, TeamMember] = field(default_factory=dict)

    def get_member(self, user_id: str) -> TeamMember | None:
        return self.members.get(user_id)

    def get_role(self, user_id: str) -> Role | None:
        member = self.members.get(user_id)
        return member.role if member else None

    def has_role(self, user_id: str, min_role: Role) -> bool:
        role = self.get_role(user_id)
        if role is None:
            return False
        return role.level >= min_role.level

    def to_dict(self) -> dict[str, Any]:
        return {
            "team_id": self.team_id,
            "team_name": self.team_name,
            "created_at": self.created_at,
            "members": {
                uid: {
                    "user_id": m.user_id,
                    "display_name": m.display_name,
                    "role": m.role.value,
                    "joined_at": m.joined_at,
                    "last_active_at": m.last_active_at,
                }
                for uid, m in self.members.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Team":
        members = {}
        for uid, m_data in (data.get("members") or {}).items():
            members[uid] = TeamMember(
                user_id=m_data["user_id"],
                display_name=m_data["display_name"],
                role=Role(m_data.get("role", "member")),
                joined_at=m_data.get("joined_at", ""),
                last_active_at=m_data.get("last_active_at"),
            )
        return cls(
            team_id=data["team_id"],
            team_name=data["team_name"],
            created_at=data.get("created_at", ""),
            members=members,
        )


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_teams_dir() -> Path:
    return get_kyourai_home() / "teams"


def get_team_dir(team_id: str) -> Path:
    return get_teams_dir() / team_id


def get_shared_dir(team_id: str) -> Path:
    return get_team_dir(team_id) / "shared"


def get_member_dir(team_id: str, user_id: str) -> Path:
    return get_team_dir(team_id) / "members" / user_id


def get_team_config_path(team_id: str) -> Path:
    return get_team_dir(team_id) / "team.json"


# ---------------------------------------------------------------------------
# Team manager
# ---------------------------------------------------------------------------

class TeamManager:
    """Manages team lifecycle, membership, and memory space isolation.

    Thread-safe. All team config writes are atomic. Memory spaces are
    isolated by directory structure — shared memory lives in the team's
    shared/ dir, private memory in each member's dir.

    The agent's MemoryManager is wired to the active user's private + shared
    memory by swapping KYOURAI_HOME context per request (see TeamMemoryRouter).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def create_team(self, team_name: str, creator_user_id: str, creator_display_name: str) -> Team:
        """Create a new team. The creator becomes an admin."""
        with self._lock:
            team_id = secrets.token_urlsafe(8)
            while get_team_dir(team_id).exists():
                team_id = secrets.token_urlsafe(8)

            team = Team(
                team_id=team_id,
                team_name=team_name,
                members={
                    creator_user_id: TeamMember(
                        user_id=creator_user_id,
                        display_name=creator_display_name,
                        role=Role.ADMIN,
                    )
                },
            )
            self._save_team(team)
            self._ensure_dirs(team_id, creator_user_id)
            logger.info("Created team '%s' (id=%s) with admin %s", team_name, team_id, creator_user_id)
            return team

    def add_member(
        self,
        team_id: str,
        user_id: str,
        display_name: str,
        role: Role = Role.MEMBER,
        *,
        added_by: str = "",
    ) -> TeamMember:
        """Add a member to a team. Requires admin privileges."""
        team = self.load_team(team_id)
        if team is None:
            raise ValueError(f"Team {team_id} not found")
        if not team.has_role(added_by, Role.ADMIN):
            raise PermissionError(f"User {added_by} cannot add members (requires admin)")

        with self._lock:
            member = TeamMember(user_id=user_id, display_name=display_name, role=role)
            team.members[user_id] = member
            self._save_team(team)
            self._ensure_member_dir(team_id, user_id)
            logger.info("Added member %s to team %s as %s", user_id, team_id, role.value)
            return member

    def remove_member(self, team_id: str, user_id: str, *, removed_by: str = "") -> bool:
        """Remove a member from a team. Requires admin privileges."""
        team = self.load_team(team_id)
        if team is None:
            return False
        if not team.has_role(removed_by, Role.ADMIN):
            raise PermissionError(f"User {removed_by} cannot remove members (requires admin)")
        if user_id not in team.members:
            return False

        with self._lock:
            del team.members[user_id]
            self._save_team(team)
            # Note: we don't delete the member's private memory dir —
            # the admin may want to recover it. Manual cleanup required.
            logger.info("Removed member %s from team %s", user_id, team_id)
            return True

    def set_role(self, team_id: str, user_id: str, role: Role, *, set_by: str = "") -> bool:
        """Change a member's role. Requires admin privileges."""
        team = self.load_team(team_id)
        if team is None:
            return False
        if not team.has_role(set_by, Role.ADMIN):
            raise PermissionError(f"User {set_by} cannot change roles (requires admin)")
        if user_id not in team.members:
            return False

        with self._lock:
            team.members[user_id].role = role
            self._save_team(team)
            logger.info("Set role of %s to %s in team %s", user_id, role.value, team_id)
            return True

    def load_team(self, team_id: str) -> Team | None:
        """Load a team from disk. Returns None if not found."""
        path = get_team_config_path(team_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Team.from_dict(data)
        except Exception as e:
            logger.warning("Failed to load team %s: %s", team_id, e)
            return None

    def list_teams(self) -> list[dict[str, Any]]:
        """List all teams (metadata only, no member details)."""
        teams_dir = get_teams_dir()
        if not teams_dir.exists():
            return []
        teams = []
        for team_dir in teams_dir.iterdir():
            if not team_dir.is_dir():
                continue
            config_path = team_dir / "team.json"
            if not config_path.exists():
                continue
            try:
                data = json.loads(config_path.read_text(encoding="utf-8"))
                teams.append({
                    "team_id": data["team_id"],
                    "team_name": data["team_name"],
                    "member_count": len(data.get("members", {})),
                    "created_at": data.get("created_at"),
                })
            except Exception:
                continue
        return teams

    def update_last_active(self, team_id: str, user_id: str) -> None:
        """Update a member's last_active_at timestamp."""
        with self._lock:
            team = self.load_team(team_id)
            if team is None or user_id not in team.members:
                return
            team.members[user_id].last_active_at = datetime.now(timezone.utc).isoformat()
            self._save_team(team)

    # -- Internal helpers ---------------------------------------------------

    def _save_team(self, team: Team) -> None:
        path = get_team_config_path(team.team_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, team.to_dict(), indent=2)

    def _ensure_dirs(self, team_id: str, creator_user_id: str) -> None:
        get_shared_dir(team_id).mkdir(parents=True, exist_ok=True)
        get_team_dir(team_id).mkdir(parents=True, exist_ok=True)
        self._ensure_member_dir(team_id, creator_user_id)

    def _ensure_member_dir(self, team_id: str, user_id: str) -> None:
        get_member_dir(team_id, user_id).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Team memory router — scope switching for shared vs private memory
# ---------------------------------------------------------------------------

class TeamMemoryRouter:
    """Routes memory operations to shared or private memory spaces.

    The agent's MemoryManager reads/writes through this router. Each request
    is scoped to either:
      - "private": the active user's private memory dir
      - "shared": the team's shared memory dir
      - "both": private first, then shared (for prefetch/system prompt)

    Implementation: temporarily sets KYOURAI_HOME to the appropriate directory
    so the existing memory components (which all use get_kyourai_home()) work
    without modification.
    """

    def __init__(self, team_manager: TeamManager, team_id: str, user_id: str):
        self._team_manager = team_manager
        self._team_id = team_id
        self._user_id = user_id
        self._original_home: str | None = None
        self._lock = threading.RLock()

    def _set_home(self, path: Path) -> None:
        """Temporarily override KYOURAI_HOME."""
        self._original_home = os.environ.get("KYOURAI_HOME")
        os.environ["KYOURAI_HOME"] = str(path)

    def _restore_home(self) -> None:
        """Restore the original KYOURAI_HOME."""
        if self._original_home is not None:
            os.environ["KYOURAI_HOME"] = self._original_home
        else:
            os.environ.pop("KYOURAI_HOME", None)

    def with_private(self, fn: callable) -> Any:
        """Execute fn with KYOURAI_HOME pointing to the user's private memory."""
        with self._lock:
            member_dir = get_member_dir(self._team_id, self._user_id)
            member_dir.mkdir(parents=True, exist_ok=True)
            self._set_home(member_dir)
            try:
                return fn()
            finally:
                self._restore_home()

    def with_shared(self, fn: callable) -> Any:
        """Execute fn with KYOURAI_HOME pointing to the team's shared memory."""
        with self._lock:
            shared_dir = get_shared_dir(self._team_id)
            shared_dir.mkdir(parents=True, exist_ok=True)
            self._set_home(shared_dir)
            try:
                return fn()
            finally:
                self._restore_home()

    def with_both(self, fn: callable) -> tuple[Any, Any]:
        """Execute fn in both private and shared contexts. Returns (private_result, shared_result)."""
        private_result = self.with_private(fn)
        shared_result = self.with_shared(fn)
        return private_result, shared_result

    def check_permission(self, action: str, scope: str = "shared") -> bool:
        """Check if the active user has permission for an action.

        Actions:
          - "read": any member can read shared + own private
          - "write_shared": requires editor or admin
          - "manage_team": requires admin
          - "write_private": any member can write own private
        """
        team = self._team_manager.load_team(self._team_id)
        if team is None:
            return False
        role = team.get_role(self._user_id)
        if role is None:
            return False

        if action == "read":
            return True
        if action == "write_private":
            return True
        if action == "write_shared":
            return role.level >= Role.EDITOR.level
        if action == "manage_team":
            return role.level >= Role.ADMIN.level
        return False
