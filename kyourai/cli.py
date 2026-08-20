"""Kyourai CLI — command-line interface for the memory-first AI agent.

Commands:
  kyourai chat              — start interactive chat with the agent
  kyourai memory list       — list memory entries
  kyourai memory add        — add a memory entry
  kyourai memory search     — search holographic facts
  kyourai memory export     — export memory to KPC file
  kyourai memory import     — import memory from KPC file
  kyourai team create       — create a new team
  kyourai team list         — list teams
  kyourai team add-member   — add a member to a team
  kyourai curator run       — run the curator manually
  kyourai curator status    — show curator state
  kyourai mcp-server        — start the MCP server (for other agents)
  kyourai config show       — show current config
  kyourai config init       — create a default config.yaml
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from kyourai import __version__
from kyourai.constants import get_kyourai_home, ensure_home
from kyourai.config import load_config, save_config, get_config_value

logger = logging.getLogger(__name__)
console = Console()


def _setup_logging(verbose: bool) -> None:
    from kyourai.logging import setup_logging

    setup_logging(verbose=verbose)


# ---------------------------------------------------------------------------
# Main CLI group
# ---------------------------------------------------------------------------

@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="kyourai")
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose logging")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Kyourai (記憶雷) — memory-first AI agent."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ---------------------------------------------------------------------------
# chat — interactive chat with the agent
# ---------------------------------------------------------------------------

@main.command()
@click.option("--model", default=None, help="Model to use (e.g. openai:gpt-4o, anthropic:claude-3-5-sonnet)")
@click.option("--session", default="default", help="Session ID")
@click.option("--team", default=None, help="Team ID for team mode")
@click.option("--user", default=None, help="User ID for team mode")
def chat(model: str | None, session: str, team: str | None, user: str | None) -> None:
    """Start an interactive chat session with Kyourai."""
    from kyourai.agent import KyouraiAgent

    model = model or get_config_value("agent.model", "openai:gpt-4o")
    ensure_home()

    console.print(Panel.fit(
        f"[bold cyan]Kyourai (記憶雷)[/bold cyan] v{__version__}\n"
        f"Model: {model}\n"
        f"Session: {session}" + (f"\nTeam: {team}, User: {user}" if team else ""),
        border_style="cyan",
    ))

    agent = KyouraiAgent(model=model, session_id=session, team_id=team, user_id=user)

    message_history = None
    try:
        while True:
            try:
                user_input = Prompt.ask("[bold green]You[/bold green]")
            except (EOFError, KeyboardInterrupt):
                break
            if not user_input.strip():
                continue
            if user_input.lower() in ("exit", "quit", "/exit", "/quit"):
                break

            console.print("[bold cyan]Kyourai[/bold cyan] ", end="")
            try:
                output = asyncio.run(agent.run(user_input, message_history=message_history))
                console.print(output)
                agent.sync_turn(user_input, output)
            except Exception as e:
                console.print(f"[red]Error:[/red] {e}")
    finally:
        agent.shutdown()
        console.print("[dim]Session ended. Memory saved.[/dim]")


# ---------------------------------------------------------------------------
# memory — memory management commands
# ---------------------------------------------------------------------------

@main.group()
def memory() -> None:
    """Manage memory entries and facts."""


@memory.command("list")
@click.option("--target", type=click.Choice(["memory", "user", "all"]), default="all")
@click.option("--limit", default=50, help="Max entries to show")
def memory_list(target: str, limit: int) -> None:
    """List memory entries."""
    from kyourai.memory.builtin import BuiltinMemoryProvider

    provider = BuiltinMemoryProvider()
    provider.initialize("cli-list")
    store = provider.store

    table = Table(title="Memory Entries")
    table.add_column("Target", style="cyan")
    table.add_column("Entry", style="white")

    if target in ("memory", "all"):
        for entry in store.memory_entries[:limit]:
            table.add_row("memory", entry)
    if target in ("user", "all"):
        for entry in store.user_entries[:limit]:
            table.add_row("user", entry)

    console.print(table)
    provider.shutdown()


@memory.command("add")
@click.option("--target", type=click.Choice(["memory", "user"]), required=True)
@click.option("--content", prompt="Content", help="Memory entry content")
def memory_add(target: str, content: str) -> None:
    """Add a memory entry."""
    from kyourai.memory.builtin import BuiltinMemoryProvider

    provider = BuiltinMemoryProvider()
    provider.initialize("cli-add")
    result = json.loads(provider.handle_tool_call("memory", {"action": "add", "target": target, "content": content}))
    if result.get("success"):
        console.print(f"[green]Added to {target}:[/green] {content}")
    else:
        console.print(f"[red]Failed:[/red] {result.get('error', 'unknown')}")
    provider.shutdown()


@memory.command("search")
@click.argument("query")
@click.option("--category", default=None)
@click.option("--limit", default=10)
def memory_search(query: str, category: str | None, limit: int) -> None:
    """Search holographic facts."""
    from kyourai.memory.holographic.store import MemoryStore
    from kyourai.memory.holographic.retrieval import FactRetriever

    store = MemoryStore()
    retriever = FactRetriever(store=store, hrr_dim=store.hrr_dim)
    results = retriever.search(query, category=category, limit=limit)

    if not results:
        console.print("[dim]No results found.[/dim]")
    else:
        table = Table(title=f"Search: {query}")
        table.add_column("Score", style="cyan", justify="right")
        table.add_column("Trust", style="yellow", justify="right")
        table.add_column("Content", style="white")
        table.add_column("Category", style="dim")
        for r in results:
            table.add_row(
                f"{r.get('score', 0):.3f}",
                f"{r.get('trust_score', 0):.2f}",
                r.get("content", "")[:80],
                r.get("category", ""),
            )
        console.print(table)
    store.close()


@memory.command("facts")
@click.option("--category", default=None)
@click.option("--limit", default=20)
def memory_facts(category: str | None, limit: int) -> None:
    """List holographic facts."""
    from kyourai.memory.holographic.store import MemoryStore

    store = MemoryStore()
    facts = store.list_facts(category=category, limit=limit)

    if not facts:
        console.print("[dim]No facts stored.[/dim]")
    else:
        table = Table(title="Holographic Facts")
        table.add_column("ID", style="dim", justify="right")
        table.add_column("Trust", style="yellow", justify="right")
        table.add_column("Content", style="white")
        table.add_column("Category", style="cyan")
        for f in facts:
            table.add_row(
                str(f["fact_id"]),
                f"{f['trust_score']:.2f}",
                f["content"][:80],
                f["category"],
            )
        console.print(table)
    store.close()


@memory.command("export")
@click.argument("path")
@click.option("--no-builtin", is_flag=True, help="Skip builtin memory")
@click.option("--no-holographic", is_flag=True, help="Skip holographic facts")
def memory_export(path: str, no_builtin: bool, no_holographic: bool) -> None:
    """Export memory to a KPC (Kyourai Portable Context) file."""
    from kyourai.mcp.portable_context import export_to_file
    from kyourai.memory.builtin import BuiltinMemoryProvider
    from kyourai.memory.holographic.store import MemoryStore

    builtin = BuiltinMemoryProvider()
    builtin.initialize("cli-export")
    holo = MemoryStore()

    export_to_file(
        path,
        include_builtin=not no_builtin,
        include_holographic=not no_holographic,
        builtin_provider=builtin,
        holographic_store=holo,
    )
    console.print(f"[green]Exported memory to {path}[/green]")
    builtin.shutdown()
    holo.close()


@memory.command("import")
@click.argument("path")
@click.option("--strategy", type=click.Choice(["skip_duplicates", "overwrite", "append"]), default="skip_duplicates")
def memory_import(path: str, strategy: str) -> None:
    """Import memory from a KPC file."""
    from kyourai.mcp.portable_context import import_from_file
    from kyourai.memory.builtin import BuiltinMemoryProvider
    from kyourai.memory.holographic.store import MemoryStore

    builtin = BuiltinMemoryProvider()
    builtin.initialize("cli-import")
    holo = MemoryStore()

    summary = import_from_file(
        path,
        merge_strategy=strategy,
        builtin_provider=builtin,
        holographic_store=holo,
    )
    console.print(f"[green]Import complete:[/green] {summary}")
    builtin.shutdown()
    holo.close()


# ---------------------------------------------------------------------------
# team — team management commands
# ---------------------------------------------------------------------------

@main.group()
def team() -> None:
    """Manage teams and members."""


@team.command("create")
@click.option("--name", prompt="Team name", help="Team name")
@click.option("--user-id", prompt="Your user ID", help="Your user ID")
@click.option("--display-name", prompt="Your display name", help="Your display name")
def team_create(name: str, user_id: str, display_name: str) -> None:
    """Create a new team."""
    from kyourai.team import TeamManager

    ensure_home()
    tm = TeamManager()
    team = tm.create_team(name, creator_user_id=user_id, creator_display_name=display_name)
    console.print(f"[green]Created team:[/green] {team.team_name}")
    console.print(f"  Team ID: [cyan]{team.team_id}[/cyan]")
    console.print(f"  Your role: admin")


@team.command("list")
def team_list() -> None:
    """List all teams."""
    from kyourai.team import TeamManager

    tm = TeamManager()
    teams = tm.list_teams()
    if not teams:
        console.print("[dim]No teams found.[/dim]")
        return

    table = Table(title="Teams")
    table.add_column("Team ID", style="cyan")
    table.add_column("Name", style="white")
    table.add_column("Members", justify="right")
    table.add_column("Created", style="dim")
    for t in teams:
        table.add_row(t["team_id"], t["team_name"], str(t["member_count"]), t.get("created_at", "")[:10])
    console.print(table)


@team.command("add-member")
@click.option("--team-id", required=True, help="Team ID")
@click.option("--user-id", required=True, help="New member's user ID")
@click.option("--display-name", required=True, help="New member's display name")
@click.option("--role", type=click.Choice(["member", "editor", "admin"]), default="member")
@click.option("--added-by", required=True, help="Your user ID (must be admin)")
def team_add_member(team_id: str, user_id: str, display_name: str, role: str, added_by: str) -> None:
    """Add a member to a team."""
    from kyourai.team import TeamManager, Role

    tm = TeamManager()
    try:
        member = tm.add_member(team_id, user_id, display_name, Role(role), added_by=added_by)
        console.print(f"[green]Added {member.display_name} as {member.role.value}[/green]")
    except (ValueError, PermissionError) as e:
        console.print(f"[red]Error:[/red] {e}")


# ---------------------------------------------------------------------------
# curator — memory maintenance commands
# ---------------------------------------------------------------------------

@main.group()
def curator() -> None:
    """Manage the memory curator."""


@curator.command("run")
@click.option("--force", is_flag=True, help="Force run (bypass interval gate)")
def curator_run(force: bool) -> None:
    """Run the curator manually."""
    from kyourai.memory.holographic.store import MemoryStore
    from kyourai.memory import curator as cur

    store = MemoryStore()
    summary = cur.run_curator(store, force=force)
    console.print("[green]Curator run complete:[/green]")
    console.print_json(data=summary)
    store.close()


@curator.command("status")
def curator_status() -> None:
    """Show curator state."""
    from kyourai.memory import curator as cur

    state = cur.load_state()
    table = Table(title="Curator State")
    table.add_column("Key", style="cyan")
    table.add_column("Value", style="white")
    for k, v in state.items():
        table.add_row(k, str(v)[:80])
    console.print(table)


@curator.command("pause")
def curator_pause() -> None:
    """Pause the curator."""
    from kyourai.memory import curator as cur
    cur.set_paused(True)
    console.print("[yellow]Curator paused.[/yellow]")


@curator.command("resume")
def curator_resume() -> None:
    """Resume the curator."""
    from kyourai.memory import curator as cur
    cur.set_paused(False)
    console.print("[green]Curator resumed.[/green]")


# ---------------------------------------------------------------------------
# mcp-server — start the MCP server
# ---------------------------------------------------------------------------

@main.command("mcp-server")
def mcp_server() -> None:
    """Start the MCP server (for other agents to access Kyourai memory)."""
    from kyourai.mcp.server import main as mcp_main
    console.print("[dim]Starting MCP server on stdio...[/dim]")
    mcp_main()


# ---------------------------------------------------------------------------
# config — configuration commands
# ---------------------------------------------------------------------------

@main.group()
def config() -> None:
    """Manage configuration."""


@config.command("show")
def config_show() -> None:
    """Show current configuration."""
    cfg = load_config()
    if not cfg:
        console.print("[dim]No config.yaml found. Run 'kyourai config init' to create one.[/dim]")
        return
    console.print_json(data=cfg)


@config.command("init")
def config_init() -> None:
    """Create a default config.yaml."""
    ensure_home()
    path = get_kyourai_home() / "config.yaml"
    if path.exists():
        if not click.confirm("config.yaml already exists. Overwrite?"):
            return

    default_config = {
        "agent": {
            "model": "openai:gpt-4o",
        },
        "memory": {
            "holographic": {
                "hrr_dim": 1024,
                "default_trust": 0.5,
                "min_trust_threshold": 0.3,
            },
        },
        "curator": {
            "enabled": True,
            "interval_hours": 168,
            "min_idle_hours": 2,
            "stale_after_days": 30,
            "trust_floor": 0.1,
            "pin_threshold": 3,
        },
    }
    save_config(default_config)
    console.print(f"[green]Created config.yaml at {path}[/green]")


# ---------------------------------------------------------------------------
# skills — skill management commands
# ---------------------------------------------------------------------------

@main.group()
def skills() -> None:
    """Manage skills."""


@skills.command("list")
def skills_list() -> None:
    """List all loaded skills."""
    from kyourai.skills import SkillLoader

    loader = SkillLoader()
    loader.load_all()
    all_skills = loader.all_skills()

    if not all_skills:
        console.print("[dim]No skills found.[/dim]")
        return

    table = Table(title="Skills")
    table.add_column("Name", style="cyan")
    table.add_column("Description", style="white")
    table.add_column("Eligible", style="green")
    table.add_column("Source", style="dim")
    for s in all_skills:
        eligible = "[green]yes[/green]" if s.eligible else f"[red]no ({s.skip_reason})[/red]"
        table.add_row(s.name, s.description[:60], eligible, str(s.source_path.parent)[-40:])
    console.print(table)


@skills.command("show")
@click.argument("name")
def skills_show(name: str) -> None:
    """Show a skill's full content."""
    from kyourai.skills import SkillLoader

    loader = SkillLoader()
    loader.load_all()
    skill = loader.get_skill(name)
    if skill is None:
        console.print(f"[red]Skill '{name}' not found or not eligible.[/red]")
        return
    console.print(Panel(skill.body, title=f"Skill: {skill.name}", border_style="cyan"))


