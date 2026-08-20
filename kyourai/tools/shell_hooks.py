"""Shell hooks — pre/post hooks for terminal commands.

Allows registering callbacks that run before and after terminal commands.
Useful for:
  - Auto-linting after file edits (post-hook)
  - Running tests after git commits (post-hook)
  - Blocking dangerous commands (pre-hook)
  - Logging all commands for audit
  - Auto-formatting code after writes

Hooks are registered in config.yaml under `shell_hooks`:

  shell_hooks:
    pre:
      - name: block-rm-rf
        pattern: "rm -rf /"
        action: block
        message: "Blocked: rm -rf / is dangerous"
    post:
      - name: auto-test
        pattern: "git commit"
        action: run
        command: "pytest --tb=short"
        timeout: 30

Usage (programmatic):
  from kyourai.tools.shell_hooks import ShellHookManager
  manager = ShellHookManager()
  manager.register_pre("block-dangerous", pattern="rm -rf", action="block")
  manager.register_post("auto-test", pattern="pytest", action="log")
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hook types
# ---------------------------------------------------------------------------


ActionType = Literal["block", "warn", "run", "log", "notify"]


@dataclass(slots=True)
class ShellHook:
    """A shell hook — runs before or after a terminal command."""
    name: str
    pattern: str  # regex pattern to match against command
    action: ActionType  # what to do when matched
    message: str = ""  # message for warn/block/notify
    command: str = ""  # command to run for "run" action
    timeout: int = 30  # timeout for "run" action (seconds)
    enabled: bool = True
    # Compiled pattern (runtime)
    _compiled: re.Pattern[str] = field(default_factory=lambda: re.compile(""))

    def __post_init__(self) -> None:
        try:
            self._compiled = re.compile(self.pattern, re.IGNORECASE)
        except re.error as e:
            logger.warning("Invalid hook pattern '%s': %s", self.pattern, e)
            self.enabled = False

    def matches(self, command: str) -> bool:
        """Check if this hook matches the given command."""
        if not self.enabled:
            return False
        return bool(self._compiled.search(command))


@dataclass(slots=True)
class HookResult:
    """Result of running hooks against a command."""
    blocked: bool = False
    warnings: list[str] = field(default_factory=list)
    notifications: list[str] = field(default_factory=list)
    post_commands: list[str] = field(default_factory=list)
    matched_hooks: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Hook manager
# ---------------------------------------------------------------------------


class ShellHookManager:
    """Manage pre/post shell hooks for terminal commands."""

    def __init__(self) -> None:
        self._pre_hooks: list[ShellHook] = []
        self._post_hooks: list[ShellHook] = []
        self._load_from_config()

    def _load_from_config(self) -> None:
        """Load hooks from config.yaml."""
        try:
            from kyourai.config import get_config
            config = get_config()
            hooks_config = config.get("shell_hooks", {})

            for hook_data in hooks_config.get("pre", []):
                self.register_pre(
                    name=hook_data.get("name", "unnamed"),
                    pattern=hook_data.get("pattern", ""),
                    action=hook_data.get("action", "warn"),
                    message=hook_data.get("message", ""),
                    command=hook_data.get("command", ""),
                    timeout=hook_data.get("timeout", 30),
                )

            for hook_data in hooks_config.get("post", []):
                self.register_post(
                    name=hook_data.get("name", "unnamed"),
                    pattern=hook_data.get("pattern", ""),
                    action=hook_data.get("action", "log"),
                    message=hook_data.get("message", ""),
                    command=hook_data.get("command", ""),
                    timeout=hook_data.get("timeout", 30),
                )
        except Exception as e:
            logger.debug("No shell hooks config loaded: %s", e)

    def register_pre(
        self,
        name: str,
        pattern: str,
        action: ActionType = "warn",
        message: str = "",
        command: str = "",
        timeout: int = 30,
    ) -> ShellHook:
        """Register a pre-command hook."""
        hook = ShellHook(
            name=name,
            pattern=pattern,
            action=action,
            message=message,
            command=command,
            timeout=timeout,
        )
        self._pre_hooks.append(hook)
        return hook

    def register_post(
        self,
        name: str,
        pattern: str,
        action: ActionType = "log",
        message: str = "",
        command: str = "",
        timeout: int = 30,
    ) -> ShellHook:
        """Register a post-command hook."""
        hook = ShellHook(
            name=name,
            pattern=pattern,
            action=action,
            message=message,
            command=command,
            timeout=timeout,
        )
        self._post_hooks.append(hook)
        return hook

    def run_pre_hooks(self, command: str) -> HookResult:
        """Run pre-command hooks.

        Returns HookResult with blocked=True if any hook blocks the command.
        """
        result = HookResult()

        for hook in self._pre_hooks:
            if not hook.matches(command):
                continue

            result.matched_hooks.append(hook.name)

            if hook.action == "block":
                result.blocked = True
                result.warnings.append(
                    hook.message or f"Command blocked by hook '{hook.name}'"
                )
                logger.warning("Command blocked by hook '%s': %s", hook.name, command)
                break  # Stop on first block

            elif hook.action == "warn":
                result.warnings.append(
                    hook.message or f"Warning from hook '{hook.name}'"
                )
                logger.warning("Hook '%s' warning: %s", hook.name, hook.message)

            elif hook.action == "notify":
                result.notifications.append(
                    hook.message or f"Notification from hook '{hook.name}'"
                )

        return result

    def run_post_hooks(self, command: str, exit_code: int = 0) -> HookResult:
        """Run post-command hooks.

        Returns HookResult with post_commands that should be run.
        """
        result = HookResult()

        for hook in self._post_hooks:
            if not hook.matches(command):
                continue

            result.matched_hooks.append(hook.name)

            if hook.action == "run" and hook.command:
                result.post_commands.append(hook.command)
                logger.info("Post-hook '%s' will run: %s", hook.name, hook.command)

            elif hook.action == "notify":
                result.notifications.append(
                    hook.message or f"Notification from hook '{hook.name}'"
                )

            elif hook.action == "log":
                logger.info("Post-hook log '%s': command='%s' exit=%d",
                           hook.name, command, exit_code)

        return result

    def execute_post_commands(self, commands: list[str], timeout: int = 30) -> list[dict[str, Any]]:
        """Execute post-hook commands and return results."""
        results: list[dict[str, Any]] = []

        for cmd in commands:
            start = time.time()
            try:
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                results.append({
                    "command": cmd,
                    "exit_code": proc.returncode,
                    "stdout": proc.stdout[:1000],
                    "stderr": proc.stderr[:1000],
                    "duration_ms": round((time.time() - start) * 1000, 2),
                })
            except subprocess.TimeoutExpired:
                results.append({
                    "command": cmd,
                    "exit_code": -1,
                    "error": "Timeout",
                    "duration_ms": round((time.time() - start) * 1000, 2),
                })
            except Exception as e:
                results.append({
                    "command": cmd,
                    "exit_code": -1,
                    "error": str(e),
                    "duration_ms": round((time.time() - start) * 1000, 2),
                })

        return results

    def list_hooks(self) -> dict[str, list[dict[str, Any]]]:
        """List all registered hooks."""
        return {
            "pre": [
                {
                    "name": h.name,
                    "pattern": h.pattern,
                    "action": h.action,
                    "enabled": h.enabled,
                    "message": h.message,
                }
                for h in self._pre_hooks
            ],
            "post": [
                {
                    "name": h.name,
                    "pattern": h.pattern,
                    "action": h.action,
                    "enabled": h.enabled,
                    "message": h.message,
                    "command": h.command,
                }
                for h in self._post_hooks
            ],
        }

    def enable(self, name: str) -> bool:
        """Enable a hook by name."""
        for hook in self._pre_hooks + self._post_hooks:
            if hook.name == name:
                hook.enabled = True
                return True
        return False

    def disable(self, name: str) -> bool:
        """Disable a hook by name."""
        for hook in self._pre_hooks + self._post_hooks:
            if hook.name == name:
                hook.enabled = False
                return True
        return False
