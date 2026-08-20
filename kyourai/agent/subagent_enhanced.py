"""Enhanced subagent system — registry, lifecycle, spawn modes, tool policy.

Inspired by OpenClaw's subagent architecture but adapted for Python/asyncio:
  - SubagentRegistry: in-memory registry of all active subagent runs
  - Lifecycle states: pending → running → succeeded/failed/cancelled/timed_out
  - Spawn modes: "run" (fire-and-forget), "collect" (swarm collector)
  - Tool policy inheritance: subagents inherit parent's allow/deny lists
  - Depth limiting: prevent infinite subagent recursion
  - Controller scope: parent controls entire subtree

Usage:
    from kyourai.agent.subagent_enhanced import SubagentRegistry, SpawnMode

    registry = SubagentRegistry()
    run = registry.register(parent_session="main", task="...", mode=SpawnMode.RUN)
    # ... run subagent ...
    registry.complete(run.run_id, output="...", success=True)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SpawnMode(str, Enum):
    """How a subagent is spawned."""
    RUN = "run"           # Fire-and-forget background task
    COLLECT = "collect"   # Swarm collector — gather results from multiple


class RunStatus(str, Enum):
    """Lifecycle status of a subagent run."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    LOST = "lost"  # Process crashed without notification


class TerminalOutcome(str, Enum):
    """Terminal outcome for required-completion tasks."""
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

DEFAULT_MAX_SPAWN_DEPTH = 3
DEFAULT_MAX_CHILDREN_PER_AGENT = 5
DEFAULT_SUBAGENT_TIMEOUT = 120.0


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolPolicy:
    """Tool allow/deny policy for subagents."""
    allow: list[str] = field(default_factory=list)  # empty = allow all
    deny: list[str] = field(default_factory=list)
    workspace_only: bool = False  # restrict file access to workspace

    def is_allowed(self, tool_name: str) -> bool:
        """Check if a tool is allowed by this policy."""
        if tool_name in self.deny:
            return False
        if not self.allow:
            return True  # empty allow = allow all
        if "*" in self.allow:
            return True
        return tool_name in self.allow

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SubagentRun:
    """A registered subagent run."""
    run_id: str
    parent_session_id: str
    subagent_session_id: str
    task: str
    mode: SpawnMode
    status: RunStatus
    depth: int
    tool_policy: ToolPolicy
    created_at: float
    started_at: float = 0.0
    completed_at: float = 0.0
    output: str = ""
    error: str = ""
    duration_ms: int = 0
    progress_summary: str = ""
    tool_use_count: int = 0
    last_tool_name: str = ""
    # Internal — the asyncio task if running
    _async_task: asyncio.Task | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            RunStatus.SUCCEEDED, RunStatus.FAILED,
            RunStatus.CANCELLED, RunStatus.TIMED_OUT, RunStatus.LOST,
        )

    @property
    def is_active(self) -> bool:
        return self.status in (RunStatus.PENDING, RunStatus.RUNNING)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("_async_task", None)
        d["mode"] = self.mode.value
        d["status"] = self.status.value
        return d


# ---------------------------------------------------------------------------
# Subagent registry
# ---------------------------------------------------------------------------


