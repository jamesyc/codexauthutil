"""Click CLI commands."""

import asyncio
import json
from pathlib import Path

import click

from codexauth import store
from codexauth.display import console, interactive_prompt, render_table
from codexauth.store import (
    ProfileNotFoundError,
    activate,
    delete_profile,
    get_active,
    list_profiles,
    load_profile,
    save_profile,
)
from codexauth.usage import UsageResult, fetch_all_usage


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """Manage multiple OpenAI Codex auth.json profiles."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(list_cmd)


@cli.command("list")
@click.option("--no-interactive", is_flag=True, help="Skip the activation prompt.")
@click.option("--no-usage", is_flag=True, help="Skip fetching usage data (faster).")
def list_cmd(no_interactive, no_usage):
    """List all profiles with usage stats."""
    profiles = list_profiles()
    if not profiles:
        console.print(
            "[dim]No profiles stored. Run [bold]codexauth add <name>[/bold] to add one.[/dim]"
        )
        return

    active = get_active()
    all_data = {n: load_profile(n) for n in profiles}

    if no_usage:
        usage_map = {n: UsageResult(error="n/a") for n in profiles}
    else:
        with console.status("[dim]Fetching usage...[/dim]"):
            usage_map = asyncio.run(fetch_all_usage(all_data))

    console.print(render_table(profiles, all_data, usage_map, active))

    if not no_interactive:
        choice = interactive_prompt(profiles)
        if choice:
            _activate(choice)


@cli.command("use")
@click.argument("name")
def use_cmd(name):
    """Activate a profile by name."""
    _activate(name)


@cli.command("add")
@click.argument("name")
@click.option(
    "--file", "file_path",
    default=None,
    type=click.Path(exists=True),
    help="Path to auth.json (defaults to ~/.codex/auth.json).",
)
def add_cmd(name, file_path):
    """Save an auth.json as a named profile."""
    src = Path(file_path) if file_path else store.CODEX_AUTH
    if not src.exists():
        raise click.ClickException(f"{src} does not exist.")
    data = json.loads(src.read_text())
    if "auth_mode" not in data and "tokens" not in data:
        raise click.ClickException("File doesn't look like a valid auth.json.")
    save_profile(name, data)
    console.print(f"[green]✓[/green] Saved profile [bold]{name}[/bold]")


@cli.command("remove")
@click.argument("name")
def remove_cmd(name):
    """Delete a stored profile."""
    try:
        delete_profile(name)
    except ProfileNotFoundError as e:
        raise click.ClickException(str(e))
    if get_active() == name:
        store.ACTIVE_FILE.unlink(missing_ok=True)
    console.print(f"[green]✓[/green] Removed profile [bold]{name}[/bold]")


@cli.command("status")
def status_cmd():
    """Show the currently active profile."""
    active = get_active()
    if active:
        console.print(f"Active: [bold green]{active}[/bold green]")
    else:
        console.print("[dim]No profile currently active.[/dim]")


def _activate(name: str):
    try:
        activate(name)
    except ProfileNotFoundError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/green] Activated profile [bold]{name}[/bold]")