@skills.command("create")
@click.option("--name", required=True, help="Skill name (lowercase, hyphens)")
@click.option("--description", required=True, help="One-line description")
@click.option("--dir", "target_dir", default=None, help="Target directory (default: ~/.kyourai/skills/<name>)")
def skills_create(name: str, description: str, target_dir: str | None) -> None:
    """Create a new skill scaffold."""
    from kyourai.constants import get_skills_dir

    target = Path(target_dir) if target_dir else get_skills_dir() / name
    target.mkdir(parents=True, exist_ok=True)
    skill_file = target / "SKILL.md"
    if skill_file.exists():
        console.print(f"[red]SKILL.md already exists at {skill_file}[/red]")
        return

    content = f"""---
name: {name}
description: {description}
---

# {name.title().replace('-', ' ')}

TODO: Add instructions for the agent here.
"""
    skill_file.write_text(content, encoding="utf-8")
    console.print(f"[green]Created skill at {skill_file}[/green]")


# ---------------------------------------------------------------------------
# cron — scheduled task commands
# ---------------------------------------------------------------------------

@main.group()
def cron() -> None:
    """Manage scheduled tasks."""


@cron.command("list")
def cron_list() -> None:
    """List all scheduled tasks."""
    from kyourai.cron import CronScheduler

    scheduler = CronScheduler()
    tasks = scheduler.list_tasks()
    if not tasks:
        console.print("[dim]No cron tasks found.[/dim]")
        return

    table = Table(title="Cron Tasks")
    table.add_column("ID", style="cyan")
    table.add_column("Schedule", style="yellow")
    table.add_column("Action", style="white")
    table.add_column("Enabled", style="green")
    table.add_column("Next Run", style="dim")
    table.add_column("Runs", justify="right")
    for t in tasks:
        enabled = "[green]yes[/green]" if t.enabled else "[red]no[/red]"
        table.add_row(t.task_id, t.cron_expr, t.action, enabled, (t.next_run_at or "")[:19], str(t.run_count))
    console.print(table)


