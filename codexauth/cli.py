"""Click CLI commands."""

import asyncio
import json
from datetime import datetime
from pathlib import Path

import click

from codexauth.config import get_sync_dir
from codexauth.git_sync import GitCommandError, pull_sync_repo, push_sync_repo
from codexauth.oauth import OAuthError, begin_login, clear_pending_login, exchange_code
from codexauth.reconcile import reconcile_active_to_store, reconcile_imported_active_profile
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
        "Use `login` to bootstrap a new ChatGPT-backed profile through a browser-based OAuth flow.\n\n"
        "Sync setup:\n\n"
        "\b\n"
        "  Add CODEXAUTH_SYNC_DIR=/path/to/profiles to a repo-local .env file.\n"
        "  `pull` runs git pull and then imports from that directory.\n"
        "  `push` exports to that directory and then runs git add/commit/push.\n\n"
        "Typical workflow order:\n\n"
        "\b\n"
        "  Pull shared changes and import them:\n"
        "    codexauth pull\n\n"
        "\b\n"
        "  Export local changes and publish them:\n"
        "    codexauth push\n\n"
        "Examples:\n\n"
        "\b\n"
        "  codexauth add work\n"
        "  codexauth pull\n"
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
    _show_profiles(no_interactive=no_interactive, no_usage=no_usage)


def _show_profiles(no_interactive: bool, no_usage: bool) -> None:
    """Render stored profiles and optionally prompt for activation."""
    reconcile_result = _run_preflight_reconciliation(prompt_on_unsafe=False)

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

    console.print(f"[dim]{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}[/dim]")
    console.print(render_table(profiles, all_data, usage_map, active))
    _maybe_offer_push_after_reconcile(reconcile_result, allow_prompt=not no_interactive)

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
    reconcile_result = _run_preflight_reconciliation(prompt_on_unsafe=True)
    _activate(name)
    _maybe_offer_push_after_reconcile(reconcile_result, allow_prompt=True)


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
    "login",
    short_help="Bootstrap a new ChatGPT-backed profile via manual OAuth.",
    help=(
        "Start a browser-assisted OAuth login for a named profile.\n\n"
        "The command prints an authorization URL, asks you to open it in a browser, and then prompts for the full\n"
        "localhost callback URL after login. A browser connection error at the localhost redirect is expected."
    ),
)
@click.argument("name", required=False)
def login_cmd(name):
    """Bootstrap a ChatGPT-backed profile using a manual browser OAuth flow."""
    try:
        auth_url = begin_login(name)
        console.print("Open this URL in your browser:")
        console.print(auth_url)
        console.print(
            "[dim]After login, your browser may show a localhost connection error. "
            "That is expected. Copy the full callback URL from the address bar and paste it below.[/dim]"
        )
        callback_url = click.prompt("Callback URL", type=str)
        profile = asyncio.run(exchange_code(callback_url))
        final_name = name or click.prompt("Profile name", type=str).strip()
        if not final_name:
            raise click.ClickException("Profile name cannot be empty.")
        save_profile(final_name, profile)
        clear_pending_login()
    except OAuthError as e:
        raise click.ClickException(str(e))

    console.print(f"[green]✓[/green] Saved profile [bold]{final_name}[/bold]")
    _show_profiles(no_interactive=True, no_usage=True)


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
    "reconcile-active",
    hidden=True,
    help="Reconcile the active stored profile with ~/.codex/auth.json.",
)
def reconcile_active_cmd():
    """Reconcile the active stored profile with ~/.codex/auth.json."""
    result = reconcile_active_to_store(prompt_on_unsafe=True)
    _report_reconcile_result(result)
    _maybe_offer_push_after_reconcile(result, allow_prompt=True)


@cli.command(
    "import",
    short_help="Import profiles from CODEXAUTH_SYNC_DIR.",
    hidden=True,
    help=(
        "Import all profile JSON files from CODEXAUTH_SYNC_DIR into ~/.codexauth/tokens.\n\n"
        "The sync directory is read from CODEXAUTH_SYNC_DIR in a repo-local .env file.\n"
        "This command imports every discovered profile by default. If the incoming profile is older than the\n"
        "existing local profile, it shows both modified timestamps and asks for confirmation."
    ),
)
def import_cmd():
    """Import all profiles from CODEXAUTH_SYNC_DIR into local storage."""
    sync_dir = _require_sync_dir()
    _run_import(sync_dir)


@cli.command(
    "export",
    short_help="Export profiles to CODEXAUTH_SYNC_DIR.",
    hidden=True,
    help=(
        "Export all local profiles from ~/.codexauth/tokens into CODEXAUTH_SYNC_DIR.\n\n"
        "The sync directory is read from CODEXAUTH_SYNC_DIR in a repo-local .env file.\n"
        "This command exports every stored profile by default. If the local profile is older than the existing file\n"
        "in the sync directory, it shows both modified timestamps and asks for confirmation."
    ),
)
def export_cmd():
    """Export all local profiles into CODEXAUTH_SYNC_DIR."""
    sync_dir = _require_sync_dir()
    _run_export(sync_dir)


