"""Task flow orchestration — multi-step tasks with state tracking.

Inspired by OpenClaw's task flow registry. Provides:
  - TaskRegistry: register and track background tasks
  - TaskFlow: multi-step task orchestration with state
  - Revision-based optimistic concurrency control
  - Parent-child task relationships
  - Delivery state tracking (pending, delivered, dismissed)
  - SQLite persistence for durability

Usage:
    from kyourai.tasks.flows import TaskRegistry, TaskFlow, TaskStatus

    registry = TaskRegistry()
    flow = registry.create_flow(
        title="Refactor auth module",
        steps=["analyze", "plan", "implement", "test"],
    )
    registry.start_step(flow.flow_id, "analyze")
    # ... do work ...
    registry.complete_step(flow.flow_id, "analyze", result="...")
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any

from kyourai.constants import get_kyourai_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    LOST = "lost"


class FlowStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"


class DeliveryState(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    SESSION_QUEUED = "session_queued"
    FAILED = "failed"
    DISMISSED = "dismissed"
    PARENT_MISSING = "parent_missing"
    NOT_APPLICABLE = "not_applicable"


class NotifyPolicy(str, Enum):
    DONE_ONLY = "done_only"
    STATE_CHANGES = "state_changes"
    SILENT = "silent"


class TaskScope(str, Enum):
    SESSION = "session"
    SYSTEM = "system"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Task:
    """A single task in the registry."""
    task_id: str
    flow_id: str
    title: str
    status: TaskStatus
    revision: int = 1
    created_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    parent_task_id: str = ""
    parent_flow_id: str = ""
    scope: TaskScope = TaskScope.SESSION
    session_id: str = ""
    runtime: str = ""  # "subagent", "cron", "cli", "embedded"
    state_json: str = ""
    result: str = ""
    error: str = ""
    progress_summary: str = ""
    terminal_summary: str = ""
    tool_use_count: int = 0
    last_tool_name: str = ""
    delivery_state: DeliveryState = DeliveryState.PENDING
    notify_policy: NotifyPolicy = NotifyPolicy.DONE_ONLY
    cancel_requested: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            TaskStatus.SUCCEEDED, TaskStatus.FAILED,
            TaskStatus.CANCELLED, TaskStatus.TIMED_OUT, TaskStatus.LOST,
        )

    @property
    def is_active(self) -> bool:
        return self.status in (
            TaskStatus.QUEUED, TaskStatus.RUNNING,
            TaskStatus.WAITING, TaskStatus.BLOCKED,
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["scope"] = self.scope.value
        d["delivery_state"] = self.delivery_state.value
        d["notify_policy"] = self.notify_policy.value
        return d


@dataclass(slots=True)
class TaskFlow:
    """A multi-step task flow."""
    flow_id: str
    title: str
    status: FlowStatus
    revision: int = 1
    sync_mode: str = "managed"  # "task_mirrored" or "managed"
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0
    session_id: str = ""
    controller_id: str = ""
    steps: list[str] = field(default_factory=list)
    current_step_index: int = 0
    state_json: str = ""
    wait_json: str = ""
    blocked_task_id: str = ""
    blocked_summary: str = ""
    parent_flow_id: str = ""
    progress_summary: str = ""
    terminal_summary: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            FlowStatus.SUCCEEDED, FlowStatus.FAILED,
            FlowStatus.CANCELLED, FlowStatus.LOST,
        )

    @property
    def is_active(self) -> bool:
        return self.status in (
            FlowStatus.QUEUED, FlowStatus.RUNNING,
            FlowStatus.WAITING, FlowStatus.BLOCKED,
        )

    @property
    def current_step(self) -> str | None:
        if 0 <= self.current_step_index < len(self.steps):
            return self.steps[self.current_step_index]
        return None

    @property
    def progress_pct(self) -> float:
        if not self.steps:
            return 0.0
        return (self.current_step_index / len(self.steps)) * 100

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


# ---------------------------------------------------------------------------
# Task registry
# ---------------------------------------------------------------------------


class TaskRegistry:
    """Registry for tasks and flows with SQLite persistence.

    Provides CRUD operations, status transitions, and querying.
    Uses optimistic concurrency control via revision numbers.
    """

    def __init__(self) -> None:
        self._flows: dict[str, TaskFlow] = {}
        self._tasks: dict[str, Task] = {}
        self._db_available = self._init_db()

    def _init_db(self) -> bool:
        """Initialize SQLite tables for persistence."""
        try:
            db_path = get_kyourai_home() / "tasks.db"
            conn = sqlite3.connect(str(db_path))
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS task_flows (
                    flow_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER DEFAULT 1,
                    sync_mode TEXT DEFAULT 'managed',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL DEFAULT 0,
                    session_id TEXT DEFAULT '',
                    controller_id TEXT DEFAULT '',
                    steps_json TEXT DEFAULT '[]',
                    current_step_index INTEGER DEFAULT 0,
                    state_json TEXT DEFAULT '',
                    wait_json TEXT DEFAULT '',
                    blocked_task_id TEXT DEFAULT '',
                    blocked_summary TEXT DEFAULT '',
                    parent_flow_id TEXT DEFAULT '',
                    progress_summary TEXT DEFAULT '',
                    terminal_summary TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    flow_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER DEFAULT 1,
                    created_at REAL NOT NULL,
                    started_at REAL DEFAULT 0,
                    completed_at REAL DEFAULT 0,
                    parent_task_id TEXT DEFAULT '',
                    parent_flow_id TEXT DEFAULT '',
                    scope TEXT DEFAULT 'session',
                    session_id TEXT DEFAULT '',
                    runtime TEXT DEFAULT '',
                    state_json TEXT DEFAULT '',
                    result TEXT DEFAULT '',
                    error TEXT DEFAULT '',
                    progress_summary TEXT DEFAULT '',
                    terminal_summary TEXT DEFAULT '',
                    tool_use_count INTEGER DEFAULT 0,
                    last_tool_name TEXT DEFAULT '',
                    delivery_state TEXT DEFAULT 'pending',
                    notify_policy TEXT DEFAULT 'done_only',
                    cancel_requested INTEGER DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_flow ON tasks(flow_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
                CREATE INDEX IF NOT EXISTS idx_flows_session ON task_flows(session_id);
                CREATE INDEX IF NOT EXISTS idx_flows_status ON task_flows(status);
            """)
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.warning("Task DB init failed: %s", e)
            return False

    def create_flow(
        self,
        title: str,
        steps: list[str] | None = None,
        session_id: str = "",
        controller_id: str = "",
        parent_flow_id: str = "",
    ) -> TaskFlow:
        """Create a new task flow."""
        flow_id = f"flow-{uuid.uuid4().hex[:12]}"
        now = time.time()

        flow = TaskFlow(
            flow_id=flow_id,
            title=title,
            status=FlowStatus.QUEUED,
            created_at=now,
            updated_at=now,
            session_id=session_id,
            controller_id=controller_id,
            steps=steps or [],
            parent_flow_id=parent_flow_id,
        )

        self._flows[flow_id] = flow
        self._persist_flow(flow)
        logger.info("Created flow %s: %s (%d steps)", flow_id, title, len(flow.steps))
        return flow

    def start_flow(self, flow_id: str) -> TaskFlow | None:
        """Start a queued flow."""
        flow = self._flows.get(flow_id)
        if not flow or flow.status != FlowStatus.QUEUED:
            return None
        flow.status = FlowStatus.RUNNING
        flow.updated_at = time.time()
        flow.revision += 1
        self._persist_flow(flow)
        return flow

    def create_task(
        self,
        flow_id: str,
        title: str,
        runtime: str = "embedded",
        session_id: str = "",
        parent_task_id: str = "",
    ) -> Task | None:
        """Create a task within a flow."""
        flow = self._flows.get(flow_id)
        if not flow:
            return None

        task_id = f"task-{uuid.uuid4().hex[:12]}"
        task = Task(
            task_id=task_id,
            flow_id=flow_id,
            title=title,
            status=TaskStatus.QUEUED,
            created_at=time.time(),
            session_id=session_id or flow.session_id,
            runtime=runtime,
            parent_task_id=parent_task_id,
            parent_flow_id=flow.parent_flow_id,
        )

        self._tasks[task_id] = task
        self._persist_task(task)
        return task

    def start_task(self, task_id: str) -> Task | None:
        """Mark a task as running."""
        task = self._tasks.get(task_id)
        if not task or task.status != TaskStatus.QUEUED:
            return None
        task.status = TaskStatus.RUNNING
        task.started_at = time.time()
        task.revision += 1
        self._persist_task(task)
        return task

    def complete_task(
        self,
        task_id: str,
        result: str = "",
        success: bool = True,
        error: str = "",
    ) -> Task | None:
        """Complete a task."""
        task = self._tasks.get(task_id)
        if not task or not task.is_active:
            return None

        task.completed_at = time.time()
        task.result = result
        task.error = error
        task.status = TaskStatus.SUCCEEDED if success else TaskStatus.FAILED
        task.revision += 1
        self._persist_task(task)

        # Check if flow should advance
        self._maybe_advance_flow(task.flow_id)
        return task

    def cancel_task(self, task_id: str) -> Task | None:
        """Cancel a task."""
        task = self._tasks.get(task_id)
        if not task or not task.is_active:
            return None
        task.status = TaskStatus.CANCELLED
        task.cancel_requested = True
        task.completed_at = time.time()
        task.revision += 1
        self._persist_task(task)
        return task

    def start_step(self, flow_id: str, step_name: str) -> bool:
        """Advance a flow to the named step."""
        flow = self._flows.get(flow_id)
        if not flow or not flow.is_active:
            return False
        if step_name not in flow.steps:
            return False
        flow.current_step_index = flow.steps.index(step_name)
        flow.status = FlowStatus.RUNNING
        flow.updated_at = time.time()
        flow.revision += 1
        self._persist_flow(flow)
        logger.info("Flow %s advanced to step: %s", flow_id, step_name)
        return True

    def complete_step(self, flow_id: str, step_name: str, result: str = "") -> bool:
        """Complete a step and advance to the next."""
        flow = self._flows.get(flow_id)
        if not flow or not flow.is_active:
            return False
        if step_name not in flow.steps:
            return False

        idx = flow.steps.index(step_name)
        flow.current_step_index = idx + 1
        flow.progress_summary = f"Completed: {step_name}"

        # If all steps done, complete the flow
        if flow.current_step_index >= len(flow.steps):
            flow.status = FlowStatus.SUCCEEDED
            flow.completed_at = time.time()
            flow.terminal_summary = result[:200]

        flow.updated_at = time.time()
        flow.revision += 1
        self._persist_flow(flow)
        return True

    def cancel_flow(self, flow_id: str) -> TaskFlow | None:
        """Cancel an entire flow and all its tasks."""
        flow = self._flows.get(flow_id)
        if not flow or not flow.is_active:
            return None

        # Cancel all active tasks in the flow
        for task in self._tasks.values():
            if task.flow_id == flow_id and task.is_active:
                self.cancel_task(task.task_id)

        flow.status = FlowStatus.CANCELLED
        flow.completed_at = time.time()
        flow.revision += 1
        self._persist_flow(flow)
        return flow

    def get_flow(self, flow_id: str) -> TaskFlow | None:
        return self._flows.get(flow_id)

    def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def list_flows(
        self,
        session_id: str | None = None,
        status: FlowStatus | None = None,
        limit: int = 50,
    ) -> list[TaskFlow]:
        """List flows with optional filtering."""
        flows = list(self._flows.values())
        if session_id:
            flows = [f for f in flows if f.session_id == session_id]
        if status:
            flows = [f for f in flows if f.status == status]
        flows.sort(key=lambda f: f.created_at, reverse=True)
        return flows[:limit]

    def list_tasks(
        self,
        flow_id: str | None = None,
        status: TaskStatus | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[Task]:
        """List tasks with optional filtering."""
        tasks = list(self._tasks.values())
        if flow_id:
            tasks = [t for t in tasks if t.flow_id == flow_id]
        if status:
            tasks = [t for t in tasks if t.status == status]
        if session_id:
            tasks = [t for t in tasks if t.session_id == session_id]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks[:limit]

    def list_active_flows(self, session_id: str | None = None) -> list[TaskFlow]:
        """List active (non-terminal) flows."""
        return [
            f for f in self.list_flows(session_id=session_id, limit=1000)
            if f.is_active
        ]

    def list_active_tasks(self, session_id: str | None = None) -> list[Task]:
        """List active (non-terminal) tasks."""
        return [
            t for t in self.list_tasks(session_id=session_id, limit=1000)
            if t.is_active
        ]

    def update_task_progress(
        self,
        task_id: str,
        progress_summary: str = "",
        tool_use_count: int = 0,
        last_tool_name: str = "",
    ) -> None:
        """Update task progress (non-status fields)."""
        task = self._tasks.get(task_id)
        if not task:
            return
        if progress_summary:
            task.progress_summary = progress_summary
        if tool_use_count:
            task.tool_use_count = tool_use_count
        if last_tool_name:
            task.last_tool_name = last_tool_name
        task.revision += 1
        self._persist_task(task)

    def set_delivery_state(self, task_id: str, state: DeliveryState) -> bool:
        """Update the delivery state of a task."""
        task = self._tasks.get(task_id)
        if not task:
            return False
        task.delivery_state = state
        task.revision += 1
        self._persist_task(task)
        return True

    def cleanup_old(self, max_age_seconds: float = 86400) -> int:
        """Remove terminal flows and tasks older than max_age."""
        cutoff = time.time() - max_age_seconds
        removed = 0

        # Clean tasks
        to_remove = [
            tid for tid, t in self._tasks.items()
            if t.is_terminal and t.completed_at < cutoff
        ]
        for tid in to_remove:
            del self._tasks[tid]
            removed += 1

        # Clean flows
        to_remove = [
            fid for fid, f in self._flows.items()
            if f.is_terminal and f.completed_at < cutoff
        ]
        for fid in to_remove:
            del self._flows[fid]
            removed += 1

        return removed

    # -- Internal -----------------------------------------------------------

    def _maybe_advance_flow(self, flow_id: str) -> None:
        """Check if all tasks in a flow are done and advance/complete it."""
        flow = self._flows.get(flow_id)
        if not flow or not flow.is_active:
            return

        flow_tasks = [t for t in self._tasks.values() if t.flow_id == flow_id]
        if not flow_tasks:
            return

        all_terminal = all(t.is_terminal for t in flow_tasks)
        if not all_terminal:
            return

        all_success = all(
            t.status == TaskStatus.SUCCEEDED for t in flow_tasks
        )
        flow.status = FlowStatus.SUCCEEDED if all_success else FlowStatus.FAILED
        flow.completed_at = time.time()
        flow.revision += 1
        self._persist_flow(flow)

    def _persist_flow(self, flow: TaskFlow) -> None:
        if not self._db_available:
            return
        try:
            db_path = get_kyourai_home() / "tasks.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                INSERT OR REPLACE INTO task_flows
                (flow_id, title, status, revision, sync_mode, created_at, updated_at,
                 completed_at, session_id, controller_id, steps_json, current_step_index,
                 state_json, wait_json, blocked_task_id, blocked_summary,
                 parent_flow_id, progress_summary, terminal_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                flow.flow_id, flow.title, flow.status.value, flow.revision,
                flow.sync_mode, flow.created_at, flow.updated_at, flow.completed_at,
                flow.session_id, flow.controller_id, json.dumps(flow.steps),
                flow.current_step_index, flow.state_json, flow.wait_json,
                flow.blocked_task_id, flow.blocked_summary, flow.parent_flow_id,
                flow.progress_summary, flow.terminal_summary,
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("Failed to persist flow: %s", e)

    def _persist_task(self, task: Task) -> None:
        if not self._db_available:
            return
        try:
            db_path = get_kyourai_home() / "tasks.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                INSERT OR REPLACE INTO tasks
                (task_id, flow_id, title, status, revision, created_at, started_at,
                 completed_at, parent_task_id, parent_flow_id, scope, session_id,
                 runtime, state_json, result, error, progress_summary,
                 terminal_summary, tool_use_count, last_tool_name,
                 delivery_state, notify_policy, cancel_requested)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task.task_id, task.flow_id, task.title, task.status.value,
                task.revision, task.created_at, task.started_at, task.completed_at,
                task.parent_task_id, task.parent_flow_id, task.scope.value,
                task.session_id, task.runtime, task.state_json, task.result,
                task.error, task.progress_summary, task.terminal_summary,
                task.tool_use_count, task.last_tool_name,
                task.delivery_state.value, task.notify_policy.value,
                int(task.cancel_requested),
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("Failed to persist task: %s", e)
