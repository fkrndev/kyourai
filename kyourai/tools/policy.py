"""Tool policy system — allow/deny lists, sandbox mode, availability conditions.

Inspired by OpenClaw's tool policy system. Provides:
  - ToolPolicy: allow/deny lists with wildcard support
  - Availability conditions: auth, config, env, plugin-enabled, context
  - Sandbox mode: restrict file access to workspace only
  - Policy composition: merge multiple policies (parent + agent + session)
  - Tool descriptor: name, description, owner, availability, schema

Usage:
    from kyourai.tools.policy import ToolPolicy, AvailabilityCheck, ToolDescriptor

    policy = ToolPolicy(allow=["terminal", "read_file"], deny=["web_search"])
    if policy.is_allowed("terminal"):
        # execute tool
        ...

    # Check availability
    avail = AvailabilityCheck(env="OPENAI_API_KEY")
    if avail.is_available():
        # tool is available
        ...
"""

from __future__ import annotations

import os
import re
import logging
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool ownership
# ---------------------------------------------------------------------------


class ToolOwner(str, Enum):
    """Who owns a tool."""
    CORE = "core"
    PLUGIN = "plugin"
    MCP = "mcp"
    CHANNEL = "channel"


# ---------------------------------------------------------------------------
# Availability conditions
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AvailabilityCheck:
    """Check if a tool is available based on conditions.

    Supports: env vars, config paths, auth providers, plugin state.
    """
    kind: str  # "always", "env", "config", "auth", "plugin-enabled"
    name: str = ""  # env var name, config path, provider ID, plugin ID
    check: str = "exists"  # "exists", "non-empty", "available"

    def is_available(self, context: dict[str, Any] | None = None) -> bool:
        """Check if this availability condition is met."""
        ctx = context or {}

        if self.kind == "always":
            return True

        if self.kind == "env":
            value = os.environ.get(self.name)
            if self.check == "exists":
                return value is not None
            if self.check == "non-empty":
                return bool(value)
            return value is not None

        if self.kind == "config":
            # Navigate dotted path in context
            parts = self.name.split(".")
            current: Any = ctx.get("config", {})
            for part in parts:
                if isinstance(current, dict):
                    current = current.get(part)
                else:
                    current = None
                    break
            if self.check == "exists":
                return current is not None
            if self.check == "non-empty":
                return bool(current)
            if self.check == "available":
                return current is not None
            return current is not None

        if self.kind == "auth":
            # Check if auth provider has credentials
            auth = ctx.get("auth", {})
            return bool(auth.get(self.name))

        if self.kind == "plugin-enabled":
            plugins = ctx.get("plugins", {})
            plugin = plugins.get(self.name)
            return bool(plugin and plugin.get("enabled", False))

        return True


@dataclass(slots=True)
class AvailabilityExpression:
    """Boolean expression of availability checks (allOf / anyOf / single)."""
    checks: list[AvailabilityCheck] = field(default_factory=list)
    mode: str = "all"  # "all" = allOf, "any" = anyOf, "single"

    def is_available(self, context: dict[str, Any] | None = None) -> bool:
        if not self.checks:
            return True
        if self.mode == "all":
            return all(c.is_available(context) for c in self.checks)
        if self.mode == "any":
            return any(c.is_available(context) for c in self.checks)
        return self.checks[0].is_available(context)


# ---------------------------------------------------------------------------
# Tool descriptor
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolDescriptor:
    """Descriptor for a tool — metadata for registration and discovery."""
    name: str
    description: str
    owner: ToolOwner = ToolOwner.CORE
    owner_id: str = ""  # plugin ID or MCP server ID
    availability: AvailabilityExpression | None = None
    sort_key: str = ""
    annotations: dict[str, Any] = field(default_factory=dict)

    def is_available(self, context: dict[str, Any] | None = None) -> bool:
        if self.availability:
            return self.availability.is_available(context)
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "owner": self.owner.value,
            "owner_id": self.owner_id,
            "available": self.is_available(),
            "sort_key": self.sort_key,
        }


# ---------------------------------------------------------------------------
# Tool policy
# ---------------------------------------------------------------------------


