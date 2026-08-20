"""Plugin system — third-party capability without modifying core.

Plugins live in ~/.kyourai/plugins/ and are discovered at startup.
Each plugin is a Python module that exports a register() function
and optionally a PLUGIN_METADATA dict.

Plugin contract:
  - register(agent: KyouraiAgent) -> None
    Called at agent init to wire hooks, tools, or memory providers.
  - PLUGIN_METADATA: dict (optional)
    {'name': ..., 'version': ..., 'description': ..., 'author': ...}

Plugin types:
  1. Tool plugins: add new tools to the agent
  2. Memory plugins: add new memory providers
  3. Hook plugins: register callbacks for agent events
  4. Skill plugins: add new skills

Inspired by Hermes' plugin system (plugin_llm.py 45k LOC) but radically
simpler — no manifest, no sandboxing, no LLM-based plugin loading.
Just Python modules with a register() function.

Security: plugins run in the same process as the agent. Only install
plugins from sources you trust. This is the same trust model as
pip install.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from kyourai.constants import get_kyourai_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plugin metadata
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PluginInfo:
    """Metadata about a discovered plugin."""
    name: str
    version: str = "0.0.0"
    description: str = ""
    author: str = ""
    path: str = ""
    loaded: bool = False
    error: str | None = None


# ---------------------------------------------------------------------------
# Hook system — plugins can register callbacks for agent events
# ---------------------------------------------------------------------------

# Event types that plugins can hook into
HOOK_TYPES = frozenset({
    "pre_run",          # before agent.run() — can modify prompt
    "post_run",         # after agent.run() — can modify output
    "pre_tool",         # before a tool call — can modify args
    "post_tool",        # after a tool call — can modify result
    "session_start",    # when a session starts
    "session_end",      # when a session ends
    "memory_sync",      # when memory is synced
})


class HookRegistry:
    """Registry for plugin hooks.

    Plugins register callbacks via:
        hooks.register("post_run", my_callback)
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[Callable]] = {}

    def register(self, event: str, callback: Callable) -> None:
        """Register a hook callback for an event type."""
        if event not in HOOK_TYPES:
            logger.warning("Unknown hook type: %s (valid: %s)", event, HOOK_TYPES)
            return
        self._hooks.setdefault(event, []).append(callback)

    def get_hooks(self, event: str) -> list[Callable]:
        """Get all callbacks for an event type."""
        return self._hooks.get(event, [])

    def run_hooks(self, event: str, *args: Any, **kwargs: Any) -> list[Any]:
        """Run all hooks for an event. Returns list of results."""
        results: list[Any] = []
        for callback in self._hooks.get(event, []):
            try:
                result = callback(*args, **kwargs)
                results.append(result)
            except Exception as e:
                logger.warning("Hook %s for event '%s' failed: %s",
                               callback.__name__, event, e)
        return results

    def clear(self) -> None:
        """Clear all hooks."""
        self._hooks.clear()


# ---------------------------------------------------------------------------
# Plugin manager
# ---------------------------------------------------------------------------


