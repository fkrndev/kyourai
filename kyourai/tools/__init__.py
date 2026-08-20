"""Tool registry and built-in tools.

Core tools (always available to the agent):
  - terminal: execute shell commands
  - read_file: read file contents
  - web_search: search the web (DuckDuckGo, no API key)

Use kyourai.tools.registry.discover_core_tools() to get all schemas,
and get_tool_handler(name) to get the handler function.
"""

from kyourai.tools.registry import (
    discover_core_tools,
    get_tool_handler,
    list_core_tool_names,
)

__all__ = ["discover_core_tools", "get_tool_handler", "list_core_tool_names"]
