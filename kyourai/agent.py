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
        self.model = model
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
            self.session_db.create_session(
                session_id,
                source=source,
                model=model,
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

        # Build the Pydantic AI agent
        self._agent = self._build_agent(extra_instructions)

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
        """Build the Pydantic AI agent with memory tools and system prompt."""
        # Build system prompt from default + memory providers + skills
        memory_prompt = self.memory_manager.build_system_prompt()
        system_prompt = DEFAULT_SYSTEM_PROMPT
        if memory_prompt:
            system_prompt += "\n\n" + memory_prompt
        if self.skill_loader:
            skills_prompt = self.skill_loader.build_prompt_block()
            if skills_prompt:
                system_prompt += "\n\n" + skills_prompt
        if extra_instructions:
            system_prompt += "\n\n" + extra_instructions

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
        """Convert memory manager tool schemas into Pydantic AI Tool objects."""
        schemas = self.memory_manager.get_all_tool_schemas()
        tools = []
        for schema in schemas:
            tool = self._schema_to_tool(schema)
            if tool:
                tools.append(tool)
        return tools

    def _schema_to_tool(self, schema: dict[str, Any]) -> Tool | None:
        """Convert a JSON schema dict into a Pydantic AI Tool.

        Since the memory tools use dynamic JSON schemas (not Pydantic models),
        we create wrapper functions that accept a dict and dispatch to the
        memory manager.
        """
        tool_name = schema["name"]
        tool_desc = schema.get("description", "")
        params_schema = schema.get("parameters", {})

        # Build a Pydantic model dynamically from the parameters schema
        # For simplicity, we use a dict-based approach with Pydantic AI's
        # Tool function registration
        from pydantic import BaseModel, create_model
        from typing import Optional

        # Create a Pydantic model from the JSON schema properties
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
        """Run the agent on a user prompt. Returns the agent's output string."""
        # Track user activity for curator idle detection
        self._last_activity_ts = time.time()

        # Persist user message to session DB
        if self.session_db:
            try:
                self.session_db.add_message(self.session_id, role="user", content=user_prompt)
            except Exception:
                pass

        # Prefetch memory context for this query
        prefetch = self.memory_manager.prefetch_all(user_prompt)
        full_prompt = user_prompt
        if prefetch:
            full_prompt = f"{prefetch}\n\n{user_prompt}"

        # Maybe run curator in background
        if self._curator_runner:
            self._curator_runner.maybe_run()

        result = await self._agent.run(full_prompt, deps=self, message_history=message_history)

        # Persist assistant response to session DB
        if self.session_db:
            try:
                self.session_db.add_message(
                    self.session_id, role="assistant", content=result.output
                )
            except Exception:
                pass

        return result.output

    async def run_stream(self, user_prompt: str, *, message_history: list | None = None):
        """Run the agent with streaming output."""
        # Track user activity for curator idle detection
        self._last_activity_ts = time.time()

        # Persist user message to session DB
        if self.session_db:
            try:
                self.session_db.add_message(self.session_id, role="user", content=user_prompt)
            except Exception:
                pass

        prefetch = self.memory_manager.prefetch_all(user_prompt)
        full_prompt = user_prompt
        if prefetch:
            full_prompt = f"{prefetch}\n\n{user_prompt}"

        if self._curator_runner:
            self._curator_runner.maybe_run()

        collected = []
        async with self._agent.run_stream(full_prompt, deps=self, message_history=message_history) as result:
            async for chunk in result.stream_text(delta=True):
                collected.append(chunk)
                yield chunk

        # Persist streamed assistant response to session DB
        if self.session_db and collected:
            try:
                self.session_db.add_message(
                    self.session_id, role="assistant", content="".join(collected)
                )
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

    @property
    def recall_indicator(self) -> str:
        """Get the recall indicator for display."""
        return self.memory_manager.describe_recall()
