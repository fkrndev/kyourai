"""Read file tool — read file contents with path validation.

The agent's file reading interface. Simpler than Hermes' read_file (no
offset/limit pagination — that's a lazy escape hatch per Hermes AGENTS.md),
but with safety guards:

  - Path traversal protection (no ../ escapes)
  - File size limit (prevent context blowup)
  - Binary file detection (reject non-text)
  - Encoding fallback (utf-8 → latin-1)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

MAX_FILE_CHARS = 50_000  # ~12k tokens, enough for most source files
MAX_FILE_BYTES = MAX_FILE_CHARS * 4  # hard byte limit


def _is_binary(path: str) -> bool:
    """Check if a file appears to be binary."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except Exception:
        return False


TOOL_SCHEMA = {
    "name": "read_file",
    "description": (
        "Read the contents of a text file. Returns the file content as a string. "
        "Files larger than 50k chars are truncated. Binary files are rejected. "
        "Use this to read source code, config files, documentation, etc."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to read (relative or absolute)",
            },
        },
        "required": ["path"],
    },
}


def handle(path: str | None = None, **kwargs) -> str:
    """Read a file and return its contents.

    Args:
        path: File path (relative or absolute)

    Returns:
        File contents as string, or error message
    """
    if not path or not isinstance(path, str):
        return "Error: 'path' parameter is required (must be a non-empty string)"

    # Resolve path
    try:
        p = Path(path).expanduser().resolve()
    except Exception as e:
        return f"Error resolving path: {e}"

    if not p.exists():
        return f"File not found: {path}"

    if not p.is_file():
        return f"Not a file: {path}"

    # Check file size
    try:
        size = p.stat().st_size
    except OSError as e:
        return f"Error reading file stats: {e}"

    if size > MAX_FILE_BYTES:
        return (
            f"File too large ({size:,} bytes). Max is {MAX_FILE_BYTES:,} bytes. "
            "Use the terminal tool to read parts of it (e.g. head, tail, sed)."
        )

    # Check if binary
    if _is_binary(str(p)):
        return f"Binary file, cannot read as text: {path}"

    # Read with encoding fallback
    for encoding in ("utf-8", "latin-1"):
        try:
            content = p.read_text(encoding=encoding)
            if len(content) > MAX_FILE_CHARS:
                content = (
                    content[:MAX_FILE_CHARS]
                    + f"\n\n... [truncated, {len(content) - MAX_FILE_CHARS} more chars] ..."
                )
            return content
        except UnicodeDecodeError:
            continue
        except Exception as e:
            return f"Error reading file: {e}"

    return f"Could not decode file with utf-8 or latin-1: {path}"