@cron.command("add")
@click.option("--id", "task_id", required=True, help="Unique task ID")
@click.option("--schedule", required=True, help="Cron expression (e.g. '0 9 * * *')")
@click.option("--action", type=click.Choice(["agent_turn", "tool", "curator"]), default="curator")
@click.option("--prompt", default="", help="Prompt for agent_turn, or tool args")
@click.option("--skill", default="", help="Skill reference for agent_turn")
def cron_add(task_id: str, schedule: str, action: str, prompt: str, skill: str) -> None:
    """Add a scheduled task."""
    from kyourai.cron import CronScheduler

    scheduler = CronScheduler()
    task = scheduler.add_task(task_id, schedule, action=action, prompt=prompt, skill=skill)
    console.print(f"[green]Added cron task:[/green] {task.task_id}")
    console.print(f"  Schedule: {task.cron_expr}")
    console.print(f"  Action: {task.action}")
    console.print(f"  Next run: {task.next_run_at}")


@cron.command("remove")
@click.argument("task_id")
def cron_remove(task_id: str) -> None:
    """Remove a scheduled task."""
    from kyourai.cron import CronScheduler

    scheduler = CronScheduler()
    if scheduler.remove_task(task_id):
        console.print(f"[green]Removed task:[/green] {task_id}")
    else:
        console.print(f"[red]Task not found:[/red] {task_id}")


