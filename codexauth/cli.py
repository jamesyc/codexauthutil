"""Click CLI commands."""

import asyncio
import json
from pathlib import Path

import click

from codexauth.config import get_sync_dir
from codexauth import store
from codexauth.display import console, interactive_prompt, render_table
from codexauth.store import (
    ProfileNotFoundError,
    activate,
    delete_profile,
    get_active,
    list_profiles,
    load_profile,
    save_profile_from_file,
)
from codexauth.sync import (
    SyncCandidate,
    build_export_candidates,
    build_import_candidates,
    export_profile,
    format_modified,
    import_profile,
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
    save_profile_from_file(name, src, preserve_mtime=True)
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


@cli.command("import")
def import_cmd():
    """Import profiles from the configured sync directory."""
    sync_dir = _require_sync_dir()
    candidates = build_import_candidates(sync_dir)
    if not candidates:
        console.print(f"[dim]No profiles found in [bold]{sync_dir}[/bold].[/dim]")
        return

    selected = _select_candidates("Import profiles", candidates, "local")
    if not selected:
        return

    imported = 0
    for candidate in selected:
        if candidate.will_overwrite and not _confirm_overwrite("import", candidate, "external", "local"):
            continue
        import_profile(candidate.name, candidate.source_path)
        imported += 1
        console.print(f"[green]✓[/green] Imported profile [bold]{candidate.name}[/bold]")

    if imported == 0:
        console.print("[dim]No profiles imported.[/dim]")


@cli.command("export")
def export_cmd():
    """Export profiles to the configured sync directory."""
    sync_dir = _require_sync_dir()
    candidates = build_export_candidates(sync_dir)
    if not candidates:
        console.print("[dim]No local profiles stored to export.[/dim]")
        return

    selected = _select_candidates("Export profiles", candidates, "external")
    if not selected:
        return

    exported = 0
    for candidate in selected:
        if candidate.will_overwrite and not _confirm_overwrite("export", candidate, "local", "external"):
            continue
        export_profile(candidate.name, candidate.dest_path)
        exported += 1
        console.print(f"[green]✓[/green] Exported profile [bold]{candidate.name}[/bold]")

    if exported == 0:
        console.print("[dim]No profiles exported.[/dim]")


def _activate(name: str):
    try:
        activate(name)
    except ProfileNotFoundError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/green] Activated profile [bold]{name}[/bold]")


def _require_sync_dir() -> Path:
    sync_dir = get_sync_dir()
    if sync_dir is None:
        raise click.ClickException(
            "Missing CODEXAUTH_SYNC_DIR in .env. Add CODEXAUTH_SYNC_DIR=/path/to/profiles."
        )
    return sync_dir


def _select_candidates(title: str, candidates: list[SyncCandidate], destination_label: str) -> list[SyncCandidate]:
    console.print(f"[bold]{title}[/bold]")
    for idx, candidate in enumerate(candidates, 1):
        suffix = ""
        if candidate.will_overwrite:
            suffix = (
                f" [yellow](overwrites {destination_label}; src {format_modified(candidate.source_modified)}, "
                f"dest {format_modified(candidate.dest_modified)})[/yellow]"
            )
        console.print(f"{idx}. {candidate.name}{suffix}")

    raw_choice = click.prompt(
        "Select profiles by number (comma-separated), 'all', or 'q'",
        default="q",
        show_default=False,
    ).strip()
    if raw_choice.lower() in {"q", ""}:
        return []
    if raw_choice.lower() == "all":
        return candidates

    selected: list[SyncCandidate] = []
    seen: set[int] = set()
    for chunk in raw_choice.split(","):
        part = chunk.strip()
        try:
            idx = int(part)
        except ValueError as exc:
            raise click.ClickException(f"Invalid selection '{part}'.") from exc
        if idx < 1 or idx > len(candidates):
            raise click.ClickException(f"Selection '{idx}' is out of range.")
        if idx not in seen:
            selected.append(candidates[idx - 1])
            seen.add(idx)
    return selected


def _confirm_overwrite(
    action: str,
    candidate: SyncCandidate,
    source_label: str,
    destination_label: str,
) -> bool:
    return click.confirm(
        (
            f"{action.title()} profile '{candidate.name}' from {source_label} modified "
            f"{format_modified(candidate.source_modified)} over {destination_label} modified "
            f"{format_modified(candidate.dest_modified)}?"
        ),
        default=False,
    )