class PluginManager:
    """Discovers and loads plugins from ~/.kyourai/plugins/.

    Usage:
        pm = PluginManager()
        pm.discover()  # find all plugins
        pm.load_all(agent)  # load all discovered plugins
    """

    def __init__(self, plugins_dir: Path | None = None) -> None:
        self._plugins_dir = plugins_dir or (get_kyourai_home() / "plugins")
        if isinstance(self._plugins_dir, str):
            self._plugins_dir = Path(self._plugins_dir)
        self._discovered: dict[str, PluginInfo] = {}
        self._loaded: dict[str, Any] = {}
        self.hooks = HookRegistry()

    def discover(self) -> list[PluginInfo]:
        """Discover plugins in the plugins directory.

        Each plugin is either:
          - A single .py file: plugins/my_plugin.py
          - A package directory: plugins/my_plugin/__init__.py
        """
        self._discovered.clear()

        if not self._plugins_dir.exists():
            return []

        # Scan for .py files (not __init__.py)
        for entry in sorted(self._plugins_dir.iterdir()):
            if entry.is_file() and entry.suffix == ".py" and entry.name != "__init__.py":
                name = entry.stem
                self._discovered[name] = PluginInfo(
                    name=name,
                    path=str(entry),
                )

            # Scan for package directories
            elif entry.is_dir() and (entry / "__init__.py").exists():
                # Skip __pycache__
                if entry.name == "__pycache__":
                    continue
                name = entry.name
                self._discovered[name] = PluginInfo(
                    name=name,
                    path=str(entry / "__init__.py"),
                )

        return list(self._discovered.values())

    def load(self, name: str, agent: Any) -> bool:
        """Load a single plugin by name.

        Args:
            name: Plugin name (discovered or manually specified)
            agent: KyouraiAgent instance to pass to register()

        Returns:
            True if loaded successfully, False otherwise
        """
        if name in self._loaded:
            return True  # already loaded

        info = self._discovered.get(name)
        if not info:
            # Try to discover first
            self.discover()
            info = self._discovered.get(name)
            if not info:
                logger.warning("Plugin not found: %s", name)
                return False

        try:
            # Load the module
            module = self._load_module(name, info.path)
            if module is None:
                info.error = "Failed to load module"
                return False

            # Read metadata
            metadata = getattr(module, "PLUGIN_METADATA", {})
            if metadata:
                info.version = metadata.get("version", "0.0.0")
                info.description = metadata.get("description", "")
                info.author = metadata.get("author", "")

            # Call register function
            register_fn = getattr(module, "register", None)
            if not callable(register_fn):
                info.error = "No register() function found"
                logger.warning("Plugin %s has no register() function", name)
                return False

            # Create plugin context
            ctx = PluginContext(
                agent=agent,
                hooks=self.hooks,
                config=getattr(agent, "_config", {}),
            )

            register_fn(ctx)
            info.loaded = True
            self._loaded[name] = module
            logger.info("Loaded plugin: %s v%s", name, info.version)
            return True

        except Exception as e:
            info.error = str(e)
            info.loaded = False
            logger.warning("Failed to load plugin %s: %s", name, e)
            return False

    def load_all(self, agent: Any) -> list[PluginInfo]:
        """Load all discovered plugins.

        Returns:
            List of PluginInfo for all attempted loads
        """
        if not self._discovered:
            self.discover()

        for name in list(self._discovered.keys()):
            self.load(name, agent)

        return list(self._discovered.values())

    def _load_module(self, name: str, path: str) -> Any:
        """Load a Python module from a file path."""
        spec = importlib.util.spec_from_file_location(
            f"kyourai_plugin_{name}", path
        )
        if spec is None or spec.loader is None:
            return None

        module = importlib.util.module_from_spec(spec)
        sys.modules[f"kyourai_plugin_{name}"] = module
        spec.loader.exec_module(module)
        return module

    def get_loaded_plugins(self) -> list[str]:
        """Return names of loaded plugins."""
        return list(self._loaded.keys())

    def get_plugin_info(self) -> list[PluginInfo]:
        """Return info for all discovered plugins."""
        return list(self._discovered.values())

    def unload(self, name: str) -> bool:
        """Unload a plugin (removes from registry, doesn't unregister hooks)."""
        if name not in self._loaded:
            return False
        del self._loaded[name]
        info = self._discovered.get(name)
        if info:
            info.loaded = False
        return True

    def run_hooks(self, event: str, *args: Any, **kwargs: Any) -> list[Any]:
        """Run all hooks for an event (convenience method)."""
        return self.hooks.run_hooks(event, *args, **kwargs)

    def shutdown(self) -> None:
        """Cleanup all plugins."""
        self.hooks.clear()
        self._loaded.clear()


# ---------------------------------------------------------------------------
# Plugin context — passed to plugin register() function
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PluginContext:
    """Context passed to plugins at registration time.

    Plugins use this to access the agent, register hooks, and read config.
    """
    agent: Any
    hooks: HookRegistry
    config: dict[str, Any]

    def add_tool(self, tool: Any) -> None:
        """Add a tool to the agent (if supported)."""
        # This is a placeholder — actual tool injection would need
        # agent support for dynamic tool addition
        logger.info("Plugin add_tool called (not yet implemented for dynamic tools)")

    def add_memory_provider(self, provider: Any) -> None:
        """Add a memory provider to the agent."""
        if hasattr(self.agent, "memory_manager"):
            self.agent.memory_manager.providers.append(provider)
            logger.info("Plugin added memory provider: %s", type(provider).__name__)