class SubagentRegistry:
    """In-memory registry of all subagent runs.

    Tracks active and completed runs, enforces depth/child limits,
    and provides query/cancel operations.
    """

    def __init__(
        self,
        max_depth: int = DEFAULT_MAX_SPAWN_DEPTH,
        max_children: int = DEFAULT_MAX_CHILDREN_PER_AGENT,
    ) -> None:
        self._runs: dict[str, SubagentRun] = {}
        self._max_depth = max_depth
        self._max_children = max_children
        self._lock = asyncio.Lock()

    async def register(
        self,
        parent_session_id: str,
        task: str,
        mode: SpawnMode = SpawnMode.RUN,
        tool_policy: ToolPolicy | None = None,
    ) -> SubagentRun:
        """Register a new subagent run.

        Raises if depth or child limits are exceeded.
        """
        async with self._lock:
            # Check depth
            depth = self._get_depth(parent_session_id)
            if depth >= self._max_depth:
                raise ValueError(
                    f"Max spawn depth ({self._max_depth}) exceeded for "
                    f"session {parent_session_id}"
                )

            # Check children count
            active_children = self._count_active_children(parent_session_id)
            if active_children >= self._max_children:
                raise ValueError(
                    f"Max children ({self._max_children}) exceeded for "
                    f"session {parent_session_id}"
                )

            run_id = f"run-{uuid.uuid4().hex[:12]}"
            sub_session = f"subagent-{uuid.uuid4().hex[:8]}"

            run = SubagentRun(
                run_id=run_id,
                parent_session_id=parent_session_id,
                subagent_session_id=sub_session,
                task=task,
                mode=mode,
                status=RunStatus.PENDING,
                depth=depth,
                tool_policy=tool_policy or ToolPolicy(),
                created_at=time.time(),
            )

            self._runs[run_id] = run
            logger.info(
                "Registered subagent run %s (depth=%d, mode=%s) for parent %s",
                run_id, depth, mode.value, parent_session_id,
            )
            return run

    async def start(self, run_id: str, async_task: asyncio.Task) -> None:
        """Mark a run as started and attach its asyncio task."""
        async with self._lock:
            run = self._runs.get(run_id)
            if not run:
                raise KeyError(f"Run {run_id} not found")
            run.status = RunStatus.RUNNING
            run.started_at = time.time()
            run._async_task = async_task

    async def complete(
        self,
        run_id: str,
        output: str = "",
        success: bool = True,
        error: str = "",
    ) -> SubagentRun:
        """Mark a run as completed."""
        async with self._lock:
            run = self._runs.get(run_id)
            if not run:
                raise KeyError(f"Run {run_id} not found")
            run.completed_at = time.time()
            run.duration_ms = int(
                (run.completed_at - run.started_at) * 1000
            ) if run.started_at else 0
            run.output = output
            run.error = error
            run.status = RunStatus.SUCCEEDED if success else RunStatus.FAILED
            run._async_task = None
            logger.info(
                "Subagent run %s completed: %s (%dms)",
                run_id, run.status.value, run.duration_ms,
            )
            return run

    async def cancel(self, run_id: str) -> bool:
        """Cancel an active run."""
        async with self._lock:
            run = self._runs.get(run_id)
            if not run or not run.is_active:
                return False

            # Cancel the asyncio task if present
            if run._async_task and not run._async_task.done():
                run._async_task.cancel()

            run.status = RunStatus.CANCELLED
            run.completed_at = time.time()
            run.duration_ms = int(
                (run.completed_at - run.started_at) * 1000
            ) if run.started_at else 0
            run._async_task = None
            logger.info("Subagent run %s cancelled", run_id)
            return True

    async def mark_lost(self, run_id: str) -> bool:
        """Mark a run as lost (crashed without notification)."""
        async with self._lock:
            run = self._runs.get(run_id)
            if not run or not run.is_active:
                return False
            run.status = RunStatus.LOST
            run.completed_at = time.time()
            run._async_task = None
            logger.warning("Subagent run %s marked as lost", run_id)
            return True

    def get(self, run_id: str) -> SubagentRun | None:
        """Get a run by ID."""
        return self._runs.get(run_id)

    def list_active(self, parent_session_id: str | None = None) -> list[SubagentRun]:
        """List active runs, optionally filtered by parent session."""
        runs = [r for r in self._runs.values() if r.is_active]
        if parent_session_id:
            runs = [r for r in runs if r.parent_session_id == parent_session_id]
        return runs

    def list_completed(
        self,
        parent_session_id: str | None = None,
        limit: int = 50,
    ) -> list[SubagentRun]:
        """List completed runs, optionally filtered by parent session."""
        runs = [r for r in self._runs.values() if r.is_terminal]
        if parent_session_id:
            runs = [r for r in runs if r.parent_session_id == parent_session_id]
        runs.sort(key=lambda r: r.completed_at, reverse=True)
        return runs[:limit]

    def list_all(self, parent_session_id: str | None = None) -> list[SubagentRun]:
        """List all runs (active + completed)."""
        if parent_session_id:
            return [r for r in self._runs.values()
                    if r.parent_session_id == parent_session_id]
        return list(self._runs.values())

    def get_tree(self, session_id: str) -> dict[str, Any]:
        """Get the session tree for a given session (parent or subagent)."""
        children = [r for r in self._runs.values()
                    if r.parent_session_id == session_id]
        return {
            "session_id": session_id,
            "active_children": len([c for c in children if c.is_active]),
            "total_children": len(children),
            "children": [
                {
                    "run_id": c.run_id,
                    "status": c.status.value,
                    "task": c.task[:100],
                    "depth": c.depth,
                }
                for c in children
            ],
        }

    def count_active(self) -> int:
        """Count total active runs."""
        return len([r for r in self._runs.values() if r.is_active])

    def cleanup_old(self, max_age_seconds: float = 3600) -> int:
        """Remove completed runs older than max_age. Returns count removed."""
        cutoff = time.time() - max_age_seconds
        to_remove = [
            run_id for run_id, run in self._runs.items()
            if run.is_terminal and run.completed_at < cutoff
        ]
        for run_id in to_remove:
            del self._runs[run_id]
        return len(to_remove)

    # -- Internal helpers ---------------------------------------------------

    def _get_depth(self, parent_session_id: str) -> int:
        """Get the spawn depth for a new child of parent_session_id."""
        # If parent is itself a subagent, find its depth
        for run in self._runs.values():
            if run.subagent_session_id == parent_session_id:
                return run.depth + 1
        return 0  # parent is a root agent

    def _count_active_children(self, parent_session_id: str) -> int:
        """Count active children of a parent session."""
        return len([
            r for r in self._runs.values()
            if r.parent_session_id == parent_session_id and r.is_active
        ])


