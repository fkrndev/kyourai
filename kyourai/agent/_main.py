"""Kyourai agent core — Pydantic AI agent wired with memory components.

The agent integrates all memory layers:
  1. Builtin file-based memory (MEMORY.md, USER.md) — always-on context
  2. Holographic HRR memory — structured facts with compositional retrieval
  3. Curator — background memory maintenance

The agent exposes memory tools to the model via Pydantic AI's tool system,
and injects memory context into the system prompt via the MemoryManager.

For team mode, the agent uses TeamMemoryRouter to scope memory operations
to the active user's private + shared memory spaces.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic_ai import Agent, RunContext, Tool

from kyourai.config import get_config_value
from kyourai.memory.manager import MemoryManager
from kyourai.memory.builtin import BuiltinMemoryProvider
from kyourai.memory.holographic.provider import HolographicMemoryProvider
from kyourai.memory.holographic.store import MemoryStore
from kyourai.memory import curator as curator_module
from kyourai.state import SessionDB
from kyourai.agent.error_classifier import classify_error, ErrorCategory
from kyourai.agent.retry_utils import retry_with_backoff
from kyourai.agent.rate_limit_tracker import RateLimitTracker
from kyourai.agent.empty_response_guard import guard_response, is_empty_response
from kyourai.agent.title_generator import generate_title, generate_title_sync
from kyourai.agent.subagent import SubagentDelegator

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """\
You are Kyourai (記憶雷), a memory-first AI coding agent.

You have access to a sophisticated memory system with two layers:
1. **Builtin memory** (the `memory` tool) — always-on context loaded from
   MEMORY.md and USER.md. Use this for persistent notes about the user and
   ongoing work. The snapshot is frozen at session start — mid-session
   writes are saved to disk but won't appear in your context until next
   session.

2. **Holographic memory** (the `fact_store` tool) — deep structured facts
   with entity resolution, trust scoring, and HRR compositional retrieval.
   Use `fact_store` for:
   - `add`: Store a fact the user would expect you to remember
   - `search`: Keyword lookup
   - `probe`: Entity recall — ALL facts about a person/thing
   - `related`: What connects to an entity? Structural adjacency
   - `reason`: Compositional — facts connected to MULTIPLE entities
   - `contradict`: Memory hygiene — find conflicting claims
   - `update/remove/list`: CRUD operations

3. **Fact feedback** (the `fact_feedback` tool) — rate facts after using
   them. Mark 'helpful' if accurate, 'unhelpful' if outdated. This trains
   the memory — good facts rise, bad facts sink.

You also have core tools for interacting with the machine:
- **terminal**: Execute shell commands (git, python, npm, etc.)
- **read_file**: Read file contents (source code, configs, docs)
- **web_search**: Search the web for current information

