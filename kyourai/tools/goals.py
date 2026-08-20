"""Goal management — track and manage agent goals.

Inspired by OpenClaw's goal-tools. Provides:
  - Goal: a tracked objective with status, progress, and sub-goals
  - GoalTracker: manage goals for a session
  - Goal status lifecycle: active → completed/abandoned/deferred
  - Progress tracking with percentage
  - Sub-goal hierarchy
  - SQLite persistence

Usage:
    from kyourai.tools.goals import GoalTracker, Goal, GoalStatus

    tracker = GoalTracker(session_id="my-session")
    goal = tracker.create_goal("Refactor authentication module", priority="high")
    tracker.update_progress(goal.goal_id, progress=50)
    tracker.complete_goal(goal.goal_id, outcome="Refactored to use JWT")
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

from kyourai.constants import get_kyourai_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class GoalStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    DEFERRED = "deferred"
    BLOCKED = "blocked"


class GoalPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Goal:
    """A tracked agent goal."""
    goal_id: str
    session_id: str
    title: str
    description: str = ""
    status: GoalStatus = GoalStatus.ACTIVE
    priority: GoalPriority = GoalPriority.MEDIUM
    progress: int = 0  # 0-100
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0
    parent_goal_id: str = ""
    sub_goals: list[str] = field(default_factory=list)
    outcome: str = ""
    blockers: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    due_at: float = 0.0  # 0 = no deadline

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            GoalStatus.COMPLETED, GoalStatus.ABANDONED,
        )

    @property
    def is_active(self) -> bool:
        return self.status in (
            GoalStatus.ACTIVE, GoalStatus.BLOCKED, GoalStatus.DEFERRED,
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["priority"] = self.priority.value
        return d


# ---------------------------------------------------------------------------
# Goal tracker
# ---------------------------------------------------------------------------


class GoalTracker:
    """Track and manage goals for a session.

    Goals are persisted in SQLite for durability across sessions.
    """

    def __init__(self, session_id: str = "") -> None:
        self.session_id = session_id
        self._goals: dict[str, Goal] = {}
        self._db_available = self._init_db()

    def _init_db(self) -> bool:
        try:
            db_path = get_kyourai_home() / "goals.db"
            conn = sqlite3.connect(str(db_path))
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS goals (
                    goal_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    status TEXT DEFAULT 'active',
                    priority TEXT DEFAULT 'medium',
                    progress INTEGER DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL DEFAULT 0,
                    parent_goal_id TEXT DEFAULT '',
                    sub_goals_json TEXT DEFAULT '[]',
                    outcome TEXT DEFAULT '',
                    blockers_json TEXT DEFAULT '[]',
                    tags_json TEXT DEFAULT '[]',
                    due_at REAL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_goals_session ON goals(session_id);
                CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
            """)
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.warning("Goals DB init failed: %s", e)
            return False

    def create_goal(
        self,
        title: str,
        description: str = "",
        priority: GoalPriority | str = GoalPriority.MEDIUM,
        parent_goal_id: str = "",
        tags: list[str] | None = None,
        due_at: float = 0.0,
    ) -> Goal:
        """Create a new goal."""
        goal_id = f"goal-{uuid.uuid4().hex[:12]}"
        now = time.time()

        if isinstance(priority, str):
            priority = GoalPriority(priority.lower())

        goal = Goal(
            goal_id=goal_id,
            session_id=self.session_id,
            title=title,
            description=description,
            priority=priority,
            created_at=now,
            updated_at=now,
            parent_goal_id=parent_goal_id,
            tags=tags or [],
            due_at=due_at,
        )

        self._goals[goal_id] = goal
        self._persist(goal)

        # Add to parent's sub-goals
        if parent_goal_id and parent_goal_id in self._goals:
            parent = self._goals[parent_goal_id]
            parent.sub_goals.append(goal_id)
            self._persist(parent)

        logger.info("Created goal %s: %s", goal_id, title)
        return goal

    def update_progress(self, goal_id: str, progress: int) -> Goal | None:
        """Update goal progress (0-100)."""
        goal = self._goals.get(goal_id)
        if not goal or goal.is_terminal:
            return None

        goal.progress = max(0, min(100, progress))
        goal.updated_at = time.time()

        # Auto-complete at 100%
        if goal.progress >= 100:
            goal.status = GoalStatus.COMPLETED
            goal.completed_at = time.time()

        self._persist(goal)
        return goal

    def complete_goal(self, goal_id: str, outcome: str = "") -> Goal | None:
        """Mark a goal as completed."""
        goal = self._goals.get(goal_id)
        if not goal or goal.is_terminal:
            return None

        goal.status = GoalStatus.COMPLETED
        goal.progress = 100
        goal.outcome = outcome
        goal.completed_at = time.time()
        goal.updated_at = time.time()
        self._persist(goal)
        return goal

    def abandon_goal(self, goal_id: str, reason: str = "") -> Goal | None:
        """Mark a goal as abandoned."""
        goal = self._goals.get(goal_id)
        if not goal or goal.is_terminal:
            return None

        goal.status = GoalStatus.ABANDONED
        goal.outcome = reason
        goal.completed_at = time.time()
        goal.updated_at = time.time()
        self._persist(goal)
        return goal

    def defer_goal(self, goal_id: str, reason: str = "") -> Goal | None:
        """Defer a goal."""
        goal = self._goals.get(goal_id)
        if not goal or goal.is_terminal:
            return None

        goal.status = GoalStatus.DEFERRED
        goal.outcome = reason
        goal.updated_at = time.time()
        self._persist(goal)
        return goal

    def block_goal(self, goal_id: str, blocker: str = "") -> Goal | None:
        """Mark a goal as blocked."""
        goal = self._goals.get(goal_id)
        if not goal or goal.is_terminal:
            return None

        goal.status = GoalStatus.BLOCKED
        if blocker:
            goal.blockers.append(blocker)
        goal.updated_at = time.time()
        self._persist(goal)
        return goal

    def unblock_goal(self, goal_id: str) -> Goal | None:
        """Unblock a goal."""
        goal = self._goals.get(goal_id)
        if not goal or goal.status != GoalStatus.BLOCKED:
            return None

        goal.status = GoalStatus.ACTIVE
        goal.blockers.clear()
        goal.updated_at = time.time()
        self._persist(goal)
        return goal

    def add_tag(self, goal_id: str, tag: str) -> Goal | None:
        """Add a tag to a goal."""
        goal = self._goals.get(goal_id)
        if not goal:
            return None
        if tag not in goal.tags:
            goal.tags.append(tag)
            goal.updated_at = time.time()
            self._persist(goal)
        return goal

    def get(self, goal_id: str) -> Goal | None:
        return self._goals.get(goal_id)

    def list_active(self) -> list[Goal]:
        """List active goals."""
        return [g for g in self._goals.values() if g.is_active]

    def list_completed(self, limit: int = 20) -> list[Goal]:
        """List completed goals."""
        goals = [g for g in self._goals.values() if g.is_terminal]
        goals.sort(key=lambda g: g.completed_at, reverse=True)
        return goals[:limit]

    def list_all(self) -> list[Goal]:
        """List all goals."""
        return list(self._goals.values())

    def list_by_priority(self, priority: GoalPriority) -> list[Goal]:
        """List goals by priority."""
        return [g for g in self._goals.values()
                if g.priority == priority and g.is_active]

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of all goals."""
        all_goals = list(self._goals.values())
        active = [g for g in all_goals if g.is_active]
        completed = [g for g in all_goals if g.status == GoalStatus.COMPLETED]
        abandoned = [g for g in all_goals if g.status == GoalStatus.ABANDONED]
        blocked = [g for g in all_goals if g.status == GoalStatus.BLOCKED]

        avg_progress = (
            sum(g.progress for g in active) / len(active) if active else 0
        )

        return {
            "total": len(all_goals),
            "active": len(active),
            "completed": len(completed),
            "abandoned": len(abandoned),
            "blocked": len(blocked),
            "avg_progress": round(avg_progress, 1),
            "high_priority": len([g for g in active if g.priority == GoalPriority.HIGH]),
            "critical": len([g for g in active if g.priority == GoalPriority.CRITICAL]),
        }

    def get_tree(self, goal_id: str) -> dict[str, Any]:
        """Get the goal tree (goal + sub-goals)."""
        goal = self._goals.get(goal_id)
        if not goal:
            return {}

        def build_node(g: Goal) -> dict[str, Any]:
            children = [self._goals[sid] for sid in g.sub_goals if sid in self._goals]
            return {
                "goal_id": g.goal_id,
                "title": g.title,
                "status": g.status.value,
                "progress": g.progress,
                "priority": g.priority.value,
                "children": [build_node(c) for c in children],
            }

        return build_node(goal)

    def cleanup_old(self, max_age_days: int = 30) -> int:
        """Remove terminal goals older than max_age_days."""
        cutoff = time.time() - (max_age_days * 86400)
        to_remove = [
            gid for gid, g in self._goals.items()
            if g.is_terminal and g.completed_at < cutoff
        ]
        for gid in to_remove:
            del self._goals[gid]
        return len(to_remove)

    def _persist(self, goal: Goal) -> None:
        if not self._db_available:
            return
        try:
            db_path = get_kyourai_home() / "goals.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                INSERT OR REPLACE INTO goals
                (goal_id, session_id, title, description, status, priority,
                 progress, created_at, updated_at, completed_at, parent_goal_id,
                 sub_goals_json, outcome, blockers_json, tags_json, due_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                goal.goal_id, goal.session_id, goal.title, goal.description,
                goal.status.value, goal.priority.value, goal.progress,
                goal.created_at, goal.updated_at, goal.completed_at,
                goal.parent_goal_id, json.dumps(goal.sub_goals),
                goal.outcome, json.dumps(goal.blockers),
                json.dumps(goal.tags), goal.due_at,
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("Failed to persist goal: %s", e)