# ---------------------------------------------------------------------------
# Enhanced delegator — uses registry + tool policy
# ---------------------------------------------------------------------------


class EnhancedSubagentDelegator:
    """Enhanced subagent delegator with registry, lifecycle, and tool policy.

    Wraps SubagentDelegator with:
      - SubagentRegistry for tracking all runs
      - Tool policy inheritance from parent
      - Spawn modes (run, collect)
      - Depth limiting
      - Cancel/monitor operations
    """

    def __init__(
        self,
        parent: Any,
        registry: SubagentRegistry | None = None,
    ) -> None:
        self._parent = parent
        self._registry = registry or SubagentRegistry()
        self._parent_session = getattr(parent, "session_id", "main")

    async def delegate(
        self,
        task: str,
        *,
        model: str | None = None,
        extra_instructions: str = "",
        timeout: float = DEFAULT_SUBAGENT_TIMEOUT,
        mode: SpawnMode = SpawnMode.RUN,
        tool_policy: ToolPolicy | None = None,
    ) -> SubagentRun:
        """Delegate a task to a subagent with full lifecycle tracking.

        Args:
            task: Task description for the subagent
            model: Model to use (defaults to parent's model)
            extra_instructions: Additional system prompt instructions
            timeout: Maximum execution time in seconds
            mode: Spawn mode (run or collect)
            tool_policy: Tool policy for the subagent (inherits parent's if None)

        Returns:
            SubagentRun with the completed result
        """
        # Inherit parent's tool policy if not specified
        if tool_policy is None:
            tool_policy = self._inherit_tool_policy()

        # Register the run
        run = await self._registry.register(
            parent_session_id=self._parent_session,
            task=task,
            mode=mode,
            tool_policy=tool_policy,
        )

        # Create and start the async task
        async_task = asyncio.create_task(
            self._run_subagent(run, model, extra_instructions, timeout)
        )
        await self._registry.start(run.run_id, async_task)

        # Wait for completion
        try:
            result_run = await async_task
            return result_run
        except asyncio.CancelledError:
            await self._registry.cancel(run.run_id)
            raise

    async def delegate_collect(
        self,
        tasks: list[str],
        *,
        model: str | None = None,
        timeout: float = DEFAULT_SUBAGENT_TIMEOUT,
        tool_policy: ToolPolicy | None = None,
    ) -> list[SubagentRun]:
        """Delegate multiple tasks in collect mode (parallel swarm).

        All tasks run in parallel and results are collected.
        """
        coros = [
            self.delegate(
                task=t,
                model=model,
                timeout=timeout,
                mode=SpawnMode.COLLECT,
                tool_policy=tool_policy,
            )
            for t in tasks
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        # Convert exceptions to failed runs
        final: list[SubagentRun] = []
        for r in results:
            if isinstance(r, SubagentRun):
                final.append(r)
            elif isinstance(r, Exception):
                # Create a failed run for the exception
                run = await self._registry.register(
                    parent_session_id=self._parent_session,
                    task="(collect exception)",
                    mode=SpawnMode.COLLECT,
                )
                await self._registry.complete(
                    run.run_id, success=False, error=str(r)
                )
                final.append(run)
        return final

    async def cancel(self, run_id: str) -> bool:
        """Cancel an active subagent run."""
        return await self._registry.cancel(run_id)

    def list_active(self) -> list[SubagentRun]:
        """List active subagent runs for this parent."""
        return self._registry.list_active(self._parent_session)

    def list_completed(self, limit: int = 20) -> list[SubagentRun]:
        """List completed subagent runs for this parent."""
        return self._registry.list_completed(self._parent_session, limit)

    @property
    def registry(self) -> SubagentRegistry:
        return self._registry

    # -- Internal -----------------------------------------------------------

    async def _run_subagent(
        self,
        run: SubagentRun,
        model: str | None,
        extra_instructions: str,
        timeout: float,
    ) -> SubagentRun:
        """Execute the subagent and update the registry on completion."""
        from kyourai.agent import KyouraiAgent

        try:
            sub_model = model or self._parent.model
            subagent = KyouraiAgent(
                model=sub_model,
                session_id=run.subagent_session_id,
                enable_curator=False,
                enable_skills=False,
                enable_cron=False,
                extra_instructions=extra_instructions,
            )

            result = await asyncio.wait_for(
                subagent.run(run.task),
                timeout=timeout,
            )

            subagent.shutdown()
            completed = await self._registry.complete(
                run.run_id, output=result, success=True
            )
            return completed

        except asyncio.TimeoutError:
            logger.warning("Subagent %s timed out after %ss", run.run_id, timeout)
            completed = await self._registry.complete(
                run.run_id,
                success=False,
                error=f"Timed out after {timeout}s",
            )
            completed.status = RunStatus.TIMED_OUT
            return completed

        except asyncio.CancelledError:
            await self._registry.cancel(run.run_id)
            raise

        except Exception as e:
            logger.warning("Subagent %s failed: %s", run.run_id, e)
            completed = await self._registry.complete(
                run.run_id, success=False, error=str(e)
            )
            return completed

    def _inherit_tool_policy(self) -> ToolPolicy:
        """Inherit tool policy from parent agent's config."""
        try:
            config = getattr(self._parent, "_config", {})
            tools_cfg = config.get("tools", {}) if config else {}
            return ToolPolicy(
                allow=tools_cfg.get("allow", []),
                deny=tools_cfg.get("deny", []),
                workspace_only=tools_cfg.get("workspace_only", False),
            )
        except Exception:
            return ToolPolicy()
