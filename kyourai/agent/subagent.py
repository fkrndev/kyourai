"""Subagent delegation — spawn subagents for parallel task execution.

Allows the main agent to delegate subtasks to independent subagent
instances. Each subagent runs in its own session with its own memory
context, and results are collected back.

Simpler than Hermes' subagent_lifecycle.py (20k LOC) — no async
delegation queue, no delivery tracking, no cross-session handoff.
Just a clean spawn→run→collect pattern.

Usage:
    delegator = SubagentDelegator(parent_agent)
    result = await delegator.delegate(
        task="Research the latest Python async patterns",
        model="openai:gpt-4o",
    )
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DelegationResult:
    """Result of a subagent delegation."""
    task: str
    output: str
    session_id: str
    success: bool = True
    error: str | None = None
    duration_ms: int = 0


class SubagentDelegator:
    """Manages subagent delegation for a parent agent.

    Each subagent gets its own KyouraiAgent instance with its own session,
    but shares the parent's memory providers (read-only access to facts).
    """

    def __init__(self, parent: Any) -> None:
        """Initialize with a parent KyouraiAgent.

        Args:
            parent: The parent KyouraiAgent instance
        """
        self._parent = parent
        self._active: dict[str, asyncio.Task] = {}

    async def delegate(
        self,
        task: str,
        *,
        model: str | None = None,
        extra_instructions: str = "",
        timeout: float = 120.0,
    ) -> DelegationResult:
        """Delegate a task to a subagent.

        Args:
            task: The task description / prompt for the subagent
            model: Model to use (defaults to parent's model)
            extra_instructions: Additional system prompt instructions
            timeout: Maximum execution time in seconds

        Returns:
            DelegationResult with the subagent's output
        """
        import time

        session_id = f"subagent-{uuid.uuid4().hex[:8]}"
        start = time.time()

        try:
            # Create a subagent with its own session
            # We import here to avoid circular imports
            from kyourai.agent import KyouraiAgent

            sub_model = model or self._parent.model
            # Pass model directly — KyouraiAgent handles both string and
            # object models (coerces to string only for SessionDB storage)

            subagent = KyouraiAgent(
                model=sub_model,
                session_id=session_id,
                enable_curator=False,  # subagents don't run curator
                enable_skills=False,   # subagents don't need skills
                enable_cron=False,     # subagents don't run cron
                extra_instructions=extra_instructions,
            )

            # Run the subagent with a timeout
            result = await asyncio.wait_for(
                subagent.run(task),
                timeout=timeout,
            )

            duration = int((time.time() - start) * 1000)
            subagent.shutdown()

            return DelegationResult(
                task=task,
                output=result,
                session_id=session_id,
                success=True,
                duration_ms=duration,
            )

        except asyncio.TimeoutError:
            duration = int((time.time() - start) * 1000)
            logger.warning("Subagent %s timed out after %ss", session_id, timeout)
            return DelegationResult(
                task=task,
                output="",
                session_id=session_id,
                success=False,
                error=f"Timed out after {timeout}s",
                duration_ms=duration,
            )
        except Exception as e:
            duration = int((time.time() - start) * 1000)
            logger.warning("Subagent %s failed: %s", session_id, e)
            return DelegationResult(
                task=task,
                output="",
                session_id=session_id,
                success=False,
                error=str(e),
                duration_ms=duration,
            )

    async def delegate_batch(
        self,
        tasks: list[str],
        *,
        model: str | None = None,
        timeout: float = 120.0,
    ) -> list[DelegationResult]:
        """Delegate multiple tasks in parallel.

        Args:
            tasks: List of task descriptions
            model: Model to use for all subagents
            timeout: Maximum execution time per subagent

        Returns:
            List of DelegationResults (in same order as tasks)
        """
        coros = [
            self.delegate(task, model=model, timeout=timeout)
            for task in tasks
        ]
        results = await asyncio.gather(*coros, return_exceptions=False)
        return list(results)

    def get_active_count(self) -> int:
        """Return the number of currently active subagents."""
        return len(self._active)