@cron.command("run")
@click.argument("task_id")
def cron_run(task_id: str) -> None:
    """Run a task immediately."""
    from kyourai.cron import CronScheduler

    scheduler = CronScheduler()
    result = scheduler.run_task_now(task_id)
    if result is None:
        console.print(f"[red]Task not found:[/red] {task_id}")
    else:
        console.print(f"[green]Task result:[/green]")
        console.print(result[:500])


@cron.command("enable")
@click.argument("task_id")
def cron_enable(task_id: str) -> None:
    """Enable a task."""
    from kyourai.cron import CronScheduler
    scheduler = CronScheduler()
    if scheduler.enable_task(task_id):
        console.print(f"[green]Enabled:[/green] {task_id}")
    else:
        console.print(f"[red]Task not found:[/red] {task_id}")


@cron.command("disable")
@click.argument("task_id")
def cron_disable(task_id: str) -> None:
    """Disable a task."""
    from kyourai.cron import CronScheduler
    scheduler = CronScheduler()
    if scheduler.disable_task(task_id):
        console.print(f"[yellow]Disabled:[/yellow] {task_id}")
    else:
        console.print(f"[red]Task not found:[/red] {task_id}")


# ---------------------------------------------------------------------------
# serve — start the OpenAI-compatible API server
# ---------------------------------------------------------------------------

