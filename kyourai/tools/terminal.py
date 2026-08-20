"""Terminal tool — execute shell commands safely.

The agent's primary interface to the machine. Runs commands with:
  - Working directory tracking (per-session)
  - Timeout enforcement (configurable, default 30s)
  - Output truncation (prevent context blowup)
  - Safety blocklist (rm -rf /, shutdown, etc.)
  - Non-interactive (stdin closed — prevents hangs)

Inspired by Hermes' terminal tool but radically simpler — no PTY, no
Docker/SSH backends, no environment management. Just subprocess.run with
safety guards.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any

# Safety blocklist — commands that should never run
_BLOCKED_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf $HOME",
    "mkfs",
    "dd if=/dev/zero of=/dev/",
    "shutdown",
    "reboot",
    ":(){ :|:& };:",  # fork bomb
    "> /dev/sda",
    "chmod -R 777 /",
]

MAX_OUTPUT_CHARS = 10_000  # truncate output to prevent context blowup
DEFAULT_TIMEOUT = 30  # seconds


def _check_safety(command: str) -> str | None:
    """Return error message if command is blocked, None if safe."""
    cmd_lower = command.lower().strip()
    for pattern in _BLOCKED_PATTERNS:
        if pattern.lower() in cmd_lower:
            return f"Blocked: command matches dangerous pattern '{pattern}'"
    return None


def _truncate(output: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """Truncate output to max_chars, adding a notice if truncated."""
    if len(output) <= max_chars:
        return output
    half = max_chars // 2
    return (
        output[:half]
        + f"\n\n... [truncated {len(output) - max_chars} chars] ...\n\n"
        + output[-half:]
    )


TOOL_SCHEMA = {
    "name": "terminal",
    "description": (
        "Execute a shell command and return stdout+stderr. "
        "Use this for running code, checking files, git operations, "
        "and any system interaction. Commands run non-interactively "
        "with a 30-second timeout. Output is truncated to 10k chars."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 30, max 120)",
            },
        },
        "required": ["command"],
    },
}


def handle(command: str | None = None, timeout: int | None = None, **kwargs) -> str:
    """Execute a shell command and return the output.

    Args:
        command: The shell command to execute
        timeout: Timeout in seconds (default 30, max 120)

    Returns:
        stdout + stderr output, or error message
    """
    if not command or not isinstance(command, str):
        return "Error: 'command' parameter is required (must be a non-empty string)"

    # Safety check
    blocked = _check_safety(command)
    if blocked:
        return blocked

    # Clamp timeout
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    timeout = max(1, min(timeout, 120))

    # Run the command
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,  # non-interactive
            cwd=os.getcwd(),
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n" if output else "") + f"[stderr]\n{result.stderr}"
        if not output:
            output = f"(exit code {result.returncode}, no output)"
        elif result.returncode != 0:
            output = f"(exit code {result.returncode})\n{output}"
        return _truncate(output)
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s: {command[:100]}"
    except Exception as e:
        return f"Error executing command: {e}"
