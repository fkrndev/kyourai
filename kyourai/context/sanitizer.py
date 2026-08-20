"""Message sanitization — clean messages before sending to model.

Ensures message history is well-formed before sending to the LLM:
  - Strict role alternation (no two same-role messages in a row)
  - Remove empty messages
  - Merge consecutive same-role messages
  - Truncate excessively long messages
  - Strip control characters
  - Ensure system prompt is first (if present)
  - Validate message structure

This is critical for:
  - Prompt caching (alternation invariant)
  - API compliance (some providers reject malformed messages)
  - Cost (empty/duplicate messages waste tokens)

Usage:
  from kyourai.context.sanitizer import sanitize_messages
  clean = sanitize_messages(raw_messages)
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# Maximum message content length (chars) — truncate beyond this
MAX_MESSAGE_LENGTH = 32_000

# Control characters to strip (except newline, tab, carriage return)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def sanitize_messages(
    messages: list[dict[str, str]],
    *,
    max_length: int = MAX_MESSAGE_LENGTH,
    merge_consecutive: bool = True,
    strip_control: bool = True,
    ensure_system_first: bool = True,
) -> list[dict[str, str]]:
    """Sanitize a list of chat messages.

    Operations (in order):
      1. Filter out empty/invalid messages
      2. Strip control characters
      3. Truncate excessively long messages
      4. Merge consecutive same-role messages
      5. Ensure system prompt is first (if present)
      6. Ensure strict role alternation

    Args:
        messages: Raw message list
        max_length: Max content length per message (chars)
        merge_consecutive: Merge consecutive same-role messages
        strip_control: Strip control characters
        ensure_system_first: Move system messages to front

    Returns:
        Sanitized message list
    """
    if not messages:
        return []

    result: list[dict[str, str]] = []

    # 1. Filter empty/invalid messages
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not role or not isinstance(content, str):
            continue
        if not content.strip():
            continue
        result.append({"role": role, "content": content})

    if not result:
        return []

    # 2. Strip control characters
    if strip_control:
        for msg in result:
            msg["content"] = _CONTROL_CHARS.sub("", msg["content"])

    # 3. Truncate long messages
    for msg in result:
        if len(msg["content"]) > max_length:
            original_len = len(msg["content"])
            msg["content"] = msg["content"][:max_length] + f"\n[...truncated {original_len - max_length} chars]"
            logger.warning(
                "Truncated %s message from %d to %d chars",
                msg["role"],
                original_len,
                max_length,
            )

    # 4. Merge consecutive same-role messages
    if merge_consecutive:
        result = _merge_consecutive(result)

    # 5. Ensure system prompt is first
    if ensure_system_first:
        result = _ensure_system_first(result)

    # 6. Ensure strict role alternation
    result = _ensure_alternation(result)

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _merge_consecutive(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Merge consecutive messages with the same role."""
    if len(messages) <= 1:
        return messages

    result: list[dict[str, str]] = [messages[0]]

    for msg in messages[1:]:
        if msg["role"] == result[-1]["role"]:
            # Merge content
            result[-1]["content"] += "\n\n" + msg["content"]
        else:
            result.append(msg)

    return result


def _ensure_system_first(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Move all system messages to the front, merged into one."""
    system_msgs = [m for m in messages if m["role"] == "system"]
    non_system = [m for m in messages if m["role"] != "system"]

    if not system_msgs:
        return non_system

    # Merge all system messages into one
    if len(system_msgs) == 1:
        return system_msgs + non_system

    merged_content = "\n\n".join(m["content"] for m in system_msgs)
    return [{"role": "system", "content": merged_content}] + non_system


def _ensure_alternation(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Ensure strict role alternation (no two same-role in a row).

    If two same-role messages appear consecutively, they are merged.
    System messages are exempt (they can appear anywhere, but we keep
    them at the front per _ensure_system_first).
    """
    if len(messages) <= 1:
        return messages

    result: list[dict[str, str]] = [messages[0]]

    for msg in messages[1:]:
        prev = result[-1]
        if msg["role"] == prev["role"] and msg["role"] != "system":
            # Merge to maintain alternation
            prev["content"] += "\n\n" + msg["content"]
        else:
            result.append(msg)

    return result


def validate_messages(messages: list[dict[str, str]]) -> list[str]:
    """Validate message list and return list of issues (empty if valid).

    Checks:
      - Non-empty list
      - Each message has role and content
      - Roles are valid (system, user, assistant, tool)
      - No empty content
      - Role alternation (except system)
    """
    issues: list[str] = []

    if not messages:
        issues.append("Message list is empty")
        return issues

    valid_roles = {"system", "user", "assistant", "tool", "function"}

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            issues.append(f"Message {i}: not a dict")
            continue

        role = msg.get("role")
        if not role:
            issues.append(f"Message {i}: missing 'role'")
        elif role not in valid_roles:
            issues.append(f"Message {i}: invalid role '{role}'")

        content = msg.get("content")
        if content is None:
            issues.append(f"Message {i}: missing 'content'")
        elif not isinstance(content, str):
            issues.append(f"Message {i}: content is not a string")
        elif not content.strip():
            issues.append(f"Message {i}: empty content")

    # Check alternation
    non_system = [m for m in messages if m.get("role") != "system"]
    for i in range(1, len(non_system)):
        if non_system[i]["role"] == non_system[i - 1]["role"]:
            issues.append(
                f"Role alternation violation: consecutive '{non_system[i]['role']}' "
                f"at positions {i-1} and {i}"
            )

    return issues