@main.command("serve")
@click.option("--host", default="127.0.0.1", help="Bind host")
@click.option("--port", default=18789, help="Bind port")
@click.option("--model", default=None, help="Default model (e.g. openai:gpt-4o)")
@click.option("--api-key", default=None, help="API key for auth (default: KYOURAI_API_KEY env)")
def serve(host: str, port: int, model: str | None, api_key: str | None) -> None:
    """Start the OpenAI-compatible API server."""
    from kyourai.api import run_server

    model = model or get_config_value("agent.model", "openai:gpt-4o")
    console.print(Panel.fit(
        f"[bold cyan]Kyourai API Server[/bold cyan]\n"
        f"Host: {host}:{port}\n"
        f"Model: {model}\n"
        f"API: /v1/chat/completions, /v1/models, /v1/sessions, /v1/insights\n"
        f"\n[dim]Dashboard: cd dashboard && npm run dev[/dim]\n"
        f"[dim]  → http://localhost:3000 (proxies API to :{port})[/dim]",
        border_style="cyan",
    ))
    run_server(host=host, port=port, model=model, api_key=api_key)


# ---------------------------------------------------------------------------
# sessions — session history management
# ---------------------------------------------------------------------------

@main.group()
def sessions() -> None:
    """Browse and search session history."""


@sessions.command("list")
@click.option("--limit", default=20, help="Max sessions to show")
@click.option("--source", default=None, help="Filter by source (cli/api/team)")
def sessions_list(limit: int, source: str | None) -> None:
    """List recent sessions."""
    from kyourai.state import SessionDB

    db = SessionDB()
    rows = db.list_sessions(limit=limit, source=source)
    db.close()

    if not rows:
        console.print("[dim]No sessions found.[/dim]")
        return

    table = Table(title="Recent Sessions", show_lines=False)
    table.add_column("ID", style="cyan", no_wrap=True, max_width=20)
    table.add_column("Source", style="magenta")
    table.add_column("Model", style="blue")
    table.add_column("Msgs", justify="right")
    table.add_column("Tools", justify="right")
    table.add_column("Started", style="dim")

    for r in rows:
        started = r.get("started_at")
        if started:
            from datetime import datetime
            ts = datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M")
        else:
            ts = ""
        table.add_row(
            r.get("id", "")[:20],
            r.get("source", ""),
            (r.get("model") or "")[:25],
            str(r.get("message_count", 0)),
            str(r.get("tool_call_count", 0)),
            ts,
        )
    console.print(table)


@sessions.command("search")
@click.argument("query")
@click.option("--limit", default=20, help="Max results")
def sessions_search(query: str, limit: int) -> None:
    """Search session messages with full-text search."""
    from kyourai.state import SessionDB

    db = SessionDB()
    results = db.search_messages(query, limit=limit)
    db.close()

    if not results:
        console.print(f"[dim]No results for '{query}'.[/dim]")
        return

    console.print(f"[bold]Found {len(results)} result(s) for '{query}':[/bold]\n")
    for r in results:
        from datetime import datetime
        ts = datetime.fromtimestamp(r.get("timestamp", 0)).strftime("%Y-%m-%d %H:%M")
        role = r.get("role", "")
        title = r.get("title") or r.get("session_id", "")[:20]
        snippet = r.get("snippet", r.get("content", "")[:100])
        console.print(
            f"  [cyan]{ts}[/cyan] [{role}] [dim]{title}[/dim]\n"
            f"  {snippet}\n"
        )


@sessions.command("show")
@click.argument("session_id")
@click.option("--limit", default=100, help="Max messages to show")
def sessions_show(session_id: str, limit: int) -> None:
    """Show messages from a specific session."""
    from kyourai.state import SessionDB

    db = SessionDB()
    session = db.get_session(session_id)
    if not session:
        console.print(f"[red]Session '{session_id}' not found.[/red]")
        db.close()
        return

    msgs = db.get_messages(session_id, limit=limit)
    db.close()

    console.print(Panel.fit(
        f"[bold]{session.get('title') or session_id}[/bold]\n"
        f"Source: {session.get('source', '')}  "
        f"Model: {session.get('model', '')}  "
        f"Messages: {session.get('message_count', 0)}",
        border_style="cyan",
    ))

    for m in msgs:
        role = m.get("role", "")
        content = m.get("content", "")
        if not content:
            continue
        if role == "user":
            console.print(f"[bold green]user[/bold green]: {content[:200]}")
        elif role == "assistant":
            console.print(f"[bold blue]assistant[/bold blue]: {content[:200]}")
        elif role == "tool":
            console.print(f"[dim]tool({m.get('tool_name', '')}): {content[:100]}[/dim]")


# ---------------------------------------------------------------------------
# mcp — MCP server catalog management
# ---------------------------------------------------------------------------

@main.group("mcp")
def mcp_group() -> None:
    """MCP server catalog — discover, register, and connect to MCP servers."""


