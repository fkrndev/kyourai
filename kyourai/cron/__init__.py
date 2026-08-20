"""Cron scheduler — persistent scheduled tasks for the agent.

Inspired by OpenClaw's cron system, adapted for Kyourai.

Tasks are stored in SQLite and run on a background thread. Each task has:
  - A cron expression (5-field: minute hour day month weekday)
  - An action type: "agent_turn" (run agent on a prompt), "tool" (call a
    memory tool), or "curator" (run the memory curator)
  - Optional skill reference (for agent_turn tasks)
  - Enabled/disabled flag
  - Last run time and next run time

The scheduler checks every 60 seconds for due tasks and runs them in
background threads. Only one instance of each task runs at a time.

Usage:
    scheduler = CronScheduler(store=memory_store)
    scheduler.start()
    scheduler.add_task("daily-curator", "0 9 * * *", action="curator")
    scheduler.stop()
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from croniter import croniter

from kyourai.constants import get_kyourai_home
from kyourai.utils import atomic_json_write

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 60


@dataclass
class CronTask:
    """A scheduled task."""
    task_id: str          # unique identifier
    cron_expr: str        # 5-field cron expression
    action: str           # "agent_turn", "tool", "curator"
    prompt: str = ""      # prompt for agent_turn, tool name for tool
    skill: str = ""       # optional skill reference
    enabled: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_run_at: str | None = None
    next_run_at: str | None = None
    run_count: int = 0
    last_result: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "cron_expr": self.cron_expr,
            "action": self.action,
            "prompt": self.prompt,
            "skill": self.skill,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
            "run_count": self.run_count,
            "last_result": self.last_result,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CronTask":
        return cls(
            task_id=data["task_id"],
            cron_expr=data["cron_expr"],
            action=data.get("action", "agent_turn"),
            prompt=data.get("prompt", ""),
            skill=data.get("skill", ""),
            enabled=data.get("enabled", True),
            created_at=data.get("created_at", ""),
            last_run_at=data.get("last_run_at"),
            next_run_at=data.get("next_run_at"),
            run_count=data.get("run_count", 0),
            last_result=data.get("last_result"),
        )


def _cron_state_path() -> Path:
    return get_kyourai_home() / "cron_state.json"


def _load_tasks() -> dict[str, CronTask]:
    """Load tasks from the state file."""
    path = _cron_state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tasks = {}
        for tid, tdata in (data.get("tasks") or {}).items():
            tasks[tid] = CronTask.from_dict(tdata)
        return tasks
    except Exception as e:
        logger.warning("Failed to load cron state: %s", e)
        return {}


def _save_tasks(tasks: dict[str, CronTask]) -> None:
    """Save tasks to the state file."""
    path = _cron_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "tasks": {tid: t.to_dict() for tid, t in tasks.items()},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_json_write(path, data, indent=2)


def _compute_next_run(cron_expr: str, base: datetime | None = None) -> str | None:
    """Compute the next run time for a cron expression."""
    if base is None:
        base = datetime.now(timezone.utc)
    try:
        cron = croniter(cron_expr, base)
        return cron.get_next(datetime).isoformat()
    except Exception as e:
        logger.warning("Invalid cron expression '%s': %s", cron_expr, e)
        return None


def _is_due(task: CronTask, now: datetime | None = None) -> bool:
    """Check if a task is due to run."""
    if not task.enabled:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    if not task.next_run_at:
        return True  # never run — schedule immediately
    try:
        next_run = datetime.fromisoformat(task.next_run_at)
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=timezone.utc)
        return now >= next_run
    except Exception:
        return True


class CronScheduler:
    """Background scheduler for cron-based tasks.

    Runs a background thread that checks every 60 seconds for due tasks.
    Each task runs in its own thread. Only one instance of each task runs
    at a time (guarded by a per-task lock).
    """

    def __init__(
        self,
        *,
        agent_run_fn: Callable[[str, str], str] | None = None,
        tool_call_fn: Callable[[str, dict], str] | None = None,
        curator_fn: Callable[[], dict] | None = None,
    ):
        self._tasks: dict[str, CronTask] = _load_tasks()
        self._agent_run_fn = agent_run_fn
        self._tool_call_fn = tool_call_fn
        self._curator_fn = curator_fn
        self._running_locks: dict[str, threading.Lock] = {}
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._state_lock = threading.RLock()

        # Compute next run for any tasks that don't have one
        for task in self._tasks.values():
            if not task.next_run_at:
                task.next_run_at = _compute_next_run(task.cron_expr)
        _save_tasks(self._tasks)

    # -- Public API ----------------------------------------------------------

    def add_task(
        self,
        task_id: str,
        cron_expr: str,
        action: str = "agent_turn",
        prompt: str = "",
        skill: str = "",
        enabled: bool = True,
    ) -> CronTask:
        """Add a new scheduled task. Overwrites if task_id exists."""
        with self._state_lock:
            task = CronTask(
                task_id=task_id,
                cron_expr=cron_expr,
                action=action,
                prompt=prompt,
                skill=skill,
                enabled=enabled,
                next_run_at=_compute_next_run(cron_expr),
            )
            self._tasks[task_id] = task
            self._running_locks[task_id] = threading.Lock()
            _save_tasks(self._tasks)
            logger.info("Added cron task '%s' (%s %s)", task_id, cron_expr, action)
            return task

    def remove_task(self, task_id: str) -> bool:
        """Remove a scheduled task."""
        with self._state_lock:
            if task_id not in self._tasks:
                return False
            del self._tasks[task_id]
            self._running_locks.pop(task_id, None)
            _save_tasks(self._tasks)
            logger.info("Removed cron task '%s'", task_id)
            return True

    def enable_task(self, task_id: str) -> bool:
        """Enable a task."""
        with self._state_lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.enabled = True
            task.next_run_at = _compute_next_run(task.cron_expr)
            _save_tasks(self._tasks)
            return True

    def disable_task(self, task_id: str) -> bool:
        """Disable a task."""
        with self._state_lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            task.enabled = False
            _save_tasks(self._tasks)
            return True

    def list_tasks(self) -> list[CronTask]:
        """List all tasks."""
        with self._state_lock:
            return list(self._tasks.values())

    def get_task(self, task_id: str) -> CronTask | None:
        """Get a task by ID."""
        with self._state_lock:
            return self._tasks.get(task_id)

    def run_task_now(self, task_id: str) -> str | None:
        """Run a task immediately, ignoring its schedule. Returns the result."""
        with self._state_lock:
            task = self._tasks.get(task_id)
        if task is None:
            return None
        return self._execute_task(task)

    def start(self) -> None:
        """Start the background scheduler thread."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="kyourai-cron")
        self._thread.start()
        logger.info("Cron scheduler started")

    def stop(self) -> None:
        """Stop the background scheduler thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("Cron scheduler stopped")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- Internal ------------------------------------------------------------

    def _run_loop(self) -> None:
        """Main scheduler loop — checks for due tasks every 60 seconds."""
        while not self._stop_event.is_set():
            try:
                self._check_and_run()
            except Exception as e:
                logger.warning("Cron scheduler error: %s", e)
            self._stop_event.wait(_CHECK_INTERVAL_SECONDS)

    def _check_and_run(self) -> None:
        """Check all tasks and run any that are due."""
        now = datetime.now(timezone.utc)
        with self._state_lock:
            tasks = list(self._tasks.values())

        for task in tasks:
            if _is_due(task, now):
                # Run in a background thread (non-blocking)
                lock = self._running_locks.get(task.task_id)
                if lock is not None and lock.acquire(blocking=False):
                    def _run(t=task, l=lock):
                        try:
                            self._execute_task(t)
                        finally:
                            l.release()
                    threading.Thread(target=_run, daemon=True).start()

    def _execute_task(self, task: CronTask) -> str | None:
        """Execute a single task and update its state."""
        logger.info("Running cron task '%s' (action=%s)", task.task_id, task.action)
        result = None
        try:
            if task.action == "agent_turn" and self._agent_run_fn:
                result = self._agent_run_fn(task.prompt, task.skill)
            elif task.action == "tool" and self._tool_call_fn:
                args = json.loads(task.prompt) if task.prompt.startswith("{") else {"content": task.prompt}
                result = self._tool_call_fn(task.prompt, args) if isinstance(task.prompt, str) else ""
            elif task.action == "curator" and self._curator_fn:
                result = json.dumps(self._curator_fn())
            else:
                result = f"No handler for action: {task.action}"
        except Exception as e:
            result = f"Error: {e}"
            logger.warning("Cron task '%s' failed: %s", task.task_id, e)

        # Update task state
        with self._state_lock:
            task.last_run_at = datetime.now(timezone.utc).isoformat()
            task.next_run_at = _compute_next_run(task.cron_expr)
            task.run_count += 1
            task.last_result = result[:500] if result else None
            _save_tasks(self._tasks)

        return result


def referenced_skill_names() -> set[str]:
    """Return skill names referenced by any cron task (for curator protection)."""
    tasks = _load_tasks()
    return {t.skill for t in tasks.values() if t.skill}
