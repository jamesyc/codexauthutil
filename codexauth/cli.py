"""Click CLI commands."""

import asyncio
import json
from pathlib import Path

import click

from codexauth.config import get_sync_dir
from codexauth.git_sync import GitCommandError, pull_sync_repo, push_sync_repo
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


@click.group(
    invoke_without_command=True,
    context_settings={"max_content_width": 100},
    help=(
        "Manage multiple OpenAI Codex auth.json profiles.\n\n"
        "Profiles are stored locally in ~/.codexauth/tokens as named copies of Codex auth.json files.\n"
        "The active profile is copied into ~/.codex/auth.json when you run `use` or activate one from `list`.\n\n"
        "ChatGPT-backed profiles refresh tokens automatically during usage lookup in `list` when the stored\n"
        "refresh timestamp is stale or missing.\n\n"
        "Sync setup:\n\n"
        "\b\n"
        "  Add CODEXAUTH_SYNC_DIR=/path/to/profiles to a repo-local .env file.\n"
        "  `import` and `export` copy profile JSON files between local storage and that directory.\n"
        "  `pull` and `push` run Git commands inside that same sync directory.\n\n"
        "Typical workflow order:\n\n"
        "\b\n"
        "  Pull shared changes first, then import:\n"
        "    codexauth pull\n"
        "    codexauth import\n\n"
        "\b\n"
        "  Export, then publish:\n"
        "    codexauth export\n"
        "    codexauth push\n\n"
        "Examples:\n\n"
        "\b\n"
        "  codexauth add work\n"
        "  codexauth pull\n"
        "  codexauth import\n"
        "  codexauth export\n"
        "  codexauth push"
    ),
)
@click.pass_context
def cli(ctx):
    if ctx.invoked_subcommand is None:
        ctx.invoke(list_cmd)


@cli.command(
    "list",
    short_help="List profiles, usage, and activation options.",
    help=(
        "List stored profiles in ~/.codexauth/tokens.\n\n"
        "For ChatGPT-backed profiles, this command fetches live quota usage and automatically refreshes tokens\n"
        "first when `last_refresh` is missing, invalid, or at least 8 days old.\n\n"
        "By default, `list` also prompts you to activate one of the shown profiles after rendering the table."
    ),
)
@click.option(
    "--no-interactive",
    is_flag=True,
    help="Show the table only and skip the prompt to activate a profile.",
)
@click.option(
    "--no-usage",
    is_flag=True,
    help="Skip live quota lookups and show profiles immediately.",
)
def list_cmd(no_interactive, no_usage):
    """List profiles, auto-refresh stale ChatGPT tokens during usage lookup, and offer activation."""
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


@cli.command(
    "use",
    short_help="Activate a stored profile.",
    help=(
        "Activate a stored profile by copying ~/.codexauth/tokens/<name>.json into ~/.codex/auth.json.\n\n"
        "If ~/.codex/auth.json already exists, it is first backed up to ~/.codexauth/auth.json.bak."
    ),
)
@click.argument("name")
def use_cmd(name):
    """Activate a stored profile by copying it into ~/.codex/auth.json."""
    _activate(name)


@cli.command(
    "add",
    short_help="Save the current auth.json as a named profile.",
    help=(
        "Save an auth.json file as a named profile in ~/.codexauth/tokens.\n\n"
        "By default this reads ~/.codex/auth.json. Use --file to save a different auth.json.\n"
        "The source file's modified time is preserved so import/export overwrite prompts can compare timestamps."
    ),
)
@click.argument("name")
@click.option(
    "--file", "file_path",
    default=None,
    type=click.Path(exists=True),
    help="Read auth.json from this path instead of the default ~/.codex/auth.json.",
)
def add_cmd(name, file_path):
    """Save the current auth.json as a named profile in ~/.codexauth/tokens."""
    src = Path(file_path) if file_path else store.CODEX_AUTH
    if not src.exists():
        raise click.ClickException(f"{src} does not exist.")
    data = json.loads(src.read_text())
    if "auth_mode" not in data and "tokens" not in data:
        raise click.ClickException("File doesn't look like a valid auth.json.")
    save_profile_from_file(name, src, preserve_mtime=True)
    console.print(f"[green]✓[/green] Saved profile [bold]{name}[/bold]")