IMPORTANT: Before answering questions about the user or projects, ALWAYS
probe or reason first to recall relevant facts from holographic memory.
"""


class KyouraiAgent:
    """Kyourai agent — Pydantic AI agent with wired memory components.

    Usage:
        agent = KyouraiAgent(model="openai:gpt-4o")
        result = await agent.run("What do you know about my project?")
        print(result.output)

    For team mode:
        agent = KyouraiAgent(model="openai:gpt-4o", team_id="...", user_id="...")
    """

    def __init__(
        self,
        model: str = "openai:gpt-4o",
        *,
        session_id: str = "default",
        team_id: str | None = None,
        user_id: str | None = None,
        enable_curator: bool = True,
        enable_skills: bool = True,
        enable_cron: bool = True,
        skills_allowlist: list[str] | None = None,
        extra_instructions: str = "",
    ):
        # Resolve model through provider adapter system
        # (handles API keys, provider-specific config, fallbacks)
        from kyourai.providers import resolve_model, parse_model_string
        try:
            self.model = resolve_model(model)
            self._provider_name, self._model_name = parse_model_string(model)
        except ValueError:
            # Provider not configured (missing API key etc.) — pass through
            # to pydantic-ai as-is (e.g. TestModel, or user has key in env)
            self.model = model
            self._provider_name, self._model_name = parse_model_string(model)

        self.session_id = session_id
        self.team_id = team_id
        self.user_id = user_id
        self.enable_curator = enable_curator
        self.enable_skills = enable_skills
        self.enable_cron = enable_cron

        # Track last user activity for curator idle detection
        self._last_activity_ts: float = time.time()

        # Session state persistence (SQLite + FTS5)
        self.session_db: SessionDB | None = None
        try:
            self.session_db = SessionDB()
            source = "team" if team_id else "cli"
            # Model may be a non-string (TestModel, etc.) — coerce to str
            model_str = model if isinstance(model, str) else str(model)
            self.session_db.create_session(
                session_id,
                source=source,
                model=model_str,
                team_id=team_id or "",
                user_id=user_id or "",
            )
        except Exception as e:
            logger.warning("SessionDB init failed (session persistence disabled): %s", e)

        # Build memory manager with builtin + holographic providers
        self.memory_manager = MemoryManager()
        self._holographic_config: dict[str, Any] = get_config_value("memory.holographic", {}) or {}

        # Team memory router (if in team mode)
        self._team_router = None
        if team_id and user_id:
            from kyourai.team import TeamManager, TeamMemoryRouter
            self._team_manager = TeamManager()
            self._team_router = TeamMemoryRouter(self._team_manager, team_id, user_id)

        # Initialize memory in the appropriate scope
        self._init_memory()

        # Load skills
        self.skill_loader = None
        if enable_skills:
            self._init_skills(skills_allowlist)

        # Build the Pydantic AI agent (system prompt is frozen here —
        # byte-stable for the conversation lifetime for prompt caching)
        self._agent = self._build_agent(extra_instructions)
        # Pydantic AI stores system prompts as a list (dynamic + static)
        self._system_prompt: str = self._agent._system_prompts[0] if self._agent._system_prompts else ""

        # Context compression config
        self._model_context_limit: int = int(
            get_config_value("agent.context_limit", 128_000)
        )
        from kyourai.context import estimate_tokens
        self._system_prompt_tokens: int = estimate_tokens(self._system_prompt)

        # Rate limit tracker (per-provider sliding window)
        self._rate_limiter = RateLimitTracker()

        # Subagent delegator (lazy init — only when first needed)
        self._delegator: SubagentDelegator | None = None

        # Title generation flag (only auto-title on first turn)
        self._titled: bool = False

        # Output verification (opt-in via config)
        self._verify_output: bool = bool(get_config_value("agent.verify_output", False))

        # Plugin manager (lazy init — only when first needed)
        self._plugin_manager: Any = None

        # Curator background runner
        self._curator_runner = None
        if enable_curator:
            self._init_curator()

        # Cron scheduler
        self._cron_scheduler = None
        if enable_cron:
            self._init_cron()

    def _init_memory(self) -> None:
        """Initialize memory providers in the correct scope (private/shared/team)."""
        if self._team_router:
            # Team mode: initialize in private + shared contexts
            def init_private():
                builtin = BuiltinMemoryProvider()
                builtin.initialize(self.session_id)
                holo = HolographicMemoryProvider(config=self._holographic_config)
                holo.initialize(self.session_id)
                return builtin, holo

            def init_shared():
                builtin = BuiltinMemoryProvider()
                builtin.initialize(self.session_id + "-shared")
                holo = HolographicMemoryProvider(config=self._holographic_config)
                holo.initialize(self.session_id + "-shared")
                return builtin, holo

            self._private_builtin, self._private_holo = self._team_router.with_private(init_private)
            self._shared_builtin, self._shared_holo = self._team_router.with_shared(init_shared)

            # Register the shared providers with the manager (shared is the default context)
            self.memory_manager.add_provider(self._shared_builtin)
            self.memory_manager.add_provider(self._shared_holo)
        else:
            # Solo mode: single memory space
            builtin = BuiltinMemoryProvider()
            builtin.initialize(self.session_id)
            holo = HolographicMemoryProvider(config=self._holographic_config)
            holo.initialize(self.session_id)

            self.memory_manager.add_provider(builtin)
            self.memory_manager.add_provider(holo)

    def _init_curator(self) -> None:
        """Initialize the curator background runner."""
        # Get the holographic store from the provider
        holo_provider = None
        for p in self.memory_manager.providers:
            if isinstance(p, HolographicMemoryProvider):
                holo_provider = p
                break
        if holo_provider and holo_provider._store:
            curator_config = get_config_value("curator", {}) or {}
            min_idle_hours = float(curator_config.get("min_idle_hours", 2))
            min_idle_seconds = min_idle_hours * 3600.0

            def is_idle() -> bool:
                return (time.time() - self._last_activity_ts) >= min_idle_seconds

            self._curator_runner = curator_module.CuratorBackgroundRunner(
                store=holo_provider._store,
                config=curator_config,
                is_idle_fn=is_idle,
            )

    def _init_skills(self, allowlist: list[str] | None) -> None:
        """Initialize the skills loader."""
        from kyourai.skills import SkillLoader
        self.skill_loader = SkillLoader(allowlist=allowlist)
        self.skill_loader.load_all()

    def _init_cron(self) -> None:
        """Initialize the cron scheduler."""
        from kyourai.cron import CronScheduler

        def agent_run_fn(prompt: str, skill: str) -> str:
            # Run agent synchronously in a new event loop
            import asyncio
            try:
                loop = asyncio.new_event_loop()
                output = loop.run_until_complete(self.run(prompt))
                return output
            except Exception as e:
                logger.warning("Cron agent_run failed: %s", e)
                return f"Error: {e}"

        def curator_fn() -> dict:
            holo_provider = None
            for p in self.memory_manager.providers:
                if isinstance(p, HolographicMemoryProvider):
                    holo_provider = p
                    break
            if holo_provider and holo_provider._store:
                return curator_module.run_curator(holo_provider._store, force=True)
            return {"error": "no store"}

        self._cron_scheduler = CronScheduler(
            agent_run_fn=agent_run_fn,
            curator_fn=curator_fn,
        )
        self._cron_scheduler.start()

    def _build_agent(self, extra_instructions: str) -> Agent:
        """Build the Pydantic AI agent with memory tools and system prompt.

        Uses the dynamic prompt builder to assemble the system prompt from:
        base identity, memory context, tool descriptions, skills, user
        preferences, verification config, and extra instructions.

        The system prompt is built ONCE here and stays byte-stable for the
        conversation lifetime (prompt caching invariant).
        """
        from kyourai.agent.prompt_builder import build_system_prompt

        # Gather tool schemas for the prompt builder
        from kyourai.tools import discover_core_tools
        all_tool_schemas = self.memory_manager.get_all_tool_schemas() + discover_core_tools()

        # Gather skills prompt
        skills_prompt = ""
        if self.skill_loader:
            skills_prompt = self.skill_loader.build_prompt_block() or ""

        # Build the system prompt dynamically
        system_prompt = build_system_prompt(
            memory_prompt=self.memory_manager.build_system_prompt(),
            skills_prompt=skills_prompt,
            tool_schemas=all_tool_schemas,
            config=self._config if hasattr(self, "_config") else None,
            extra_instructions=extra_instructions,
            verify_output=bool(get_config_value("agent.verify_output", False)),
        )

        # Build tools from memory manager schemas
        tools = self._build_tools()

        # Create the Pydantic AI agent
        agent = Agent(
            model=self.model,
            system_prompt=system_prompt,
            tools=tools,
            deps_type=KyouraiAgent,  # pass self as deps
            name="kyourai",
        )
        return agent

    def _build_tools(self) -> list[Tool]:
        """Convert memory + core tool schemas into Pydantic AI Tool objects."""
        # Memory tools (memory, fact_store, fact_feedback)
        schemas = self.memory_manager.get_all_tool_schemas()
        tools = []
        for schema in schemas:
            tool = self._schema_to_tool(schema, source="memory")
            if tool:
                tools.append(tool)

        # Core tools (terminal, read_file, web_search)
        from kyourai.tools import discover_core_tools
        core_schemas = discover_core_tools()
        for schema in core_schemas:
            tool = self._schema_to_tool(schema, source="core")
            if tool:
                tools.append(tool)

        return tools

    def _schema_to_tool(self, schema: dict[str, Any], *, source: str = "memory") -> Tool | None:
        """Convert a JSON schema dict into a Pydantic AI Tool.

        Args:
            schema: Tool JSON schema with 'name', 'description', 'parameters'
            source: "memory" for memory manager tools, "core" for core tools
        """
        tool_name = schema["name"]
        tool_desc = schema.get("description", "")
        params_schema = schema.get("parameters", {})

        from pydantic import BaseModel, create_model
        from typing import Optional

        properties = params_schema.get("properties", {})
        required = set(params_schema.get("required", []))

        fields = {}
        type_map = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "array": list,
        }

        for prop_name, prop_schema in properties.items():
            prop_type = prop_schema.get("type", "string")
            py_type = type_map.get(prop_type, str)
            if prop_name in required:
                fields[prop_name] = (py_type, ...)
            else:
                fields[prop_name] = (Optional[py_type], None)

        # Special handling for 'action' enum — make it a Literal
        if "action" in properties:
            from typing import Literal
            enum_values = properties["action"].get("enum", [])
            if enum_values:
                if "action" in required:
                    fields["action"] = (Literal[tuple(enum_values)], ...)
                else:
                    fields["action"] = (Optional[Literal[tuple(enum_values)]], None)

        try:
            ParamModel = create_model(f"{tool_name}_params", **fields)
        except Exception as e:
            logger.warning("Failed to create model for tool %s: %s", tool_name, e)
            return None

        if source == "core":
            # Core tools dispatch to their own handler (no memory manager)
            from kyourai.tools import get_tool_handler

            core_handler = get_tool_handler(tool_name)
            if core_handler is None:
                logger.warning("No handler found for core tool %s", tool_name)
                return None

            def make_core_handler(name: str, fn):
                def handler(ctx: RunContext["KyouraiAgent"], **kwargs) -> str:
                    args = {k: v for k, v in kwargs.items() if v is not None}
                    return fn(**args)
                return handler

            handler = make_core_handler(tool_name, core_handler)
        else:
            # Memory tools dispatch to memory manager
            def make_handler(name: str):
                def handler(ctx: RunContext["KyouraiAgent"], **kwargs) -> str:
                    args = {k: v for k, v in kwargs.items() if v is not None}
                    return ctx.deps.memory_manager.handle_tool_call(name, args)
                return handler

            handler = make_handler(tool_name)

        handler.__name__ = tool_name
        handler.__doc__ = tool_desc

        return Tool(handler, name=tool_name, description=tool_desc, takes_ctx=True)

    # -- Public API ----------------------------------------------------------

    async def run(self, user_prompt: str, *, message_history: list | None = None) -> str:
        """Run the agent on a user prompt. Returns the agent's output string.

        Includes: prompt caching (byte-stable system prompt), context
        compression (if message_history exceeds threshold), rate limiting,
        retry with backoff (on transient errors), empty response guard,
        and auto title generation (on first turn).
        """
        # Track user activity for curator idle detection
        self._last_activity_ts = time.time()

        # Persist user message to session DB
        if self.session_db:
            try:
                self.session_db.add_message(self.session_id, role="user", content=user_prompt)
            except Exception:
                pass

        # Compress message history if needed (preserves cache — only touches
        # message_history, never the system prompt)
        if message_history:
            from kyourai.context import compress_if_needed
            message_history = await compress_if_needed(
                message_history,
                model=self.model,
                model_context=self._model_context_limit,
                system_prompt_tokens=self._system_prompt_tokens,
            )

        # Maybe run curator in background
        if self._curator_runner:
            self._curator_runner.maybe_run()

        # Rate limit check (non-blocking — just records)
        provider = str(self.model).split(":")[0] if isinstance(self.model, str) else "default"
        await self._rate_limiter.wait_if_needed_async(provider)

        # Run with retry + empty response guard
        output = await self._run_with_retry(user_prompt, message_history)

        # Record successful request for rate limiting
        self._rate_limiter.record(provider)

        # Output verification (if enabled)
        if self._verify_output:
            from kyourai.agent.verification import verify_output, format_verification_warning
            verification = verify_output(output)
            if verification.has_warnings:
                output += format_verification_warning(verification)

        # Run post_run plugin hooks (can modify output)
        if self._plugin_manager:
            hook_results = self._plugin_manager.run_hooks("post_run", output)
            # If any hook returned a modified output, use the last one
            for result in hook_results:
                if isinstance(result, str) and result:
                    output = result

        # Persist assistant response to session DB
        if self.session_db:
            try:
                self.session_db.add_message(
                    self.session_id, role="assistant", content=output
                )
            except Exception:
                pass

        # Auto-generate title on first turn
        if not self._titled and self.session_db:
            self._titled = True
            try:
                title = generate_title_sync(user_prompt, output)
                self.session_db.update_session(self.session_id, title=title)
            except Exception:
                pass

        return output

    async def _run_with_retry(
        self,
        user_prompt: str,
        message_history: list | None = None,
    ) -> str:
        """Run the agent with retry on transient errors and empty response guard."""
        max_empty_retries = 2

        async def _call():
            result = await self._agent.run(
                user_prompt, deps=self, message_history=message_history
            )
            return result.output

        for empty_attempt in range(max_empty_retries + 1):
            try:
                output = await retry_with_backoff(_call, max_retries=3)
            except Exception as e:
                classification = classify_error(e)
                if classification.category == ErrorCategory.FATAL:
                    raise
                # For retryable errors that exhausted retries, return error message
                logger.warning("Agent run failed after retries: %s", e)
                return f"Error: {e}"

            # Empty response guard
            guarded, should_retry = guard_response(output, retry_count=empty_attempt)
            if not should_retry:
                return guarded
            # Retry with empty response guard
            logger.warning("Empty response, retrying (attempt %d)", empty_attempt + 1)

        return guarded  # type: ignore[possibly-undefined]

    async def run_stream(self, user_prompt: str, *, message_history: list | None = None):
        """Run the agent with streaming output.

        Same caching + compression + rate limiting as run().
        Note: retry is not applied to streaming (partial output can't be
        retried cleanly). Empty response guard is applied after collection.
        """
        # Track user activity for curator idle detection
        self._last_activity_ts = time.time()

        # Persist user message to session DB
        if self.session_db:
            try:
                self.session_db.add_message(self.session_id, role="user", content=user_prompt)
            except Exception:
                pass

        # Compress message history if needed
        if message_history:
            from kyourai.context import compress_if_needed
            message_history = await compress_if_needed(
                message_history,
                model=self.model,
                model_context=self._model_context_limit,
                system_prompt_tokens=self._system_prompt_tokens,
            )

        if self._curator_runner:
            self._curator_runner.maybe_run()

        # Rate limit check
        provider = str(self.model).split(":")[0] if isinstance(self.model, str) else "default"
        await self._rate_limiter.wait_if_needed_async(provider)

        collected = []
        async with self._agent.run_stream(user_prompt, deps=self, message_history=message_history) as result:
            async for chunk in result.stream_text(delta=True):
                collected.append(chunk)
                yield chunk

        self._rate_limiter.record(provider)

        # Persist streamed assistant response to session DB
        full_output = "".join(collected)
        if self.session_db and full_output:
            try:
                self.session_db.add_message(
                    self.session_id, role="assistant", content=full_output
                )
            except Exception:
                pass

        # Auto-generate title on first turn
        if not self._titled and self.session_db and full_output:
            self._titled = True
            try:
                title = generate_title_sync(user_prompt, full_output)
                self.session_db.update_session(self.session_id, title=title)
            except Exception:
                pass

    def sync_turn(self, user_content: str, assistant_content: str) -> None:
        """Sync a turn to memory (non-blocking).

        Also persists the turn to the session DB for history/search.
        """
        self.memory_manager.sync_all(user_content, assistant_content, session_id=self.session_id)
        if self.session_db:
            try:
                self.session_db.add_turn(
                    self.session_id, user_content, assistant_content
                )
            except Exception:
                pass

    def get_message_history(self, *, limit: int = 50) -> list[dict[str, str]]:
        """Get message history for this session (for rewind/continuation)."""
        if not self.session_db:
            return []
        return self.session_db.get_message_history(self.session_id, limit=limit)

    async def delegate(self, task: str, **kwargs: Any) -> Any:
        """Delegate a task to a subagent.

        Creates a subagent with its own session but shared memory context.
        Useful for parallel task execution or isolating complex subtasks.

        Args:
            task: Task description for the subagent
            **kwargs: Passed to SubagentDelegator.delegate()

        Returns:
            DelegationResult with the subagent's output
        """
        if self._delegator is None:
            self._delegator = SubagentDelegator(self)
        return await self._delegator.delegate(task, **kwargs)

    async def delegate_batch(self, tasks: list[str], **kwargs: Any) -> list[Any]:
        """Delegate multiple tasks to subagents in parallel.

        Args:
            tasks: List of task descriptions
            **kwargs: Passed to SubagentDelegator.delegate_batch()

        Returns:
            List of DelegationResults (in same order as tasks)
        """
        if self._delegator is None:
            self._delegator = SubagentDelegator(self)
        return await self._delegator.delegate_batch(tasks, **kwargs)

    def load_plugins(self) -> list[Any]:
        """Discover and load plugins from ~/.kyourai/plugins/.

        Returns:
            List of PluginInfo for all discovered plugins
        """
        from kyourai.agent.plugin_system import PluginManager
        if self._plugin_manager is None:
            self._plugin_manager = PluginManager()
        return self._plugin_manager.load_all(self)

    def get_plugins(self) -> list[Any]:
        """Get info about discovered plugins."""
        if self._plugin_manager is None:
            return []
        return self._plugin_manager.get_plugin_info()

    def shutdown(self) -> None:
        """Shutdown all memory providers, curator, cron scheduler, and session DB."""
        if self._cron_scheduler:
            self._cron_scheduler.stop()
        if self._curator_runner:
            self._curator_runner.wait(timeout=5.0)
        self.memory_manager.shutdown_all()
        if self.session_db:
            try:
                self.session_db.end_session(self.session_id)
                self.session_db.close()
            except Exception:
                pass
        if self._plugin_manager:
            self._plugin_manager.shutdown()
        self._rate_limiter.reset()

    @property
    def recall_indicator(self) -> str:
        """Get the recall indicator for display."""
        return self.memory_manager.describe_recall()