@mcp_group.command("list")
def mcp_list() -> None:
    """List registered MCP servers."""
    from kyourai.mcp.catalog import MCPCatalog

    catalog = MCPCatalog()
    servers = catalog.list_servers()

    if not servers:
        console.print("[dim]No MCP servers registered. Use 'kyourai mcp bundled' to see available servers.[/dim]")
        return

    table = Table(title="Registered MCP Servers")
    table.add_column("Name", style="cyan")
    table.add_column("Transport", style="blue")
    table.add_column("Enabled", justify="center")
    table.add_column("Connected", justify="center")
    table.add_column("Description", style="dim")

    for s in servers:
        enabled = "[green]✓[/green]" if s.enabled else "[red]✗[/red]"
        connected = "[green]✓[/green]" if s.connected else "[dim]—[/dim]"
        table.add_row(s.name, s.transport, enabled, connected, s.description)

    console.print(table)


@mcp_group.command("bundled")
def mcp_bundled() -> None:
    """List bundled MCP server templates."""
    from kyourai.mcp.catalog import MCPCatalog

    catalog = MCPCatalog()
    bundled = catalog.list_bundled()

    table = Table(title="Bundled MCP Server Templates")
    table.add_column("Name", style="cyan")
    table.add_column("Command", style="blue")
    table.add_column("Registered", justify="center")
    table.add_column("Description", style="dim")

    for b in bundled:
        registered = "[green]✓[/green]" if b["registered"] else "[dim]—[/dim]"
        cmd = f"{b['command']} {' '.join(b['args'][:1])}"
        table.add_row(b["name"], cmd, registered, b["description"])

    console.print(table)
    console.print("\n[dim]Register with: kyourai mcp register-bundled <name>[/dim]")


@mcp_group.command("register-bundled")
@click.argument("name")
def mcp_register_bundled(name: str) -> None:
    """Register a bundled MCP server by name."""
    from kyourai.mcp.catalog import MCPCatalog

    catalog = MCPCatalog()
    cfg = catalog.register_bundled(name)

    if cfg:
        console.print(f"[green]Registered MCP server:[/green] {name}")
        console.print(f"  Command: {cfg.command} {' '.join(cfg.args)}")
        console.print(f"  Description: {cfg.description}")
    else:
        console.print(f"[red]Unknown bundled server:[/red] {name}")
        console.print("[dim]Use 'kyourai mcp bundled' to see available servers.[/dim]")


@mcp_group.command("register")
@click.argument("name")
@click.option("--command", required=True, help="Command to run")
@click.option("--args", default="", help="Arguments (space-separated)")
@click.option("--transport", default="stdio", help="Transport: stdio, sse, streamable_http")
@click.option("--url", default=None, help="URL for SSE/HTTP transport")
@click.option("--description", default="", help="Description")
@click.option("--auto-connect", is_flag=True, help="Auto-connect on agent startup")
def mcp_register(
    name: str,
    command: str,
    args: str,
    transport: str,
    url: str | None,
    description: str,
    auto_connect: bool,
) -> None:
    """Register a custom MCP server."""
    from kyourai.mcp.catalog import MCPCatalog

    catalog = MCPCatalog()
    arg_list = args.split() if args else []
    cfg = catalog.register(
        name=name,
        command=command,
        args=arg_list,
        transport=transport,
        url=url,
        description=description,
        auto_connect=auto_connect,
    )
    console.print(f"[green]Registered MCP server:[/green] {name}")
    console.print(f"  Command: {cfg.command} {' '.join(cfg.args)}")


@mcp_group.command("unregister")
@click.argument("name")
def mcp_unregister(name: str) -> None:
    """Remove an MCP server from the catalog."""
    from kyourai.mcp.catalog import MCPCatalog

    catalog = MCPCatalog()
    if catalog.unregister(name):
        console.print(f"[green]Unregistered:[/green] {name}")
    else:
        console.print(f"[red]Not found:[/red] {name}")


@mcp_group.command("connect")
@click.argument("name")
def mcp_connect(name: str) -> None:
    """Connect to an MCP server."""
    from kyourai.mcp.catalog import MCPCatalog

    catalog = MCPCatalog()
    result = catalog.connect(name)

    if not result:
        console.print(f"[red]Server not found:[/red] {name}")
        return

    if result.connected:
        console.print(f"[green]Connected:[/green] {name}")
    elif result.error:
        console.print(f"[red]Failed:[/red] {result.error}")


@mcp_group.command("disconnect")
@click.argument("name")
def mcp_disconnect(name: str) -> None:
    """Disconnect from an MCP server."""
    from kyourai.mcp.catalog import MCPCatalog

    catalog = MCPCatalog()
    if catalog.disconnect(name):
        console.print(f"[green]Disconnected:[/green] {name}")
    else:
        console.print(f"[red]Not connected:[/red] {name}")


