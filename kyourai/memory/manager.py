"""MemoryManager — orchestrates memory providers for the agent.

Ported from Hermes Agent (agent/memory_manager.py), adapted for Kyourai.

Single integration point in the agent loop. Replaces scattered per-backend
code with one manager that delegates to registered providers.

Only ONE external plugin provider is allowed at a time — attempting to
register a second external provider is rejected with a warning. This
prevents tool schema bloat and conflicting memory backends.

Usage:
    manager = MemoryManager()
    manager.add_provider(BuiltinMemoryProvider())  # always first
    manager.add_provider(holographic_provider)      # at most one external

    # System prompt
    prompt_parts.append(manager.build_system_prompt())

    # Pre-turn
    context = manager.prefetch_all(user_message)

    # Post-turn
    manager.sync_all(user_msg, assistant_response)
    manager.queue_prefetch_all(user_msg)
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from kyourai.memory.provider import MemoryProvider, RecallStatus, is_trivial_prompt

logger = logging.getLogger(__name__)

_SYNC_DRAIN_TIMEOUT_S = 5.0
_EXTERNAL_PREFETCH_TIMEOUT_S = 8.0

# Context fencing — memory context injected by providers is wrapped in these
# tags so it can be scrubbed from visible output.
_FENCE_TAG_RE = re.compile(r'</?\s*memory-context\s*>', re.IGNORECASE)
_INTERNAL_CONTEXT_RE = re.compile(
    r'<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>', re.IGNORECASE
)
_INTERNAL_NOTE_RE = re.compile(
    r'\[System note:\s*The following is recalled memory context,\s*NOT new user input\.\s*'
    r'Treat as (?:informational background data|authoritative reference data[^\]]*)\.\]\s*',
    re.IGNORECASE,
)


def sanitize_context(text: str) -> str:
    """Strip fence tags, injected context blocks, and system notes from provider output."""
    text = _INTERNAL_CONTEXT_RE.sub('', text)
    text = _INTERNAL_NOTE_RE.sub('', text)
    text = _FENCE_TAG_RE.sub('', text)
    return text


def normalize_tool_schema(schema: Any) -> dict[str, Any] | None:
    """Return a function-tool dict with a resolvable top-level name.

    Handles both bare function schemas and already-wrapped OpenAI tool entries.
    Returns None for anything without a resolvable name.
    """
    if not isinstance(schema, dict):
        return None
    if schema.get("type") == "function" and isinstance(schema.get("function"), dict):
        schema = schema["function"]
        if not isinstance(schema, dict):
            return None
    name = schema.get("name", "")
    if not name or not isinstance(name, str):
        return None
    return schema


def tool_error(message: str) -> str:
    """Return a JSON error string for tool dispatch failures."""
    return json.dumps({"error": message}, ensure_ascii=False)


class MemoryManager:
    """Orchestrates the built-in provider plus at most one external provider.

    The builtin provider is always first. Only one non-builtin (external)
    provider is allowed. Failures in one provider never block the other.
    """

    def __init__(self, *, external_prefetch_timeout: float | None = None) -> None:
        self._providers: list[MemoryProvider] = []
        self._tool_to_provider: dict[str, MemoryProvider] = {}
        self._has_external = False
        self._external_prefetch_timeout = (
            _EXTERNAL_PREFETCH_TIMEOUT_S if external_prefetch_timeout is None
            else float(external_prefetch_timeout)
        )
        if self._external_prefetch_timeout <= 0:
            raise ValueError("external_prefetch_timeout must be positive")
        self._external_prefetch_threads: dict[str, threading.Thread] = {}
        self._external_prefetch_lock = threading.Lock()
        # Background executor for end-of-turn sync/prefetch. Lazily created.
        self._sync_executor: ThreadPoolExecutor | None = None
        self._sync_executor_lock = threading.Lock()
        self._background_futures: dict[Future, str] = {}
        self._shutting_down = False

    # -- Registration --------------------------------------------------------

    def add_provider(self, provider: MemoryProvider) -> None:
        """Register a memory provider.

        Built-in provider (name "builtin") is always accepted.
        Only one external (non-builtin) provider is allowed.
        """
        is_builtin = provider.name == "builtin"
        if not is_builtin:
            if self._has_external:
                existing = next(
                    (p.name for p in self._providers if p.name != "builtin"), "unknown"
                )
                logger.warning(
                    "Rejected memory provider '%s' — external provider '%s' is "
                    "already registered. Only one external memory provider allowed.",
                    provider.name, existing,
                )
                return
            self._has_external = True

        self._providers.append(provider)

        for raw_schema in provider.get_tool_schemas():
            schema = normalize_tool_schema(raw_schema)
            if schema is None:
                continue
            tool_name = schema["name"]
            if tool_name and tool_name not in self._tool_to_provider:
                self._tool_to_provider[tool_name] = provider
            elif tool_name in self._tool_to_provider:
                logger.warning(
                    "Memory tool name conflict: '%s' already registered by %s",
                    tool_name, self._tool_to_provider[tool_name].name,
                )

        logger.info("Memory provider '%s' registered (%d tools)", provider.name, len(provider.get_tool_schemas()))

    @property
    def providers(self) -> list[MemoryProvider]:
        return list(self._providers)

    def get_provider(self, name: str) -> MemoryProvider | None:
        for p in self._providers:
            if p.name == name:
                return p
        return None

    # -- System prompt -------------------------------------------------------

    def build_system_prompt(self) -> str:
        """Collect system prompt blocks from all providers."""
        blocks = []
        for provider in self._providers:
            try:
                block = provider.system_prompt_block()
                if block and block.strip():
                    blocks.append(block)
            except Exception as e:
                logger.warning("Memory provider '%s' system_prompt_block() failed: %s", provider.name, e)
        return "\n\n".join(blocks)

    # -- Prefetch / recall ---------------------------------------------------

    def prefetch_all(self, query: str, *, session_id: str = "") -> str:
        """Collect prefetch context from all providers.

        Returns merged context text labeled by provider. Empty providers
        are skipped. Failures in one provider don't block others.
        """
        if is_trivial_prompt(query):
            return ""
        parts = []
        for provider in self._providers:
            try:
                result = self._prefetch_provider(provider, query, session_id=session_id)
                if result and result.strip():
                    parts.append(result)
            except Exception as e:
                logger.debug("Memory provider '%s' prefetch failed (non-fatal): %s", provider.name, e)
        return "\n\n".join(parts)

    def _prefetch_provider(
        self, provider: MemoryProvider, query: str, *, session_id: str = ""
    ) -> str:
        if provider.name == "builtin":
            return provider.prefetch(query, session_id=session_id)

        # External providers get a timeout so a slow backend doesn't block the turn.
        result_box: dict[str, str] = {}
        error_box: dict[str, Exception] = {}

        def _run() -> None:
            try:
                result_box["value"] = provider.prefetch(query, session_id=session_id)
            except Exception as e:
                error_box["error"] = e

        thread = threading.Thread(target=_run, daemon=True, name=f"mem-prefetch-{provider.name}")
        thread.start()
        thread.join(timeout=self._external_prefetch_timeout)

        if thread.is_alive():
            logger.warning(
                "Memory provider '%s' prefetch timed out after %.1fs — using empty result",
                provider.name, self._external_prefetch_timeout,
            )
            with self._external_prefetch_lock:
                self._external_prefetch_threads[provider.name] = thread
            return ""

        if "error" in error_box:
            raise error_box["error"]
        return result_box.get("value", "")

    def describe_recall(self) -> str:
        """Build a deterministic, model-independent recall indicator line."""
        segments: list[str] = []
        for provider in self._providers:
            try:
                status = provider.recall_status()
            except Exception as e:
                logger.debug("Memory provider '%s' recall_status failed: %s", provider.name, e)
                continue
            if status is None:
                continue
            if status.count == 1:
                detail = "recalled 1 memory"
            elif status.count > 1:
                detail = f"recalled {status.count} memories"
            else:
                detail = "recalled relevant memory"
            segments.append(f"{status.glyph} {status.provider_label} — {detail}")
        return "  ".join(segments)

    def queue_prefetch_all(self, query: str, *, session_id: str = "") -> None:
        """Queue background prefetch on all providers for the next turn."""
        providers = list(self._providers)
        if not providers:
            return
        if is_trivial_prompt(query):
            return

        def _run() -> None:
            for provider in providers:
                try:
                    provider.queue_prefetch(query, session_id=session_id)
                except Exception as e:
                    logger.debug("Memory provider '%s' queue_prefetch failed: %s", provider.name, e)

        self._submit_background(_run, kind="prefetch")

    # -- Sync ----------------------------------------------------------------

    @staticmethod
    def _provider_sync_accepts_messages(provider: MemoryProvider) -> bool:
        try:
            signature = inspect.signature(provider.sync_turn)
        except (TypeError, ValueError):
            return True
        params = list(signature.parameters.values())
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
            return True
        return "messages" in signature.parameters

    def sync_all(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """Sync a completed turn to all providers (background, non-blocking)."""
        providers = list(self._providers)
        if not providers:
            return
        if not user_content or not user_content.strip():
            return

        def _run() -> None:
            for provider in providers:
                try:
                    if messages is not None and self._provider_sync_accepts_messages(provider):
                        provider.sync_turn(
                            user_content, assistant_content,
                            session_id=session_id, messages=messages,
                        )
                    else:
                        provider.sync_turn(
                            user_content, assistant_content, session_id=session_id,
                        )
                except Exception as e:
                    logger.warning("Memory provider '%s' sync_turn failed: %s", provider.name, e)

        self._submit_background(_run)

    # -- Background dispatch -------------------------------------------------

    def _submit_background(self, fn, *, kind: str = "write") -> None:
        """Queue fn on the serialized worker and track its durability class."""
        executor = self._get_sync_executor()
        if executor is None:
            if self._shutting_down:
                logger.warning("Memory manager shutting down; rejecting late %s task", kind)
                return
            try:
                fn()
            except Exception as e:
                logger.debug("Inline memory background task failed: %s", e)
            return
        try:
            with self._sync_executor_lock:
                if self._shutting_down:
                    logger.warning("Memory manager shutting down; rejecting late %s task", kind)
                    return
                future = executor.submit(fn)
                self._background_futures[future] = kind
            future.add_done_callback(self._forget_background_future)
        except RuntimeError:
            if self._shutting_down:
                return
            try:
                fn()
            except Exception as e:
                logger.debug("Inline memory background task failed: %s", e)

    def _forget_background_future(self, future: Future) -> None:
        with self._sync_executor_lock:
            self._background_futures.pop(future, None)

    def _get_sync_executor(self) -> ThreadPoolExecutor | None:
        """Lazily create the single-worker background executor."""
        if self._shutting_down:
            return None
        if self._sync_executor is not None:
            return self._sync_executor
        with self._sync_executor_lock:
            if self._shutting_down:
                return None
            if self._sync_executor is None:
                self._sync_executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="mem-sync"
                )
            return self._sync_executor

    def flush_pending(self, timeout: float | None = None) -> bool:
        """Block until queued sync/prefetch work has drained."""
        executor = self._sync_executor
        if executor is None:
            return True
        try:
            fut = executor.submit(lambda: None)
        except RuntimeError:
            return True
        try:
            fut.result(timeout=timeout)
            return True
        except Exception:
            return False

    # -- Tools ---------------------------------------------------------------

    def get_all_tool_schemas(self) -> list[dict[str, Any]]:
        """Collect tool schemas from all providers."""
        schemas = []
        seen = set()
        for provider in self._providers:
            try:
                for raw_schema in provider.get_tool_schemas():
                    schema = normalize_tool_schema(raw_schema)
                    if schema is None:
                        continue
                    name = schema["name"]
                    if name not in seen:
                        schemas.append(schema)
                        seen.add(name)
            except Exception as e:
                logger.warning("Memory provider '%s' get_tool_schemas() failed: %s", provider.name, e)
        return schemas

    def get_all_tool_names(self) -> set[str]:
        return set(self._tool_to_provider.keys())

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self._tool_to_provider

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        """Route a tool call to the correct provider. Returns JSON string."""
        provider = self._tool_to_provider.get(tool_name)
        if provider is None:
            return tool_error(f"No memory provider handles tool '{tool_name}'")
        try:
            return provider.handle_tool_call(tool_name, args, **kwargs)
        except Exception as e:
            logger.error("Memory provider '%s' handle_tool_call(%s) failed: %s", provider.name, tool_name, e)
            return tool_error(f"Memory tool '{tool_name}' failed: {e}")

    # -- Lifecycle hooks -----------------------------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        for provider in self._providers:
            try:
                provider.on_turn_start(turn_number, message, **kwargs)
            except Exception as e:
                logger.debug("Memory provider '%s' on_turn_start failed: %s", provider.name, e)

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        for provider in self._providers:
            try:
                provider.on_session_end(messages)
            except Exception as e:
                logger.warning("Memory provider '%s' on_session_end failed: %s", provider.name, e)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        if not new_session_id:
            return
        if rewound:
            kwargs["rewound"] = True
        for provider in self._providers:
            try:
                provider.on_session_switch(
                    new_session_id, parent_session_id=parent_session_id, reset=reset, **kwargs
                )
            except Exception as e:
                logger.debug("Memory provider '%s' on_session_switch failed: %s", provider.name, e)

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        parts = []
        for provider in self._providers:
            try:
                result = provider.on_pre_compress(messages)
                if result and result.strip():
                    parts.append(result)
            except Exception as e:
                logger.debug("Memory provider '%s' on_pre_compress failed: %s", provider.name, e)
        return "\n\n".join(parts)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Notify external providers when the built-in memory tool writes."""
        for provider in self._providers:
            if provider.name == "builtin":
                continue
            try:
                provider.on_memory_write(action, target, content, metadata=dict(metadata or {}))
            except Exception as e:
                logger.debug("Memory provider '%s' on_memory_write failed: %s", provider.name, e)

    _MIRRORED_MEMORY_ACTIONS = {"add", "replace", "remove"}

    @staticmethod
    def _memory_tool_result_succeeded(result: Any) -> bool:
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                return False
        if not isinstance(result, dict):
            return False
        return result.get("success") is True

    def notify_memory_tool_write(
        self,
        tool_result: Any,
        tool_args: dict[str, Any],
        *,
        build_metadata: Any = None,
    ) -> None:
        """Mirror a built-in memory tool call to external providers."""
        if not self._memory_tool_result_succeeded(tool_result):
            return
        target = str(tool_args.get("target") or "memory")
        operations = tool_args.get("operations")
        if isinstance(operations, list) and operations:
            raw_operations = operations
        else:
            raw_operations = [{"action": tool_args.get("action"), "content": tool_args.get("content")}]

        for op in raw_operations:
            if not isinstance(op, dict):
                continue
            action = str(op.get("action") or "")
            if action not in self._MIRRORED_MEMORY_ACTIONS:
                continue
            try:
                metadata = dict(build_metadata() if build_metadata else {})
                self.on_memory_write(action, target, str(op.get("content") or ""), metadata=metadata)
            except Exception as e:
                logger.debug("notify_memory_tool_write failed for op %s: %s", action, e)

    def on_delegation(self, task: str, result: str, *, child_session_id: str = "", **kwargs) -> None:
        for provider in self._providers:
            try:
                provider.on_delegation(task, result, child_session_id=child_session_id, **kwargs)
            except Exception as e:
                logger.debug("Memory provider '%s' on_delegation failed: %s", provider.name, e)

    # -- Shutdown ------------------------------------------------------------

    def shutdown_all(self) -> None:
        """Shut down all providers (reverse order) with bounded drain."""
        self._shutting_down = True
        # Drain background work
        executor = self._sync_executor
        if executor is not None:
            try:
                for fut in list(self._background_futures):
                    try:
                        fut.result(timeout=_SYNC_DRAIN_TIMEOUT_S)
                    except Exception:
                        pass
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        # Shutdown providers in reverse order
        for provider in reversed(self._providers):
            try:
                provider.shutdown()
            except Exception as e:
                logger.debug("Memory provider '%s' shutdown failed: %s", provider.name, e)