@cli.command(
    "remove",
    short_help="Delete a stored profile.",
    help=(
        "Delete a stored profile from ~/.codexauth/tokens.\n\n"
        "If the deleted profile is currently marked active, the active marker file is cleared."
    ),
)
@click.argument("name")
def remove_cmd(name):
    """Delete a stored profile and clear the active marker if it was selected."""
    try:
        delete_profile(name)
    except ProfileNotFoundError as e:
        raise click.ClickException(str(e))
    if get_active() == name:
        store.ACTIVE_FILE.unlink(missing_ok=True)
    console.print(f"[green]✓[/green] Removed profile [bold]{name}[/bold]")


@cli.command(
    "status",
    short_help="Show the active profile.",
    help="Show which stored profile name is currently recorded in ~/.codexauth/active.",
)
def status_cmd():
    """Show which stored profile is currently marked active."""
    active = get_active()
    if active:
        console.print(f"Active: [bold green]{active}[/bold green]")
    else:
        console.print("[dim]No profile currently active.[/dim]")


@cli.command(
    "import",
    short_help="Import profiles from CODEXAUTH_SYNC_DIR.",
    help=(
        "Import selected profile JSON files from CODEXAUTH_SYNC_DIR into ~/.codexauth/tokens.\n\n"
        "The sync directory is read from CODEXAUTH_SYNC_DIR in a repo-local .env file.\n"
        "When a selected profile would overwrite an existing local profile, this command shows both modified\n"
        "timestamps and asks for confirmation."
    ),
)
def import_cmd():
    """Import selected profiles from CODEXAUTH_SYNC_DIR into local storage."""
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


@cli.command(
    "export",
    short_help="Export profiles to CODEXAUTH_SYNC_DIR.",
    help=(
        "Export selected local profiles from ~/.codexauth/tokens into CODEXAUTH_SYNC_DIR.\n\n"
        "The sync directory is read from CODEXAUTH_SYNC_DIR in a repo-local .env file.\n"
        "When a selected export would overwrite an existing file in the sync directory, this command shows both\n"
        "modified timestamps and asks for confirmation."
    ),
)
def export_cmd():
    """Export selected local profiles into CODEXAUTH_SYNC_DIR."""
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


@cli.command(
    "pull",
    short_help="Run git pull in the sync directory.",
    help=(
        "Run `git pull` in CODEXAUTH_SYNC_DIR.\n\n"
        "Use this before `import` when the sync directory is a Git repository that receives shared profile updates."
    ),
)
def pull_cmd():
    """Run git pull in CODEXAUTH_SYNC_DIR before importing shared profiles."""
    sync_dir = _require_sync_dir()
    try:
        message = pull_sync_repo(sync_dir)
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except GitCommandError as e:
        raise click.ClickException(e.message)
    console.print(f"[green]✓[/green] Pulled sync repo [bold]{sync_dir}[/bold]")
    if message:
        console.print(f"[dim]{message}[/dim]")


@cli.command(
    "push",
    short_help="Stage, commit, and push sync-directory changes.",
    help=(
        "Run Git publication steps in CODEXAUTH_SYNC_DIR after exporting profiles.\n\n"
        "This command runs:\n"
        "\n"
        "\b\n"
        "  git add .\n"
        "  git commit -m \"Update exported codexauth profiles\"\n"
        "  git push\n\n"
        "If `git add .` leaves no staged changes, the command exits successfully without committing or pushing."
    ),
)
def push_cmd():
    """Run git add/commit/push in CODEXAUTH_SYNC_DIR after exporting profiles."""
    sync_dir = _require_sync_dir()
    try:
        message = push_sync_repo(sync_dir)
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except GitCommandError as e:
        raise click.ClickException(e.message)

    if message == "No changes to commit.":
        console.print(f"[dim]{message}[/dim]")
        return

    console.print(f"[green]✓[/green] Pushed sync repo [bold]{sync_dir}[/bold]")
    if message:
        console.print(f"[dim]{message}[/dim]")


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
