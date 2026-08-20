"""Terminal UI (TUI) for Kyourai — interactive chat with memory sidebar.

Built with Textual. Split-pane layout:
  - Left: chat messages (scrollable)
  - Right: memory sidebar (recent facts, recall indicator)
  - Bottom: input field
  - Top: header with session info

Usage:
  kyourai tui --model openai:gpt-4o
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Header,
    Footer,
    Input,
    Label,
    RichLog,
    Static,
)
from textual.reactive import reactive
from rich.text import Text
from rich.panel import Panel
from rich.markdown import Markdown

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Memory sidebar widget
# ---------------------------------------------------------------------------


class MemorySidebar(Static):
    """Sidebar showing recent facts and recall indicator."""

    facts: reactive[list[dict]] = reactive([], layout=True)

    def render(self) -> Any:
        if not self.facts:
            return Panel(
                Text("No facts recalled yet", style="dim"),
                title="Memory",
                border_style="blue",
            )

        lines: list[str] = []
        for fact in self.facts[:10]:  # show top 10
            trust = fact.get("trust", 0.5)
            trust_bar = "█" * int(trust * 5) + "░" * (5 - int(trust * 5))
            entity = fact.get("entity", "?")
            content = fact.get("content", "")[:60]
            lines.append(f"[{trust_bar}] {entity}: {content}")

        return Panel(
            "\n".join(lines),
            title=f"Memory ({len(self.facts)} facts)",
            border_style="blue",
        )


# ---------------------------------------------------------------------------
# Main TUI app
# ---------------------------------------------------------------------------


class KyouraiTUI(App):
    """Kyourai Terminal UI — interactive chat with memory."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #main-area {
        height: 1fr;
        layout: horizontal;
    }

    #chat-area {
        width: 2fr;
        border: solid $primary;
        padding: 0 1;
    }

    #sidebar-area {
        width: 1fr;
        border: solid $accent;
        padding: 0 1;
    }

    #input-area {
        height: 3;
        dock: bottom;
    }

    Input {
        border: solid $primary;
    }

    .msg-user {
        color: $text;
        background: $primary-darken-2;
        padding: 0 1;
        margin: 0 0 1 0;
    }

    .msg-assistant {
        color: $text;
        padding: 0 1;
        margin: 0 0 1 0;
    }

    .msg-system {
        color: $yellow;
        padding: 0 1;
        margin: 0 0 1 0;
    }

    #status-bar {
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+l", "clear", "Clear"),
        ("ctrl+m", "toggle_memory", "Memory"),
    ]

    show_memory: reactive[bool] = reactive(True)
    is_running: reactive[bool] = reactive(False)

    def __init__(self, model: str = "openai:gpt-4o", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.model = model
        self.agent: Any = None
        self._history: list[dict[str, str]] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main-area"):
            with Vertical(id="chat-area"):
                yield RichLog(id="chat-log", markup=True, wrap=True)
            with Vertical(id="sidebar-area"):
                yield MemorySidebar(id="memory-sidebar")
        yield Input(id="input-area", placeholder="Type a message... (Ctrl+C to quit)")
        yield Static("Ready", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        """Initialize the agent when TUI starts."""
        self.title = "Kyourai TUI"
        self.sub_title = f"Model: {self.model}"

        # Initialize agent in background
        self._init_agent()

        # Focus input
        self.query_one(Input).focus()

    @work(thread=True)
    def _init_agent(self) -> None:
        """Initialize the KyouraiAgent."""
        try:
            from kyourai.agent import KyouraiAgent
            from pydantic_ai.models.test import TestModel

            # Use TestModel if no API key configured
            try:
                self.agent = KyouraiAgent(
                    model=self.model,
                    session_id="tui-session",
                    enable_curator=True,
                    enable_skills=True,
                    enable_cron=False,
                )
            except ValueError:
                # No API key — fall back to test model
                self.agent = KyouraiAgent(
                    model=TestModel(),
                    session_id="tui-session",
                    enable_curator=False,
                    enable_skills=False,
                    enable_cron=False,
                )

            self.call_from_thread(self._update_status, "Agent ready")
            self.call_from_thread(
                self._add_system_message,
                f"Kyourai initialized with model: {self.model}\n"
                "Type a message and press Enter to chat.\n"
                "Ctrl+M to toggle memory sidebar, Ctrl+L to clear.",
            )

        except Exception as e:
            self.call_from_thread(self._update_status, f"Error: {e}")
            self.call_from_thread(self._add_system_message, f"Failed to init: {e}")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user input."""
        if self.is_running or not self.agent:
            return

        msg = event.value.strip()
        if not msg:
            return

        # Clear input
        event.value = ""

        # Add user message to chat
        self._add_user_message(msg)
        self._history.append({"role": "user", "content": msg})

        # Update status
        self.is_running = True
        self._update_status("Thinking...")

        # Run agent in background
        self._run_agent(msg)

    @work(thread=True)
    def _run_agent(self, msg: str) -> None:
        """Run the agent and display the response."""
        try:
            # Run the agent
            result = asyncio.run(self.agent.run(msg, message_history=list(self._history)))

            # Add assistant message
            self.call_from_thread(self._add_assistant_message, result)
            self._history.append({"role": "assistant", "content": result})

            # Update memory sidebar
            self.call_from_thread(self._update_memory)

        except Exception as e:
            self.call_from_thread(self._add_system_message, f"Error: {e}")
        finally:
            self.call_from_thread(self._set_running, False)
            self.call_from_thread(self._update_status, "Ready")
            self.call_from_thread(self.query_one(Input).focus)

    def _add_user_message(self, msg: str) -> None:
        """Add a user message to the chat log."""
        log = self.query_one(RichLog)
        log.write(Text("You: ", style="bold cyan") + Text(msg))

    def _add_assistant_message(self, msg: str) -> None:
        """Add an assistant message to the chat log."""
        log = self.query_one(RichLog)
        log.write(Text("Kyourai: ", style="bold green") + Text(msg))

    def _add_system_message(self, msg: str) -> None:
        """Add a system message to the chat log."""
        log = self.query_one(RichLog)
        log.write(Text(msg, style="yellow"))

    def _update_status(self, status: str) -> None:
        """Update the status bar."""
        self.query_one("#status-bar", Static).update(status)

    def _set_running(self, running: bool) -> None:
        self.is_running = running

    def _update_memory(self) -> None:
        """Update the memory sidebar with recent facts."""
        if not self.agent:
            return

        try:
            # Search for facts related to recent conversation
            manager = self.agent.memory_manager
            if hasattr(manager, "holographic"):
                holo = manager.holographic
                if holo and hasattr(holo, "store"):
                    facts = holo.store.search_facts("", limit=10)
                    self.query_one(MemorySidebar).facts = facts
        except Exception:
            pass

    def action_clear(self) -> None:
        """Clear the chat log."""
        self.query_one(RichLog).clear()
        self._history.clear()

    def action_toggle_memory(self) -> None:
        """Toggle memory sidebar visibility."""
        self.show_memory = not self.show_memory
        sidebar = self.query_one("#sidebar-area")
        sidebar.display = self.show_memory

    def on_unmount(self) -> None:
        """Cleanup when TUI exits."""
        if self.agent:
            try:
                self.agent.shutdown()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_tui(model: str = "openai:gpt-4o") -> None:
    """Start the Kyourai TUI."""
    app = KyouraiTUI(model=model)
    app.run()