@cli.command(
    "pull",
    short_help="Pull the sync repo, then import profiles.",
    help=(
        "Run `git pull` in CODEXAUTH_SYNC_DIR, then import all profiles from that directory into local storage.\n\n"
        "Overwrite cases still ask for confirmation during the import step."
    ),
)
def pull_cmd():
    """Run git pull, then import profiles from CODEXAUTH_SYNC_DIR."""
    sync_dir = _require_sync_dir()
    preflight_result = _run_preflight_reconciliation(prompt_on_unsafe=True)
    try:
        message = pull_sync_repo(sync_dir)
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except GitCommandError as e:
        raise click.ClickException(e.message)
    console.print(f"[green]✓[/green] Pulled sync repo [bold]{sync_dir}[/bold]")
    if message:
        console.print(f"[dim]{message}[/dim]")
    imported_names = _run_import(sync_dir)
    post_import_result = reconcile_imported_active_profile(imported_names)
    _report_reconcile_result(post_import_result)
    active = get_active()
    imported_active = bool(active and active in imported_names)
    push_candidate = (
        post_import_result if imported_active else preflight_result
    )
    _maybe_offer_push_after_reconcile(push_candidate, allow_prompt=True)


@cli.command(
    "push",
    short_help="Export profiles, then commit and push sync changes.",
    help=(
        "Export all local profiles into CODEXAUTH_SYNC_DIR, then run Git publication steps there.\n\n"
        "This command runs:\n"
        "\n"
        "\b\n"
        "  export local profiles into CODEXAUTH_SYNC_DIR\n"
        "  git add .\n"
        "  git commit -m \"Update exported codexauth profiles\"\n"
        "  git push\n\n"
        "If `git add .` leaves no staged changes, the command exits successfully without committing or pushing."
    ),
)
def push_cmd():
    """Export profiles, then run git add/commit/push in CODEXAUTH_SYNC_DIR."""
    sync_dir = _require_sync_dir()
    _push_sync_changes(sync_dir)


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


def _run_import(sync_dir: Path) -> set[str]:
    candidates = build_import_candidates(sync_dir)
    if not candidates:
        console.print(f"[dim]No profiles found in [bold]{sync_dir}[/bold].[/dim]")
        return set()

    imported = 0
    imported_names: set[str] = set()
    for candidate in candidates:
        if candidate.should_confirm_overwrite and not _confirm_overwrite(
            "import", candidate, "external", "local"
        ):
            continue
        import_profile(candidate.name, candidate.source_path)
        imported += 1
        imported_names.add(candidate.name)
        console.print(f"[green]✓[/green] Imported profile [bold]{candidate.name}[/bold]")

    if imported == 0:
        console.print("[dim]No profiles imported.[/dim]")
    return imported_names


def _run_export(sync_dir: Path) -> None:
    candidates = build_export_candidates(sync_dir)
    if not candidates:
        console.print("[dim]No local profiles stored to export.[/dim]")
        return

    exported = 0
    for candidate in candidates:
        if candidate.should_confirm_overwrite and not _confirm_overwrite(
            "export", candidate, "local", "external"
        ):
            continue
        export_profile(candidate.name, candidate.dest_path)
        exported += 1
        console.print(f"[green]✓[/green] Exported profile [bold]{candidate.name}[/bold]")

    if exported == 0:
        console.print("[dim]No profiles exported.[/dim]")


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


def _report_reconcile_result(result) -> None:
    if result.status == "updated":
        console.print(f"[green]✓[/green] {result.message}")
    elif result.status == "warning":
        console.print(f"[yellow]![/yellow] {result.message}")
    elif result.status == "unsafe":
        console.print(f"[yellow]![/yellow] {result.message}")


def _run_preflight_reconciliation(prompt_on_unsafe: bool):
    result = reconcile_active_to_store(prompt_on_unsafe=prompt_on_unsafe)
    if result.status == "updated":
        console.print(f"[green]✓[/green] {result.message}")
    elif result.status == "warning":
        console.print(f"[yellow]![/yellow] {result.message}")
    elif result.status == "unsafe":
        console.print(f"[yellow]![/yellow] {result.message}")
    return result


def _maybe_offer_push_after_reconcile(result, allow_prompt: bool) -> None:
    if not allow_prompt or not result or not result.store_updated_from_auth:
        return

    sync_dir = get_sync_dir()
    if sync_dir is None:
        return

    banner = "#" * 48
    console.print(f"[bold red]{banner}[/bold red]")
    console.print("[bold red]##### An app updated local auth.json       #####[/bold red]")
    console.print(f"[bold red]{banner}[/bold red]")
    console.print(f"##### Updating local store now...          #####")
    console.print(f"##### Successfully reconciled local store. #####")
    if _confirm_yes_no("Reconciliation updated local store. Push these changes now? [y/N]: "):
        _push_sync_changes(sync_dir)


def _push_sync_changes(sync_dir: Path) -> None:
    _run_export(sync_dir)
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


def _confirm_yes_no(prompt: str) -> bool:
    while True:
        value = click.prompt(prompt, prompt_suffix="", default="", show_default=False).strip().lower()
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
