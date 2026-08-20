"""Tool registry — auto-discovery and registration of agent-callable tools.

Tools are the agent's hands. Each tool is a Python module in kyourai/tools/
that exports a TOOL_SCHEMA dict and a handler function. The registry
discovers them at startup and the agent wires them into Pydantic AI.

Core tools (always available):
  - terminal: execute shell commands (with safety checks)
  - read_file: read file contents (with path validation)
  - web_search: search the web (delegated to DuckDuckGo, no API key needed)

Design rules (from Hermes AGENTS.md):
  - Core tools are expensive — every tool ships on every API call.
  - Only fundamental, broadly useful tools belong here.
  - Niche capability should be a skill or plugin, not a core tool.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool schema type
# ---------------------------------------------------------------------------

# Each tool module exports:
#   TOOL_SCHEMA: dict[str, Any] — JSON schema for the tool
#   handle(**kwargs) -> str — the tool handler
#
# Schema format:
#   {
#     "name": "terminal",
#     "description": "Execute a shell command...",
#     "parameters": { ... JSON schema ... },
#   }

_CORE_TOOL_MODULES = [
    "kyourai.tools.terminal",
    "kyourai.tools.read_file",
    "kyourai.tools.web_search",
]


def discover_core_tools() -> list[dict[str, Any]]:
    """Discover and load all core tool schemas.

    Returns a list of TOOL_SCHEMA dicts. Failed imports are logged and
    skipped — a broken tool should never prevent the agent from starting.
    """
    schemas: list[dict[str, Any]] = []
    for module_path in _CORE_TOOL_MODULES:
        try:
            import importlib

            mod = importlib.import_module(module_path)
            schema = getattr(mod, "TOOL_SCHEMA", None)
            if schema and isinstance(schema, dict) and "name" in schema:
                schemas.append(schema)
                logger.debug("Loaded core tool: %s", schema["name"])
        except Exception as e:
            logger.warning("Failed to load core tool %s: %s", module_path, e)
    return schemas


def get_tool_handler(tool_name: str):
    """Return the handler function for a named core tool.

    Returns None if the tool is not found.
    """
    for module_path in _CORE_TOOL_MODULES:
        try:
            import importlib

            mod = importlib.import_module(module_path)
            schema = getattr(mod, "TOOL_SCHEMA", None)
            if schema and schema.get("name") == tool_name:
                handler = getattr(mod, "handle", None)
                if callable(handler):
                    return handler
        except Exception:
            continue
    return None


def list_core_tool_names() -> list[str]:
    """Return the names of available core tools (without importing them)."""
    return ["terminal", "read_file", "web_search"]