@mcp_group.command("status")
def mcp_status() -> None:
    """Show status of all MCP servers."""
    from kyourai.mcp.catalog import MCPCatalog

    catalog = MCPCatalog()
    statuses = catalog.status()

    if not statuses:
        console.print("[dim]No MCP servers registered.[/dim]")
        return

    table = Table(title="MCP Server Status")
    table.add_column("Name", style="cyan")
    table.add_column("Enabled", justify="center")
    table.add_column("Connected", justify="center")
    table.add_column("Auto-Connect", justify="center")
    table.add_column("Error", style="red")

    for s in statuses:
        enabled = "[green]✓[/green]" if s["enabled"] else "[red]✗[/red]"
        connected = "[green]✓[/green]" if s["connected"] else "[dim]—[/dim]"
        auto = "[green]✓[/green]" if s["auto_connect"] else "[dim]—[/dim]"
        table.add_row(s["name"], enabled, connected, auto, s.get("error") or "")

    console.print(table)


# ---------------------------------------------------------------------------
# usage — token usage and cost tracking
# ---------------------------------------------------------------------------

@main.command("usage")
@click.option("--days", default=30, help="Days to look back")
@click.option("--by-model", is_flag=True, help="Break down by model")
def usage(days: int, by_model: bool) -> None:
    """Show token usage and estimated costs."""
    from kyourai.usage import UsageTracker

    tracker = UsageTracker()

    if by_model:
        by_model_data = tracker.get_by_model(days=days)
        if not by_model_data:
            console.print(f"[dim]No usage data in the last {days} days.[/dim]")
            return

        table = Table(title=f"Usage by Model (Last {days} days)")
        table.add_column("Model", style="cyan")
        table.add_column("Prompt Tokens", justify="right")
        table.add_column("Completion Tokens", justify="right")
        table.add_column("Total Tokens", justify="right")
        table.add_column("Cost (USD)", justify="right", style="green")
        table.add_column("Calls", justify="right")

        for model, total in sorted(by_model_data.items(), key=lambda x: x[1].total_cost_usd, reverse=True):
            table.add_row(
                model,
                f"{total.total_prompt_tokens:,}",
                f"{total.total_completion_tokens:,}",
                f"{total.total_tokens:,}",
                f"${total.total_cost_usd:.4f}",
                str(total.entry_count),
            )
        console.print(table)
    else:
        total = tracker.get_totals(days=days)
        if total.entry_count == 0:
            console.print(f"[dim]No usage data in the last {days} days.[/dim]")
            return

        console.print(Panel.fit(
            f"[bold]Usage (Last {days} days)[/bold]\n\n"
            f"Prompt tokens:     [cyan]{total.total_prompt_tokens:,}[/cyan]\n"
            f"Completion tokens: [cyan]{total.total_completion_tokens:,}[/cyan]\n"
            f"Total tokens:      [cyan]{total.total_tokens:,}[/cyan]\n"
            f"Estimated cost:    [green]${total.total_cost_usd:.4f}[/green]\n"
            f"API calls:         {total.entry_count}",
            border_style="cyan",
        ))


# ---------------------------------------------------------------------------
# context — show detected coding context
# ---------------------------------------------------------------------------

@main.command("context")
def context() -> None:
    """Show detected coding context for the current directory."""
    from kyourai.context.coding import detect_coding_context

    ctx = detect_coding_context()

    console.print(Panel.fit(
        f"[bold]Coding Context[/bold]\n\n"
        f"Directory:       {ctx.directory}\n"
        f"Project:         {ctx.project_name or '(unknown)'}\n"
        f"Git repo:        {'yes' if ctx.is_git_repo else 'no'}\n"
        f"Git branch:      {ctx.git_branch or '-'}\n"
        f"Git status:      {ctx.git_status or '-'}\n"
        f"Git remote:      {ctx.git_remote or '-'}\n"
        f"Languages:       {', '.join(ctx.languages) or '-'}\n"
        f"Primary:         {ctx.primary_language or '-'}\n"
        f"Frameworks:      {', '.join(ctx.frameworks) or '-'}\n"
        f"Package managers:{', '.join(ctx.package_managers) or '-'}\n"
        f"Test frameworks: {', '.join(ctx.test_frameworks) or '-'}\n"
        f"Linters:         {', '.join(ctx.linters) or '-'}\n"
        f"Has README:      {'yes' if ctx.has_readme else 'no'}\n"
        f"Has tests:       {'yes' if ctx.has_tests else 'no'}",
        border_style="cyan",
    ))


# ---------------------------------------------------------------------------
# insights — usage analytics
# ---------------------------------------------------------------------------

