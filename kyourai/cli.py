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
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(name)s: %(message)s")


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
        f"Endpoints: /v1/chat/completions, /v1/models, /health",
        border_style="cyan",
    ))
    run_server(host=host, port=port, model=model, api_key=api_key)


if __name__ == "__main__":
    main()
