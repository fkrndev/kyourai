"""Dynamic system prompt builder — assemble system prompt from components.

Replaces the static DEFAULT_SYSTEM_PROMPT with a dynamic builder that
includes:
  - Base agent identity + instructions
  - Memory system description (from memory manager)
  - Skills list (from skill loader)
  - Tool descriptions (from registered tools)
  - User preferences (from config)
  - Extra instructions (passed at init)

The system prompt is built ONCE at agent init and stays byte-stable for
the conversation lifetime (prompt caching invariant). This module just
makes the construction modular and data-driven instead of hardcoded.

Inspired by Hermes' prompt_builder.py (125k LOC) but radically simpler.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base prompt — agent identity and core instructions
# ---------------------------------------------------------------------------

_BASE_PROMPT = """\
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


# ---------------------------------------------------------------------------
# Tool descriptions — auto-generated from tool schemas
# ---------------------------------------------------------------------------


def _build_tool_section(tool_schemas: list[dict[str, Any]]) -> str:
    """Build the tools section of the system prompt."""
    if not tool_schemas:
        return ""

    lines = ["You have the following tools available:"]

    for schema in tool_schemas:
        name = schema.get("name", "")
        desc = schema.get("description", "")
        # Truncate long descriptions
        if len(desc) > 100:
            desc = desc[:97] + "..."
        lines.append(f"- **{name}**: {desc}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Skills section
# ---------------------------------------------------------------------------


def _build_skills_section(skills_prompt: str | None) -> str:
    """Include the skills prompt block if available."""
    if not skills_prompt:
        return ""
    return f"\n\n## Available Skills\n\n{skills_prompt}"


# ---------------------------------------------------------------------------
# User preferences section
# ---------------------------------------------------------------------------


def _build_preferences_section(config: dict[str, Any] | None) -> str:
    """Build user preferences section from config."""
    if not config:
        return ""

    prefs: list[str] = []

    # Language preference
    lang = config.get("agent", {}).get("language")
    if lang and lang != "en":
        prefs.append(f"- Respond in {lang} when possible")

    # Response style
    style = config.get("agent", {}).get("response_style")
    if style:
        prefs.append(f"- Response style: {style}")

    # Code style
    code_style = config.get("agent", {}).get("code_style")
    if code_style:
        prefs.append(f"- Code style: {code_style}")

    # Custom instructions
    custom = config.get("agent", {}).get("custom_instructions")
    if custom:
        prefs.append(f"- {custom}")

    if not prefs:
        return ""

    return "\n\n## User Preferences\n\n" + "\n".join(prefs)


# ---------------------------------------------------------------------------
# Verification section
# ---------------------------------------------------------------------------


def _build_verification_section(verify_enabled: bool) -> str:
    """Build verification instructions if enabled."""
    if not verify_enabled:
        return ""

    return (
        "\n\n## Output Verification\n\n"
        "Your output may be verified for accuracy. When you claim tests pass, "
        "builds succeed, or files were created, these claims will be checked. "
        "Only make claims you have verified with actual tool calls."
    )


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------


def build_system_prompt(
    *,
    memory_prompt: str = "",
    skills_prompt: str = "",
    tool_schemas: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
    extra_instructions: str = "",
    verify_output: bool = False,
    coding_context: str = "",
) -> str:
    """Assemble the complete system prompt from all components.

    This is called ONCE at agent init. The result stays byte-stable for
    the conversation lifetime (prompt caching invariant).

    Args:
        memory_prompt: System prompt from memory manager (builtin + holographic)
        skills_prompt: Skills prompt block from skill loader
        tool_schemas: List of tool schemas for tool descriptions
        config: User config dict for preferences
        extra_instructions: Extra instructions passed at init
        verify_output: Whether output verification is enabled
        coding_context: Coding context prompt section (git, language, framework)

    Returns:
        Complete system prompt string
    """
    parts: list[str] = [_BASE_PROMPT]

    # Memory section
    if memory_prompt:
        parts.append(f"\n\n{memory_prompt}")

    # Coding context (detected from working directory)
    if coding_context:
        parts.append(f"\n\n{coding_context}")

    # Tools section
    if tool_schemas:
        tool_section = _build_tool_section(tool_schemas)
        if tool_section:
            parts.append(f"\n\n## Tools\n\n{tool_section}")

    # Skills section
    skills_section = _build_skills_section(skills_prompt)
    if skills_section:
        parts.append(skills_section)

    # User preferences
    prefs_section = _build_preferences_section(config)
    if prefs_section:
        parts.append(prefs_section)

    # Verification
    verify_section = _build_verification_section(verify_output)
    if verify_section:
        parts.append(verify_section)

    # Extra instructions (always last — user override)
    if extra_instructions:
        parts.append(f"\n\n## Additional Instructions\n\n{extra_instructions}")

    return "".join(parts)