@main.command("insights")
@click.option("--days", default=30, help="Days to look back")
def insights(days: int) -> None:
    """Show usage insights and analytics."""
    from kyourai.state import SessionDB, InsightsEngine

    db = SessionDB()
    engine = InsightsEngine(db)
    report = engine.generate(days=days)
    db.close()

    if report.get("empty"):
        console.print(f"[dim]No session data in the last {days} days.[/dim]")
        return

    ov = report["overview"]
    console.print(Panel.fit(
        f"[bold cyan]Kyourai Insights — Last {days} days[/bold cyan]",
        border_style="cyan",
    ))

    # Overview
    table = Table(title="Overview", show_header=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Sessions", str(ov.get("total_sessions", 0)))
    table.add_row("Active sessions", str(ov.get("active_sessions", 0)))
    table.add_row("Messages", str(ov.get("total_messages", 0)))
    table.add_row("Tool calls", str(ov.get("total_tool_calls", 0)))
    table.add_row("Facts stored", str(ov.get("total_facts", 0)))
    table.add_row("Avg msgs/session", str(ov.get("avg_messages_per_session", 0)))
    console.print(table)

    # Models
    if report.get("models"):
        table = Table(title="Models")
        table.add_column("Model", style="blue")
        table.add_column("Sessions", justify="right")
        table.add_column("Messages", justify="right")
        table.add_column("Tool calls", justify="right")
        for m in report["models"]:
            table.add_row(
                m["model"],
                str(m["sessions"]),
                str(m["messages"]),
                str(m["tool_calls"]),
            )
        console.print(table)

    # Tools
    if report.get("tools"):
        table = Table(title="Tool Usage")
        table.add_column("Tool", style="magenta")
        table.add_column("Calls", justify="right")
        for t in report["tools"]:
            table.add_row(t["tool_name"], str(t["call_count"]))
        console.print(table)

    # Activity
    activity = report.get("activity", {})
    if activity.get("by_day"):
        console.print("\n[bold]Activity (messages per day):[/bold]")
        for day, count in activity["by_day"][-14:]:  # last 14 days
            bar = "█" * min(count, 40)
            console.print(f"  {day} [dim]{bar}[/dim] {count}")


# ---------------------------------------------------------------------------
# health — detailed health check
# ---------------------------------------------------------------------------

@main.command("health")
def health() -> None:
    """Run a detailed health check of all Kyourai components."""
    from kyourai.production import HealthChecker

    checker = HealthChecker()
    report = checker.check_all()

    status_color = "green" if report.all_healthy else "red"
    console.print(Panel.fit(
        f"[bold]Kyourai Health Check[/bold]\n"
        f"Status: [{status_color}]{report.status.upper()}[/{status_color}]\n"
        f"Version: {report.version}",
        border_style=status_color,
    ))

    table = Table(title="Components")
    table.add_column("Component", style="cyan")
    table.add_column("Status", justify="center")
    table.add_column("Detail", style="dim")
    table.add_column("Latency (ms)", justify="right")

    for comp in report.components:
        status = "[green]✓ healthy[/green]" if comp.healthy else "[red]✗ unhealthy[/red]"
        table.add_row(
            comp.name,
            status,
            comp.detail,
            f"{comp.latency_ms:.1f}",
        )

    console.print(table)


# ---------------------------------------------------------------------------
# config-validate — validate config file
# ---------------------------------------------------------------------------

@main.command("config-validate")
def config_validate() -> None:
    """Validate config.yaml and show any errors or warnings."""
    from kyourai.production import validate_config

    result = validate_config()

    if result.valid:
        console.print("[green]✓ Config is valid[/green]")
    else:
        console.print("[red]✗ Config has errors:[/red]")
        for err in result.errors:
            console.print(f"  [red]• {err}[/red]")

    if result.warnings:
        console.print("\n[yellow]Warnings:[/yellow]")
        for warn in result.warnings:
            console.print(f"  [yellow]• {warn}[/yellow]")

    if result.migrated:
        console.print("\n[blue]Config was migrated to current version.[/blue]")


# ---------------------------------------------------------------------------
# providers — list available LLM providers
# ---------------------------------------------------------------------------

@main.command("providers")
def providers() -> None:
    """List available LLM providers and their status."""
    from kyourai.providers import list_providers

    provider_list = list_providers()

    table = Table(title="Available LLM Providers")
    table.add_column("Provider", style="cyan")
    table.add_column("Default Model", style="blue")
    table.add_column("Env Key", style="magenta")
    table.add_column("Status", justify="center")
    table.add_column("Description", style="dim")

    for p in provider_list:
        status = "[green]✓ Ready[/green]" if p["has_key"] else "[red]✗ No key[/red]"
        table.add_row(
            p["name"],
            p["default_model"],
            p["env_key"],
            status,
            p["description"],
        )

    console.print(table)
    console.print(
        "\n[dim]Set API keys via environment variables. "
        "Use provider:model format (e.g. anthropic:claude-3.5-sonnet).[/dim]"
    )


# ---------------------------------------------------------------------------
# tui — terminal UI
# ---------------------------------------------------------------------------

@main.command("tui")
@click.option("--model", default=None, help="Model to use (e.g. openai:gpt-4o)")
def tui(model: str | None) -> None:
    """Start the terminal UI (TUI) for interactive chat."""
    model = model or get_config_value("agent.model", "openai:gpt-4o")

    try:
        from kyourai.tui import run_tui
        run_tui(model=model)
    except ImportError:
        console.print(
            "[red]TUI requires 'textual' package. Install with:[/red]\n"
            "  pip install textual"
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
