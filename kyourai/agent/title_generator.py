"""Title generation — auto-title sessions from the first user exchange.

Generates a concise title (3-5 words) from the first user message +
assistant response. Uses the same model the agent uses — no separate
summarizer. Falls back to a truncated first message if title generation
fails.

Inspired by Hermes' title_generator.py (32k LOC) but radically simpler:
no async queue, no background generation, no title source tracking.
Just a synchronous call that returns a title string.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 60
FALLBACK_TITLE_LENGTH = 40


def _fallback_title(user_message: str) -> str:
    """Generate a fallback title from the first user message."""
    title = user_message.strip().replace("\n", " ")
    if len(title) > FALLBACK_TITLE_LENGTH:
        title = title[:FALLBACK_TITLE_LENGTH].rstrip() + "..."
    return title or "Untitled Session"


_TITLE_SYSTEM_PROMPT = """\
You generate concise session titles. Rules:
- 3-5 words, maximum 60 characters
- Capture the main topic or intent
- No quotes, no punctuation at the end
- Lowercase except proper nouns
- Example: "deploy kubernetes helm chart"
- Example: "fix memory leak in worker"
- Example: "python data analysis pipeline"
"""


async def generate_title(
    user_message: str,
    assistant_message: str,
    model: Any = None,
) -> str:
    """Generate a concise title from the first exchange.

    Args:
        user_message: First user message
        assistant_message: First assistant response
        model: Pydantic AI model instance (if None, uses fallback)

    Returns:
        Title string (3-5 words, max 60 chars)
    """
    if not user_message:
        return "Untitled Session"

    # If no model, use fallback (truncated first message)
    if model is None:
        return _fallback_title(user_message)

    # Build the title generation prompt
    prompt = (
        f"Generate a 3-5 word title for this conversation.\n\n"
        f"User: {user_message[:500]}\n\n"
        f"Assistant: {assistant_message[:500]}\n\n"
        f"Title:"
    )

    try:
        from pydantic_ai import Agent

        title_agent = Agent(
            model=model,
            system_prompt=_TITLE_SYSTEM_PROMPT,
        )
        result = await title_agent.run(prompt)
        title = result.output.strip()

        # Clean up: remove quotes, trailing punctuation
        title = title.strip("\"'`.,!?;:")
        title = title.replace("\n", " ").strip()

        # Enforce length limit
        if len(title) > MAX_TITLE_LENGTH:
            title = title[:MAX_TITLE_LENGTH].rstrip() + "..."

        # If title is too short or empty, use fallback
        if len(title) < 3:
            return _fallback_title(user_message)

        return title

    except Exception as e:
        logger.warning("Title generation failed: %s — using fallback", e)
        return _fallback_title(user_message)


def generate_title_sync(
    user_message: str,
    assistant_message: str,
) -> str:
    """Synchronous fallback title generation (no LLM call).

    Simply truncates the first user message.
    """
    return _fallback_title(user_message)
