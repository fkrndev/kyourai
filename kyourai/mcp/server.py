"""MCP server for portable context — exposes memory export/import as MCP tools.

This MCP server allows other AI agents (Claude, ChatGPT, Cursor, etc.) to
import memory FROM Kyourai and export memory TO Kyourai via the standardized
KPC (Kyourai Portable Context) format.

MCP tools exposed:
  - kyourai_export_memory: export Kyourai's memory as a KPC bundle
  - kyourai_import_memory: import a KPC bundle into Kyourai's memory
  - kyourai_list_memory: list current memory entries and facts
  - kyourai_search_memory: search Kyourai's holographic fact store

Usage:
  kyourai mcp-server  # starts the MCP server on stdio

The server uses the MCP Python SDK (mcp package). If not installed, the
server falls back to a simple JSON-RPC-over-stdio protocol.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from kyourai.memory.builtin import BuiltinMemoryProvider
from kyourai.memory.holographic.store import MemoryStore
from kyourai.memory.holographic.retrieval import FactRetriever
from kyourai.mcp.portable_context import (
    PortableContext,
    export_memory,
    import_memory,
)

logger = logging.getLogger(__name__)


class PortableContextMCPServer:
    """MCP server exposing Kyourai's memory as portable context tools.

    This is a minimal implementation that works with or without the `mcp`
    Python SDK. When the SDK is available, it uses the proper MCP protocol.
    Otherwise, it falls back to JSON-RPC over stdio.
    """

    def __init__(self) -> None:
        self._builtin_provider: BuiltinMemoryProvider | None = None
        self._holographic_store: MemoryStore | None = None
        self._retriever: FactRetriever | None = None

    def _ensure_initialized(self) -> None:
        if self._builtin_provider is None:
            self._builtin_provider = BuiltinMemoryProvider()
            self._builtin_provider.initialize("mcp-server")
        if self._holographic_store is None:
            self._holographic_store = MemoryStore()
            self._retriever = FactRetriever(store=self._holographic_store, hrr_dim=self._holographic_store.hrr_dim)

    # -- Tool implementations -----------------------------------------------

    def tool_export_memory(self, args: dict[str, Any]) -> str:
        """Export memory as a KPC JSON bundle."""
        self._ensure_initialized()
        include_builtin = args.get("include_builtin", True)
        include_holographic = args.get("include_holographic", True)
        profile = args.get("profile", {})

        ctx = export_memory(
            include_builtin=include_builtin,
            include_holographic=include_holographic,
            profile=profile,
            builtin_provider=self._builtin_provider,
            holographic_store=self._holographic_store,
        )
        return ctx.to_json()

    def tool_import_memory(self, args: dict[str, Any]) -> str:
        """Import a KPC JSON bundle into Kyourai's memory."""
        self._ensure_initialized()
        kpc_json = args.get("kpc_json", "")
        merge_strategy = args.get("merge_strategy", "skip_duplicates")

        try:
            ctx = PortableContext.from_json(kpc_json)
        except Exception as e:
            return json.dumps({"error": f"Invalid KPC bundle: {e}"})

        summary = import_memory(
            ctx,
            merge_strategy=merge_strategy,
            builtin_provider=self._builtin_provider,
            holographic_store=self._holographic_store,
        )
        return json.dumps(summary)

    def tool_list_memory(self, args: dict[str, Any]) -> str:
        """List current memory entries and facts."""
        self._ensure_initialized()
        store = self._builtin_provider.store

        result = {
            "builtin": {
                "memory_entries": list(store.memory_entries),
                "user_entries": list(store.user_entries),
            },
            "holographic": {
                "facts": self._holographic_store.list_facts(limit=args.get("limit", 50)),
            },
        }
        return json.dumps(result, ensure_ascii=False)

    def tool_search_memory(self, args: dict[str, Any]) -> str:
        """Search Kyourai's holographic fact store."""
        self._ensure_initialized()
        query = args.get("query", "")
        if not query:
            return json.dumps({"error": "query is required"})

        results = self._retriever.search(
            query,
            category=args.get("category"),
            min_trust=float(args.get("min_trust", 0.3)),
            limit=int(args.get("limit", 10)),
        )
        return json.dumps({"results": results, "count": len(results)}, ensure_ascii=False)

    # -- MCP tool schema definitions ----------------------------------------

    TOOLS = [
        {
            "name": "kyourai_export_memory",
            "description": (
                "Export Kyourai's memory as a portable context (KPC) JSON bundle. "
                "The bundle can be imported by other AI agents that support the "
                "Kyourai Portable Context format."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "include_builtin": {"type": "boolean", "default": True},
                    "include_holographic": {"type": "boolean", "default": True},
                    "profile": {"type": "object", "description": "Optional profile metadata"},
                },
            },
        },
        {
            "name": "kyourai_import_memory",
            "description": (
                "Import a portable context (KPC) JSON bundle into Kyourai's memory. "
                "Use this to transfer memory from another agent into Kyourai."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kpc_json": {"type": "string", "description": "The KPC JSON bundle to import"},
                    "merge_strategy": {
                        "type": "string",
                        "enum": ["skip_duplicates", "overwrite", "append"],
                        "default": "skip_duplicates",
                    },
                },
                "required": ["kpc_json"],
            },
        },
        {
            "name": "kyourai_list_memory",
            "description": "List all memory entries and facts in Kyourai's memory store.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 50},
                },
            },
        },
        {
            "name": "kyourai_search_memory",
            "description": "Search Kyourai's holographic fact store with hybrid FTS5 + HRR retrieval.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "category": {"type": "string"},
                    "min_trust": {"type": "number", "default": 0.3},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
    ]

    # -- JSON-RPC over stdio (fallback when mcp SDK not available) -----------

    def run_stdio(self) -> None:
        """Run a simple JSON-RPC server over stdio.

        Handles:
          - tools/list: returns the tool schemas
          - tools/call: dispatches to the appropriate tool handler
        """
        logger.info("Starting Kyourai MCP server (stdio mode)")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
                response = self._handle_request(request)
            except Exception as e:
                response = {"jsonrpc": "2.0", "error": {"code": -32603, "message": str(e)}}
                if isinstance(request, dict) and "id" in request:
                    response["id"] = request["id"]
            print(json.dumps(response), flush=True)

    def _handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "kyourai-mcp", "version": "0.1.0"},
                },
            }
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": self.TOOLS},
            }
        if method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            result = self._dispatch_tool(tool_name, tool_args)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": result}],
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"},
        }

    def _dispatch_tool(self, name: str, args: dict[str, Any]) -> str:
        if name == "kyourai_export_memory":
            return self.tool_export_memory(args)
        if name == "kyourai_import_memory":
            return self.tool_import_memory(args)
        if name == "kyourai_list_memory":
            return self.tool_list_memory(args)
        if name == "kyourai_search_memory":
            return self.tool_search_memory(args)
        return json.dumps({"error": f"Unknown tool: {name}"})

    def shutdown(self) -> None:
        if self._builtin_provider is not None:
            self._builtin_provider.shutdown()
        if self._holographic_store is not None:
            self._holographic_store.close()


def main() -> None:
    """Entry point for the MCP server."""
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    server = PortableContextMCPServer()
    try:
        server.run_stdio()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
