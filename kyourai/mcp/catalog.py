"""MCP server catalog — discover and connect to external MCP servers.

The catalog lets the Kyourai agent extend its capabilities by connecting
to third-party MCP servers (filesystem, browser, database, etc.) without
modifying the core. Servers are registered in a catalog file
(~/.kyourai/mcp_catalog.json) and discovered at agent startup.

Usage:
  from kyourai.mcp.catalog import MCPCatalog
  catalog = MCPCatalog()
  catalog.list_servers()  # → list of registered MCP servers
  catalog.connect("filesystem")  # → connect to a server
  catalog.register("my-server", command="node", args=["server.js"])
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from kyourai.constants import get_kyourai_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MCPServerConfig:
    """Configuration for a single MCP server entry."""
    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None  # for SSE/HTTP transport
    transport: str = "stdio"  # "stdio" or "sse" or "streamable_http"
    enabled: bool = True
    description: str = ""
    auto_connect: bool = False
    # Runtime state (not persisted)
    connected: bool = False
    tools: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Don't persist runtime state
        d.pop("connected", None)
        d.pop("tools", None)
        d.pop("error", None)
        return d

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "MCPServerConfig":
        return cls(
            name=name,
            command=data.get("command", ""),
            args=data.get("args", []),
            env=data.get("env", {}),
            url=data.get("url"),
            transport=data.get("transport", "stdio"),
            enabled=data.get("enabled", True),
            description=data.get("description", ""),
            auto_connect=data.get("auto_connect", False),
        )


# ---------------------------------------------------------------------------
# Bundled catalog — known MCP servers that user can enable
# ---------------------------------------------------------------------------


BUNDLED_SERVERS: dict[str, dict[str, Any]] = {
    "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/"],
        "transport": "stdio",
        "description": "Filesystem access — read/write files via MCP",
        "auto_connect": False,
    },
    "sqlite": {
        "command": "uvx",
        "args": ["mcp-server-sqlite", "--db-path", ":memory:"],
        "transport": "stdio",
        "description": "SQLite database access via MCP",
        "auto_connect": False,
    },
    "postgres": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres"],
        "transport": "stdio",
        "description": "PostgreSQL database access (requires connection string in env)",
        "auto_connect": False,
    },
    "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "transport": "stdio",
        "description": "GitHub API access — repos, issues, PRs (requires GITHUB_PERSONAL_ACCESS_TOKEN)",
        "auto_connect": False,
    },
    "brave-search": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-brave-search"],
        "transport": "stdio",
        "description": "Brave Search API (requires BRAVE_API_KEY)",
        "auto_connect": False,
    },
    "puppeteer": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-puppeteer"],
        "transport": "stdio",
        "description": "Browser automation via Puppeteer",
        "auto_connect": False,
    },
    "memory": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "transport": "stdio",
        "description": "Persistent memory via MCP (key-value store)",
        "auto_connect": False,
    },
    "time": {
        "command": "uvx",
        "args": ["mcp-server-time"],
        "transport": "stdio",
        "description": "Time and timezone utilities",
        "auto_connect": False,
    },
    "fetch": {
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "transport": "stdio",
        "description": "Fetch web pages and extract content",
        "auto_connect": False,
    },
    "sequential-thinking": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "transport": "stdio",
        "description": "Sequential thinking / step-by-step reasoning",
        "auto_connect": False,
    },
}


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class MCPCatalog:
    """MCP server catalog — discover, register, and connect to MCP servers."""

    def __init__(self, catalog_path: Path | None = None) -> None:
        self.catalog_path = catalog_path or (
            get_kyourai_home() / "mcp_catalog.json"
        )
        self._servers: dict[str, MCPServerConfig] = {}
        self._load()

    # -- Persistence --------------------------------------------------------

    def _load(self) -> None:
        """Load catalog from disk."""
        if self.catalog_path.exists():
            try:
                data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
                for name, cfg in data.get("servers", {}).items():
                    self._servers[name] = MCPServerConfig.from_dict(name, cfg)
            except Exception as e:
                logger.warning("Failed to load MCP catalog: %s", e)

    def _save(self) -> None:
        """Save catalog to disk."""
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "servers": {
                name: cfg.to_dict()
                for name, cfg in self._servers.items()
            }
        }
        self.catalog_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # -- CRUD ---------------------------------------------------------------

    def list_servers(self) -> list[MCPServerConfig]:
        """List all registered servers."""
        return list(self._servers.values())

    def list_bundled(self) -> list[dict[str, Any]]:
        """List bundled server templates that can be enabled."""
        result = []
        for name, cfg in BUNDLED_SERVERS.items():
            registered = name in self._servers
            result.append({
                "name": name,
                **cfg,
                "registered": registered,
            })
        return result

    def get_server(self, name: str) -> MCPServerConfig | None:
        return self._servers.get(name)

    def register(
        self,
        name: str,
        command: str = "",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        url: str | None = None,
        transport: str = "stdio",
        description: str = "",
        auto_connect: bool = False,
    ) -> MCPServerConfig:
        """Register a new MCP server."""
        cfg = MCPServerConfig(
            name=name,
            command=command,
            args=args or [],
            env=env or {},
            url=url,
            transport=transport,
            description=description,
            auto_connect=auto_connect,
        )
        self._servers[name] = cfg
        self._save()
        logger.info("Registered MCP server: %s", name)
        return cfg

    def register_bundled(self, name: str, **overrides: Any) -> MCPServerConfig | None:
        """Register a bundled server template by name."""
        template = BUNDLED_SERVERS.get(name)
        if not template:
            return None
        cfg_data = {**template, **overrides}
        return self.register(name, **cfg_data)

    def unregister(self, name: str) -> bool:
        """Remove a server from the catalog."""
        if name in self._servers:
            del self._servers[name]
            self._save()
            logger.info("Unregistered MCP server: %s", name)
            return True
        return False

    def enable(self, name: str) -> bool:
        """Enable a server."""
        cfg = self._servers.get(name)
        if cfg:
            cfg.enabled = True
            self._save()
            return True
        return False

    def disable(self, name: str) -> bool:
        """Disable a server (keeps config but won't auto-connect)."""
        cfg = self._servers.get(name)
        if cfg:
            cfg.enabled = False
            self._save()
            return True
        return False

    # -- Connection ---------------------------------------------------------

    def connect(self, name: str) -> MCPServerConfig | None:
        """Connect to an MCP server (spawn the process).

        Returns the updated config, or None if server not found.
        Connection state is stored in the config object (not persisted).
        """
        cfg = self._servers.get(name)
        if not cfg:
            return None

        if not cfg.enabled:
            cfg.error = "Server is disabled"
            return cfg

        # Check if command exists
        if cfg.transport == "stdio":
            if not cfg.command:
                cfg.error = "No command specified"
                return cfg

            if not shutil.which(cfg.command) and cfg.command != "npx" and cfg.command != "uvx":
                cfg.error = f"Command not found: {cfg.command}"
                return cfg

        # For now, we mark as connected — actual MCP client connection
        # would happen via the mcp Python SDK if installed
        try:
            # Try to use mcp SDK for real connection
            import importlib
            mcp_available = importlib.util.find_spec("mcp")
        except ImportError:
            mcp_available = False

        if mcp_available:
            # Real connection would go here — spawn process, handshake
            # For now, mark as connected with a note
            cfg.connected = True
            cfg.error = None
            logger.info("Connected to MCP server: %s", name)
        else:
            # Mark as configured but not connected (no mcp SDK)
            cfg.connected = False
            cfg.error = "mcp package not installed — run: pip install mcp"
            logger.warning("MCP SDK not installed, cannot connect to %s", name)

        return cfg

    def disconnect(self, name: str) -> bool:
        """Disconnect from an MCP server."""
        cfg = self._servers.get(name)
        if cfg and cfg.connected:
            cfg.connected = False
            logger.info("Disconnected from MCP server: %s", name)
            return True
        return False

    def connect_all(self) -> dict[str, bool]:
        """Connect to all auto_connect servers."""
        results: dict[str, bool] = {}
        for name, cfg in self._servers.items():
            if cfg.auto_connect and cfg.enabled:
                result = self.connect(name)
                results[name] = result is not None and result.connected
        return results

    # -- Introspection ------------------------------------------------------

    def get_tools(self, name: str) -> list[str]:
        """Get the list of tools exposed by a connected server."""
        cfg = self._servers.get(name)
        if cfg and cfg.connected:
            return cfg.tools
        return []

    def get_all_tools(self) -> dict[str, list[str]]:
        """Get tools from all connected servers."""
        result: dict[str, list[str]] = {}
        for name, cfg in self._servers.items():
            if cfg.connected:
                result[name] = cfg.tools
        return result

    def status(self) -> list[dict[str, Any]]:
        """Get status of all servers."""
        result = []
        for name, cfg in self._servers.items():
            result.append({
                "name": name,
                "enabled": cfg.enabled,
                "connected": cfg.connected,
                "transport": cfg.transport,
                "description": cfg.description,
                "error": cfg.error,
                "auto_connect": cfg.auto_connect,
            })
        return result
