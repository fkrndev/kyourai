"""Abstract base class for pluggable memory providers.

Ported from Hermes Agent (agent/memory_provider.py), adapted for Kyourai.

Memory providers give the agent persistent recall across sessions.
The MemoryManager enforces a one-external-provider limit to prevent
tool schema bloat and conflicting memory backends.

Lifecycle (called by MemoryManager, wired in the agent loop):
  initialize()          — connect, create resources, warm up
  system_prompt_block()  — static text for the system prompt
  prefetch(query)        — background recall before each turn
  sync_turn(user, asst)  — async write after each turn
  get_tool_schemas()     — tool schemas to expose to the model
  handle_tool_call()     — dispatch a tool call
  shutdown()             — clean exit

Optional hooks (override to opt in):
  on_turn_start(turn, message, **kwargs) — per-turn tick with runtime context
  on_session_end(messages)               — end-of-session extraction
  on_session_switch(new_session_id, **kwargs) — mid-process session_id rotation
  on_pre_compress(messages) -> str       — extract before context compression
  on_memory_write(action, target, content, metadata=None) — mirror built-in writes
  on_delegation(task, result, **kwargs)  — parent-side observation of subagent work
  backup_paths() -> list[str]            — extra on-disk paths for backup
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Default glyph for the deterministic memory indicators. Providers override
# per-status with their own brand mark.
INDICATOR_GLYPH = "🧠"


@dataclass(frozen=True)
class RecallStatus:
    """Summary of what a provider's most recent prefetch injected this turn.

    Returned by :meth:`MemoryProvider.recall_status` so the agent can emit a
    deterministic, model-independent "memory was used" indicator. ``count`` is
    the number of discrete memories injected; ``0`` means content was injected
    but has no discrete count (e.g. a synthesized reflect answer), which the
    indicator renders generically rather than as "0 memories". ``glyph`` is the
    brand mark the indicator leads with.
    """

    provider_label: str
    count: int
    glyph: str = INDICATOR_GLYPH


# Prompts that carry no semantic signal — trivial acknowledgements, greetings,
# slash commands, empty input. The alternation is anchored and may only be
# followed by whitespace or punctuation, so words that merely START with a
# trivial word ("k8s", "yolo", "note") do NOT match, while trailing-punctuation
# variants ("hi!", "hey.", "thanks :)", "done???") do.
TRIVIAL_PROMPT_RE = re.compile(
    r'^(yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|'
    r'hi|hey|hello|yo|sup|'
    r'continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k)'
    r'[\s!?.:;,"' + "'" + r'~\u2018\u2019\u201c\u201d\u2014\u2013\u2026()\[\]{}<>*&^%$#@!+=`\u00a0]*$',
    re.IGNORECASE,
)


def is_trivial_prompt(text: str | None) -> bool:
    """Return True if a user prompt is too trivial to warrant memory recall.

    Empty/whitespace-only input, slash commands, and bare greetings or
    acknowledgements (with optional trailing punctuation) all count as
    trivial. Callers use this to skip memory-provider prefetch/injection
    on turns that carry no semantic signal — saving a blocking network
    round-trip and preventing stale user-model context from derailing
    one-word replies.
    """
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    if stripped.startswith("/"):
        return True
    return bool(TRIVIAL_PROMPT_RE.match(stripped))


class MemoryProvider(ABC):
    """Abstract base class for memory providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier (e.g. 'builtin', 'holographic', 'honcho')."""

    # -- Core lifecycle (implement these) ------------------------------------

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if configured, has credentials, and is ready.

        Called during agent init to decide whether to activate the provider.
        Should not make network calls — just check config and installed deps.
        """

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize for a session.

        Called once at agent startup. May create resources (tables, indexes),
        establish connections, start background threads, etc.

        kwargs always include:
          - kyourai_home (str): The active KYOURAI_HOME directory path.
          - platform (str): "cli", "telegram", "discord", "cron", etc.

        kwargs may also include:
          - agent_context (str): "primary", "subagent", "cron", or "flush".
            Providers should skip writes for non-primary contexts.
          - agent_identity (str): Profile name for per-profile scoping.
          - user_id (str): Platform user identifier (team/gateway sessions).
          - parent_session_id (str): For subagents, the parent's session_id.
        """

    def unavailable_reason(self) -> str:
        """Actionable reason this provider reports unavailable, for the caller.

        Return a short, user-facing hint (e.g. which package to install) so
        the caller's "provider unavailable" warning can surface it.
        """
        return ""

    def system_prompt_block(self) -> str:
        """Return text to include in the system prompt.

        Called during system prompt assembly. Return empty string to skip.
        This is for STATIC provider info (instructions, status). Prefetched
        recall context is injected separately via prefetch().
        """
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context for the upcoming turn.

        Called before each API call. Return formatted text to inject as
        context, or empty string if nothing relevant. Implementations
        should be fast — use background threads for the actual recall
        and return cached results here.
        """
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue a background recall for the NEXT turn.

        Called after each turn completes. The result will be consumed
        by prefetch() on the next turn. Default is no-op — providers
        that do background prefetching should override this.
        """

    def recall_status(self) -> RecallStatus | None:
        """Describe what the most recent prefetch injected, for the UI.

        Return None (the default) when this provider injected nothing this
        turn or does not want a visible indicator.
        """
        return None

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """Persist a completed turn to the backend.

        Called after each turn. Should be non-blocking — queue for
        background processing if the backend has latency.

        ``messages`` is the conversation message list as of the completed
        turn, including any assistant tool calls and tool results.
        """

    @abstractmethod
    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return tool schemas this provider exposes.

        Each schema follows the OpenAI function calling format:
        {"name": "...", "description": "...", "parameters": {...}}

        Return empty list if this provider has no tools (context-only).
        """

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        """Handle a tool call for one of this provider's tools.

        Must return a JSON string (the tool result).
        Only called for tool names returned by get_tool_schemas().
        """
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

    def shutdown(self) -> None:
        """Clean shutdown — flush queues, close connections."""

    # -- Optional hooks (override to opt in) ---------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Called at the start of each turn with the user message.

        kwargs may include: remaining_tokens, model, platform, tool_count.
        """

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """Called when a session ends (explicit exit or timeout).

        Use for end-of-session fact extraction, summarization, etc.
        NOT called after every turn — only at actual session boundaries.
        """

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        """Called when the agent switches session_id mid-process.

        Fires on /resume, /branch, /reset, /new, and context compression —
        any path that reassigns session_id without tearing the provider down.

        reset=True when this is a genuinely new conversation.
        rewound=True if session_id is unchanged but transcript was truncated.
        """

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """Called before context compression discards old messages.

        Return text to include in the compression summary prompt so the
        compressor preserves provider-extracted insights. Return empty
        string for no contribution.
        """
        return ""

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Mirror built-in memory tool writes to this provider's backend.

        Called when the built-in file memory tool performs add/replace/remove.
        External providers use this to keep their vector/semantic store in sync
        with the curated file memory.
        """

    def on_delegation(self, task: str, result: str, *, child_session_id: str = "", **kwargs) -> None:
        """Called on the PARENT agent when a subagent completes.

        The parent's memory provider gets the task+result pair as an
        observation of what was delegated and what came back.
        """

    def get_config_schema(self) -> list[dict[str, Any]]:
        """Return config fields this provider needs for setup.

        Used by 'kyourai setup' to walk the user through configuration.
        Each field is a dict with:
          key, description, secret, required, default, choices, type,
          env_var, url
        """
        return []

    def save_config(self, values: dict[str, Any], kyourai_home: str) -> None:
        """Write non-secret config to the provider's native location.

        Called by 'kyourai setup' after the user provides values.
        """

    def backup_paths(self) -> list[str]:
        """Extra on-disk paths to include in `kyourai backup`."""
        return []