class ToolPolicy:
    """Tool allow/deny policy with wildcard support.

    Rules:
      - If deny contains tool name → blocked
      - If allow is empty → allow all (except denied)
      - If allow contains "*" → allow all (except denied)
      - If allow is non-empty and doesn't contain "*" → only allow listed
      - Sandbox mode restricts file access to workspace directory
    """

    def __init__(
        self,
        allow: list[str] | None = None,
        deny: list[str] | None = None,
        workspace_only: bool = False,
        sandbox: bool = False,
    ) -> None:
        self.allow = list(allow) if allow else []
        self.deny = list(deny) if deny else []
        self.workspace_only = workspace_only
        self.sandbox = sandbox

    def is_allowed(self, tool_name: str) -> bool:
        """Check if a tool is allowed by this policy."""
        # Normalize
        name = tool_name.lower().strip()

        # Check deny list first
        for d in self.deny:
            if self._matches(d.lower().strip(), name):
                return False

        # Empty allow = allow all
        if not self.allow:
            return True

        # Wildcard
        if "*" in self.allow:
            return True

        # Check allow list
        for a in self.allow:
            if self._matches(a.lower().strip(), name):
                return True

        return False

    def is_path_allowed(self, path: str | Path) -> bool:
        """Check if a file path is allowed (for sandbox/workspace_only mode)."""
        if not self.workspace_only and not self.sandbox:
            return True

        try:
            target = Path(path).resolve()
            workspace = Path.cwd().resolve()

            # Check if path is within workspace
            try:
                target.relative_to(workspace)
                return True
            except ValueError:
                pass

            # Allow temp directories even in sandbox
            if self.sandbox:
                temp_dirs = [Path(os.environ.get("TEMP", "/tmp")),
                             Path(os.environ.get("TMP", "/tmp"))]
                for td in temp_dirs:
                    try:
                        target.relative_to(td.resolve())
                        return True
                    except (ValueError, OSError):
                        pass

            return False
        except Exception:
            return not self.sandbox

    def merge(self, other: "ToolPolicy") -> "ToolPolicy":
        """Merge two policies (intersection of allows, union of denies)."""
        # If either has wildcard allow, the merged is the non-wildcard one
        if "*" in self.allow or "*" in other.allow:
            merged_allow = []
        else:
            merged_allow = list(set(self.allow) & set(other.allow)) if self.allow and other.allow else (
                self.allow or other.allow
            )

        merged_deny = list(set(self.deny) | set(other.deny))
        merged_workspace = self.workspace_only or other.workspace_only
        merged_sandbox = self.sandbox or other.sandbox

        return ToolPolicy(
            allow=merged_allow,
            deny=merged_deny,
            workspace_only=merged_workspace,
            sandbox=merged_sandbox,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow": self.allow,
            "deny": self.deny,
            "workspace_only": self.workspace_only,
            "sandbox": self.sandbox,
        }

    @staticmethod
    def _matches(pattern: str, name: str) -> bool:
        """Check if a pattern matches a tool name (supports wildcards)."""
        if pattern == name:
            return True
        if pattern == "*":
            return True
        # Prefix wildcard: "web_*" matches "web_search", "web_fetch"
        if pattern.endswith("*"):
            prefix = pattern[:-1]
            return name.startswith(prefix)
        # Suffix wildcard: "*_file" matches "read_file", "write_file"
        if pattern.startswith("*"):
            suffix = pattern[1:]
            return name.endswith(suffix)
        return False

    def __repr__(self) -> str:
        return (
            f"ToolPolicy(allow={self.allow}, deny={self.deny}, "
            f"workspace_only={self.workspace_only}, sandbox={self.sandbox})"
        )


# ---------------------------------------------------------------------------
# Tool registry — descriptor-based registration
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Registry for tool descriptors with policy-based filtering.

    Tools are registered with descriptors that include availability conditions.
    The registry filters tools based on the current context (env, config, auth)
    and the active tool policy.
    """

    def __init__(self) -> None:
        self._descriptors: dict[str, ToolDescriptor] = {}
        self._default_policy = ToolPolicy()

    def register(self, descriptor: ToolDescriptor) -> None:
        """Register a tool descriptor."""
        self._descriptors[descriptor.name] = descriptor
        logger.debug("Registered tool: %s", descriptor.name)

    def unregister(self, name: str) -> bool:
        """Unregister a tool."""
        if name in self._descriptors:
            del self._descriptors[name]
            return True
        return False

    def get(self, name: str) -> ToolDescriptor | None:
        """Get a tool descriptor by name."""
        return self._descriptors.get(name)

    def list_all(self) -> list[ToolDescriptor]:
        """List all registered tool descriptors."""
        return list(self._descriptors.values())

    def list_available(
        self,
        policy: ToolPolicy | None = None,
        context: dict[str, Any] | None = None,
    ) -> list[ToolDescriptor]:
        """List tools that are available and allowed by policy."""
        active_policy = policy or self._default_policy
        result: list[ToolDescriptor] = []

        for desc in self._descriptors.values():
            # Check policy
            if not active_policy.is_allowed(desc.name):
                continue
            # Check availability
            if not desc.is_available(context):
                continue
            result.append(desc)

        # Sort by sort_key if present
        result.sort(key=lambda d: d.sort_key or d.name)
        return result

    def list_blocked(self, policy: ToolPolicy) -> list[str]:
        """List tools blocked by the given policy."""
        return [
            name for name in self._descriptors
            if not policy.is_allowed(name)
        ]

    def set_default_policy(self, policy: ToolPolicy) -> None:
        """Set the default policy for the registry."""
        self._default_policy = policy

    @property
    def default_policy(self) -> ToolPolicy:
        return self._default_policy


# ---------------------------------------------------------------------------
# Default tool policies
# ---------------------------------------------------------------------------

# Safe policy for subagents — no web access, workspace-only file access
SUBAGENT_SAFE_POLICY = ToolPolicy(
    deny=["web_search", "web_fetch"],
    workspace_only=True,
)

# Read-only policy — no terminal, no write
READONLY_POLICY = ToolPolicy(
    allow=["read_file", "web_search", "web_fetch"],
    deny=["terminal", "write_file", "edit_file"],
)

# Full access policy (default)
FULL_ACCESS_POLICY = ToolPolicy()

# Sandbox policy — workspace only, no network
SANDBOX_POLICY = ToolPolicy(
    workspace_only=True,
    sandbox=True,
    deny=["web_search", "web_fetch"],
)
