"""Click CLI commands."""

import asyncio
import json
import shutil
from contextlib import nullcontext
from datetime import datetime
from functools import wraps
from pathlib import Path
from time import monotonic, sleep

import click
from rich.control import Control
from rich.markup import escape
from rich.text import Text

from codexauth.autosync import SyncError, sync_once
from codexauth.config import get_sync_dir
from codexauth.credentials import credential_summary
from codexauth.git_sync import (
    GitCommandError,
    pull_sync_repo,
    push_sync_repo,
    sync_local_profile_excludes,
)
from codexauth.locking import SyncBusyError, sync_lock
from codexauth.oauth import OAuthError, begin_login, clear_pending_login, exchange_code
from codexauth.reconcile import reconcile_active_to_store, reconcile_imported_active_profile
from codexauth import store
from codexauth.display import console, interactive_prompt, render_table
from codexauth.store import (
    ProfileNotFoundError,
    activate,
    delete_profile,
    get_active,
    hide_profile,
    list_hidden_profiles,
    list_profiles,
    list_visible_profiles,
    load_profile,
    save_profile,
    save_profile_from_file,
    unhide_profile,
)
from codexauth.sync import (
    SyncCandidate,
    build_export_candidates,
    build_import_candidates,
    export_hidden_profiles,
    export_profile,
    format_modified,
    import_hidden_profiles,
    import_profile,
    list_blacklisted_profiles,
    read_profile,
)
from codexauth.usage import UsageResult, fetch_all_usage
from codexauth.weekly import (
    DEFAULT_WEEKLY_START_MODEL,
    WeeklyStartResult,
    start_weekly_timer,
    weekly_window_needs_start,
)

WATCH_INTERVAL_SECONDS = 10


def _locked_sync_action(action):
    @wraps(action)
    def wrapped(*args, **kwargs):
        try:
            with sync_lock():
                return action(*args, **kwargs)
        except SyncBusyError as exc:
            raise click.ClickException(str(exc)) from exc
    return wrapped


@click.group(
    invoke_without_command=True,
    context_settings={"max_content_width": 100},
    help=(
        "Manage multiple OpenAI Codex auth.json profiles.\n\n"
        "Profiles are stored locally in ~/.codexauth/tokens as named copies of Codex auth.json files.\n"
        "The active profile is copied into ~/.codex/auth.json when you run `use` or activate one from `list`.\n\n"
        "Run without a command to sync configured accounts, then show live usage and activation options.\n\n"
        "ChatGPT-backed profiles refresh tokens automatically during usage lookup in `list` when the stored\n"
        "refresh timestamp is stale or missing.\n\n"
        "Use `login` to bootstrap a new ChatGPT-backed profile through a browser-based OAuth flow.\n\n"
        "Sync setup:\n\n"
        "\b\n"
        "  Add CODEXAUTH_SYNC_DIR=/path/to/profiles to a repo-local .env file.\n"
        "  Running without a command syncs before showing the table.\n"
        "  `watch` syncs and refreshes the table every 10 seconds.\n"
        "  `sync` merges newer credentials in both directions without showing the table.\n\n"
        "Typical workflow:\n\n"
        "\b\n"
        "  Sync accounts and show live usage:\n"
        "    codexauth\n\n"
        "Examples:\n\n"
        "\b\n"
        "  codexauth add work\n"
        "  codexauth\n"
        "  codexauth watch"
    ),
)
@click.pass_context
def cli(ctx):
    if ctx.invoked_subcommand is None:
        _show_profiles(no_interactive=False, no_usage=False, auto_sync=True)


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
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Include hidden profiles and show Mode, 5-hour usage, and Spark columns.",
)
def list_cmd(no_interactive, no_usage, show_all):
    """List profiles, auto-refresh stale ChatGPT tokens during usage lookup, and offer activation."""
    _show_profiles(no_interactive=no_interactive, no_usage=no_usage, show_all=show_all)


@cli.command(
    "watch",
    short_help="Sync and watch all profiles every 10 seconds.",
    help=(
        "Check all stored profiles, including hidden profiles, immediately and every 10 seconds.\n\n"
        "When CODEXAUTH_SYNC_DIR is configured, sync before each usage lookup. Sync failures are reported "
        "alongside the local table and retried on the next check.\n\n"
        "Shows the detailed usage table and refreshes stale tokens as `list` does. Profiles and the active "
        "account are reloaded on every check. Slow checks finish before the next one starts.\n\n"
        "The current table stays visible while sync and usage refresh in the background, then updates when ready. "
        "Redirected output keeps each snapshot. "
        "No activation or sync prompts are shown. Press Ctrl+C to stop."
    ),
)
@click.option(
    "--interval", type=click.IntRange(min=1), default=WATCH_INTERVAL_SECONDS,
    show_default=True, help="Seconds between sync and usage checks.",
)
def watch_cmd(interval):
    """Continuously sync and show live status for every stored profile without prompting."""
    try:
        while True:
            started = monotonic()
            with console.capture() as capture:
                console.print(
                    f"[dim]Watching all profiles every {interval} seconds. "
                    "Press Ctrl+C to stop.[/dim]"
                )
                _show_profiles(
                    no_interactive=True, no_usage=False, show_all=True, show_progress=False,
                    auto_sync=True,
                )
            # Leave the previous snapshot visible until the next one is complete,
            # then send the clear and replacement together in one buffered write.
            with console:
                if console.is_terminal:
                    console.print(Control.clear(), Control.home(), end="")
                console.print(Text.from_ansi(capture.get()), end="", soft_wrap=True)
            # Keep checks on cadence without overlapping slow requests.
            sleep(max(0, interval - (monotonic() - started)))
    except KeyboardInterrupt:
        console.print("[dim]Stopped watching profiles.[/dim]")


@cli.command(
    "start-weekly",
    short_help="Start unset weekly usage windows with a minimal Codex request.",
    help=(
        "Start unset weekly usage windows for stored ChatGPT-backed profiles.\n\n"
        "This fetches current usage, then sends minimal Codex requests for each selected profile whose weekly "
        "reset timestamp is missing or whose exact reset-after value is the full seven days. The requests run in "
        "temporary isolated Codex homes and do not change the active profile. With no NAME arguments, all stored "
        "profiles are checked, including hidden profiles."
    ),
)
@click.argument("names", nargs=-1)
@click.option(
    "--model",
    default=DEFAULT_WEEKLY_START_MODEL,
    show_default=True,
    help="Model override, or auto to discover small models and verify the weekly timer.",
)
@click.option("-y", "--yes", is_flag=True, help="Skip the confirmation prompt.")
def start_weekly_cmd(names, model, yes):
    """Start missing weekly timers without switching the active profile."""
    stored_names = list_profiles()
    if not stored_names:
        console.print(
            "[dim]No profiles stored. Run [bold]codexauth add <name>[/bold] to add one.[/dim]"
        )
        return

    selected_names = list(dict.fromkeys(names)) if names else stored_names
    unknown = sorted(set(selected_names) - set(stored_names))
    if unknown:
        raise click.ClickException(
            "Profile{} not found: {}".format(
                "s" if len(unknown) != 1 else "", ", ".join(unknown)
            )
        )

    profiles = {name: load_profile(name) for name in selected_names}
    with console.status("[dim]Checking weekly usage windows...[/dim]"):
        usage_summary = asyncio.run(fetch_all_usage(profiles))

    candidates = [
        name
        for name in selected_names
        if profiles[name].get("auth_mode") == "chatgpt"
        and weekly_window_needs_start(usage_summary.usage_map[name])
    ]
    unavailable = [
        name
        for name in selected_names
        if profiles[name].get("auth_mode") == "chatgpt"
        and usage_summary.usage_map[name].error is not None
    ]
    if unavailable:
        console.print(
            "[yellow]Could not determine weekly usage for:[/yellow] "
            + ", ".join(escape(name) for name in unavailable)
        )
    if not candidates:
        console.print("[dim]No selected profiles have an unset weekly usage window.[/dim]")
        return

    console.print(
        "Weekly usage window is unset for: "
        + ", ".join(escape(name) for name in candidates)
    )
    if not yes and not click.confirm(
        f"Send minimal Codex requests for {len(candidates)} profile(s)?",
        default=False,
    ):
        console.print("[dim]Cancelled.[/dim]")
        return

    results: dict[str, WeeklyStartResult] = {}
    for name in candidates:
        with console.status(f"[dim]Starting weekly window for {escape(name)}...[/dim]"):
            result = start_weekly_timer(load_profile(name), model=model)
        if result.updated_profile is not None:
            save_profile(name, result.updated_profile)
        results[name] = result

    successful_names = [name for name, result in results.items() if result.succeeded]
    verified_usage = {}
    if successful_names:
        refreshed_profiles = {name: load_profile(name) for name in successful_names}
        with console.status("[dim]Verifying weekly usage windows...[/dim]"):
            verified_usage = asyncio.run(fetch_all_usage(refreshed_profiles)).usage_map

    failures = 0
    for name in candidates:
        result = results[name]
        display_name = escape(name)
        if result.model:
            display_name += f" ({escape(result.model)})"
        if not result.succeeded:
            failures += 1
            console.print(
                f"[red]✗[/red] [bold]{display_name}[/bold]: "
                f"{escape(result.detail or 'unknown error')}"
            )
        elif verified_usage[name].error is not None:
            console.print(
                f"[yellow]•[/yellow] [bold]{display_name}[/bold]: request succeeded; "
                "could not verify the weekly window"
            )
        elif verified_usage[name].secondary_reset_at is None:
            console.print(
                f"[yellow]•[/yellow] [bold]{display_name}[/bold]: request succeeded; "
                "weekly window is not visible yet"
            )
        elif weekly_window_needs_start(verified_usage[name]):
            console.print(
                f"[yellow]•[/yellow] [bold]{display_name}[/bold]: request succeeded; "
                "weekly timer unverified (API still reports the full 7-day placeholder)"
            )
        else:
            console.print(
                f"[green]✓[/green] [bold]{display_name}[/bold]: weekly window started"
            )

    if failures:
        raise click.ClickException(f"Failed to start {failures} weekly usage window(s).")


def _show_profiles(
    no_interactive: bool,
    no_usage: bool,
    show_all: bool = False,
    *,
    show_progress: bool = True,
    auto_sync: bool = False,
) -> None:
    """Render stored profiles and optionally prompt for activation."""
    synced = _sync_for_display() if auto_sync else False
    ctx = click.get_current_context(silent=True)
    reconcile_result = _run_preflight_reconciliation(prompt_on_unsafe=False)

    all_profiles = list_profiles()
    if not all_profiles:
        console.print(
            "[dim]No profiles stored. Run [bold]codexauth add <name>[/bold] to add one.[/dim]"
        )
        return

    hidden_profiles = list_hidden_profiles()
    profiles = all_profiles if show_all else list_visible_profiles()
    if not profiles:
        console.print(
            "[dim]All profiles are hidden. Run [bold]codexauth list --all[/bold] to show them.[/dim]"
        )
        return

    active = get_active()
    all_data = {n: load_profile(n) for n in profiles}

    if no_usage:
        usage_map = {n: UsageResult(error="n/a") for n in profiles}
        refreshed_profiles: list[str] = []
    else:
        progress = console.status("[dim]Fetching usage...[/dim]") if show_progress else nullcontext()
        with progress:
            usage_summary = asyncio.run(fetch_all_usage(all_data))
        usage_map = usage_summary.usage_map
        refreshed_profiles = usage_summary.refreshed_profiles

    local_only = store.list_local_only_profiles()
    syncable_refreshed = [name for name in refreshed_profiles if name not in local_only]
    syncable_reconcile = (
        reconcile_result.store_updated_from_auth and get_active() not in local_only
    )

    console.print(f"[dim]{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}[/dim]")
    terminal_width = getattr(ctx, "terminal_width", None) if ctx else None
    if terminal_width is None:
        terminal_width = shutil.get_terminal_size(fallback=(console.width, 24)).columns
    console.print(
        render_table(
            profiles,
            all_data,
            usage_map,
            active,
            width=terminal_width,
            hidden_profiles=hidden_profiles if show_all else None,
            show_details=show_all,
        )
    )
    if auto_sync:
        # Usage lookup can rotate tokens after the initial sync. Publish those
        # updates automatically while the table remains visible.
        if synced and (syncable_refreshed or syncable_reconcile):
            _sync_for_display()
    else:
        _maybe_offer_push_after_list_updates(
            reconcile_result=reconcile_result,
            refreshed_profiles=syncable_refreshed,
            allow_prompt=not no_interactive,
        )

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
    reconciled_name = get_active()
    reconcile_result = _run_preflight_reconciliation(prompt_on_unsafe=True)
    _activate(name)
    _maybe_offer_push_after_reconcile(
        reconcile_result, allow_prompt=True, profile_name=reconciled_name
    )


@cli.command(
    "add",
    short_help="Save the current auth.json as a named profile.",
    help=(
        "Save an auth.json file as a named profile in ~/.codexauth/tokens.\n\n"
        "By default this reads ~/.codex/auth.json. Use --file to save a different auth.json.\n"
        "The source file's modified time is preserved. Sync compares credential refresh and token issue times."
    ),
)
@click.argument("name")
@click.option(
    "--file", "file_path",
    default=None,
    type=click.Path(exists=True),
    help="Read auth.json from this path instead of the default ~/.codex/auth.json.",
)
@click.option(
    "--local-only",
    is_flag=True,
    help="Keep this profile on the current machine and exclude it from all sync operations.",
)
def add_cmd(name, file_path, local_only):
    """Save the current auth.json as a named profile in ~/.codexauth/tokens."""
    _validate_profile_name(name)
    src = Path(file_path) if file_path else store.CODEX_AUTH
    if not src.exists():
        raise click.ClickException(f"{src} does not exist.")
    try:
        data = json.loads(src.read_text())
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Failed to parse {src}: {exc.msg}") from exc

    _validate_auth_json(data)
    save_profile_from_file(name, src, preserve_mtime=True)
    if local_only:
        _mark_profile_local_only(name)
    suffix = " [dim](local only)[/dim]" if local_only else ""
    console.print(f"[green]✓[/green] Saved profile [bold]{name}[/bold]{suffix}")


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
@click.option(
    "--local-only",
    is_flag=True,
    help="Keep this profile on the current machine and exclude it from all sync operations.",
)
def login_cmd(name, local_only):
    """Bootstrap a ChatGPT-backed profile using a manual browser OAuth flow."""
    try:
        if name is not None:
            _validate_profile_name(name)
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
        _validate_profile_name(final_name)
        save_profile(final_name, profile)
        if local_only:
            _mark_profile_local_only(final_name)
        clear_pending_login()
    except OAuthError as e:
        raise click.ClickException(str(e))

    suffix = " [dim](local only)[/dim]" if local_only else ""
    console.print(f"[green]✓[/green] Saved profile [bold]{final_name}[/bold]{suffix}")
    _show_profiles(no_interactive=True, no_usage=True)


@cli.command(
    "hide",
    short_help="Hide a profile from the default list.",
    help=(
        "Hide a stored profile from the default `codexauth list` view without deleting it, banning it, "
        "or changing sync behavior.\n\n"
        "Use `codexauth list --all` to include hidden profiles."
    ),
)
@click.argument("name")
def hide_cmd(name):
    """Hide a stored profile from the default list view."""
    try:
        hide_profile(name)
    except ProfileNotFoundError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/green] Hidden profile [bold]{name}[/bold]")


@cli.command(
    "unhide",
    short_help="Show a hidden profile in the default list.",
    help="Restore a stored profile to the default `codexauth list` view.",
)
@click.argument("name")
def unhide_cmd(name):
    """Restore a stored profile to the default list view."""
    try:
        unhide_profile(name)
    except ProfileNotFoundError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/green] Unhidden profile [bold]{name}[/bold]")


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
    was_local_only = name in store.list_local_only_profiles()
    try:
        delete_profile(name)
    except ProfileNotFoundError as e:
        raise click.ClickException(str(e))
    if get_active() == name:
        store.ACTIVE_FILE.unlink(missing_ok=True)
    if was_local_only:
        _refresh_local_profile_excludes()
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
        "Newer credentials for the same account replace older local credentials automatically. Identical or "
        "older incoming copies are skipped. Only ambiguous freshness or account identity asks for confirmation."
    ),
)
@_locked_sync_action
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
        "Newer credentials for the same account replace older external credentials automatically. Identical or "
        "older local copies are skipped. Only ambiguous freshness or account identity asks for confirmation."
    ),
)
@_locked_sync_action
def export_cmd():
    """Export all local profiles into CODEXAUTH_SYNC_DIR."""
    sync_dir = _require_sync_dir()
    _run_export(sync_dir)


@cli.command(
    "sync",
    short_help="Merge and sync all accounts automatically.",
    help=(
        "Fetch shared changes, merge the newer credentials for each account, and publish local changes. "
        "Active credentials and hidden-profile preferences are synced too.\n\n"
        "No prompts are shown. Ambiguous or invalid profiles are left untouched and reported. "
        "Temporary failures and rejected pushes are retried up to three times with fresh comparisons. "
        "Only one local sync runs at a time.\n\n"
        "Runs once without showing the usage table and exits with code 2 if some profiles need attention. "
        "Use `watch` to keep syncing and showing live usage."
    ),
)
def sync_cmd():
    sync_dir = _require_sync_dir()
    try:
        report = _sync_now(sync_dir)
    except (SyncError, SyncBusyError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        console.print("[dim]Stopped syncing.[/dim]")
        return
    if report.skipped:
        raise click.exceptions.Exit(2)


def _sync_for_display() -> bool:
    """Sync when configured, keeping the local table available on failure."""
    try:
        sync_dir = get_sync_dir()
        if sync_dir is None:
            return False
        _sync_now(sync_dir)
    except (SyncError, SyncBusyError, OSError, ValueError) as exc:
        console.print(Text(f"Sync needs attention: {exc}\nShowing local profiles.", style="yellow"))
        return False
    return True


def _sync_timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _sync_now(sync_dir: Path):
    report = sync_once(sync_dir, notify=lambda message: console.print(Text(message, style="yellow")))
    for name, reason in sorted(report.skipped.items()):
        console.print(Text(f"Skipped {name}: {reason}", style="yellow"))
    summary = (
        f"{_sync_timestamp()} Synced: {len(report.imported)} imported, "
        f"{len(report.exported)} exported, {len(report.removed)} removed"
    )
    if report.skipped:
        summary += f"; {len(report.skipped)} need attention"
    console.print(Text(summary, style="yellow" if report.skipped else "dim"))
    return report


@cli.command(
    "pull",
    hidden=True,
    short_help="Pull the sync repo, then import profiles.",
    help=(
        "Run `git pull --no-rebase --no-edit` in CODEXAUTH_SYNC_DIR, then import all profiles from that "
        "directory into local storage.\n\n"
        "Credential refresh and token issue times determine which copy is newer. File modification times "
        "do not decide. Only ambiguous freshness or account identity asks for confirmation."
    ),
)
@_locked_sync_action
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
    hidden=True,
    short_help="Pull, merge newer credentials, and publish sync changes.",
    help=(
        "Update CODEXAUTH_SYNC_DIR, merge the newest credentials for each account, then publish the result.\n\n"
        "Newer external credentials are imported locally before export. Active credentials are reconciled too. "
        "Identical copies are skipped; ambiguous freshness or account identity asks for confirmation.\n\n"
        "This command runs:\n"
        "\n"
        "\b\n"
        "  git pull --no-rebase --no-edit\n"
        "  merge newer external credentials into local profiles\n"
        "  export local profiles into CODEXAUTH_SYNC_DIR\n"
        "  git add .\n"
        "  git commit -m \"Update exported codexauth profiles\" (when changed)\n"
        "  git pull --no-rebase --no-edit (after a commit, to close publication races)\n"
        "  git push\n\n"
        "If `git add .` leaves no staged changes, the command skips the commit but still pushes any existing local commits."
    ),
)
def push_cmd():
    """Update the sync repo, merge credentials, then export and publish."""
    sync_dir = _require_sync_dir()
    _push_sync_changes(sync_dir)


def _activate(name: str):
    try:
        activate(name)
    except ProfileNotFoundError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/green] Activated profile [bold]{name}[/bold]")


def _validate_profile_name(name: str) -> None:
    if (
        not name
        or name != name.strip()
        or name in {".", ".."}
        or Path(name).name != name
        or "\\" in name
        or "\n" in name
        or "\r" in name
    ):
        raise click.ClickException("Profile name must be a single, non-empty filename component.")


def _mark_profile_local_only(name: str) -> None:
    store.mark_profile_local_only(name)
    _refresh_local_profile_excludes()


def _refresh_local_profile_excludes() -> None:
    sync_dir = get_sync_dir()
    if sync_dir is None:
        return
    try:
        sync_local_profile_excludes(sync_dir, store.list_local_only_profiles())
    except (FileNotFoundError, GitCommandError, ValueError) as exc:
        detail = exc.message if isinstance(exc, GitCommandError) else str(exc)
        console.print(
            "[yellow]Profile remains local-only, but the Git exclude file could not be updated: "
            f"{escape(detail)}[/yellow]"
        )


def _validate_auth_json(data: object) -> None:
    if not isinstance(data, dict):
        raise click.ClickException("File doesn't look like a valid auth.json.")

    auth_mode = data.get("auth_mode")
    if auth_mode not in {"chatgpt", "api_key"}:
        raise click.ClickException(
            "auth.json must contain a supported auth_mode of 'chatgpt' or 'api_key'."
        )

    if auth_mode == "chatgpt":
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            raise click.ClickException("chatgpt auth.json must contain a tokens object.")
        access_token = tokens.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise click.ClickException(
                "chatgpt auth.json must contain tokens.access_token."
            )
        return

    api_key = data.get("OPENAI_API_KEY")
    if not isinstance(api_key, str) or not api_key.strip():
        raise click.ClickException(
            "api_key auth.json must contain a non-empty OPENAI_API_KEY."
        )


def _require_sync_dir() -> Path:
    sync_dir = get_sync_dir()
    if sync_dir is None:
        raise click.ClickException(
            "Missing CODEXAUTH_SYNC_DIR in .env. Add CODEXAUTH_SYNC_DIR=/path/to/profiles."
        )
    return sync_dir


def _run_import(sync_dir: Path) -> set[str]:
    local_only = store.list_local_only_profiles()
    blacklisted_names = set(list_blacklisted_profiles(sync_dir)) - local_only
    candidates = [
        candidate
        for candidate in build_import_candidates(sync_dir)
        if candidate.name not in blacklisted_names and candidate.name not in local_only
    ]
    imported = 0
    imported_names: set[str] = set()
    for candidate in candidates:
        if not _should_copy_profile("import", candidate, "external", "local"):
            continue
        import_profile(candidate.name, candidate.source_path)
        imported += 1
        imported_names.add(candidate.name)
        console.print(f"[green]✓[/green] Imported profile [bold]{candidate.name}[/bold]")

    removed = 0
    for name in sorted(blacklisted_names):
        try:
            delete_profile(name)
        except ProfileNotFoundError:
            continue
        if get_active() == name:
            store.ACTIVE_FILE.unlink(missing_ok=True)
        removed += 1
        console.print(
            f"[green]✓[/green] Removed blacklisted profile [bold]{name}[/bold]"
        )

    hidden_imported = import_hidden_profiles(sync_dir)
    if hidden_imported:
        console.print("[green]✓[/green] Imported hidden profile preferences")

    if imported == 0 and removed == 0 and not hidden_imported:
        if not candidates and not blacklisted_names:
            console.print(f"[dim]No profiles found in [bold]{sync_dir}[/bold].[/dim]")
        else:
            console.print("[dim]No profiles imported.[/dim]")
    elif imported == 0 and removed == 0:
        console.print("[dim]No profiles imported.[/dim]")
    return imported_names


def _run_export(sync_dir: Path) -> None:
    local_only = store.list_local_only_profiles()
    blacklisted_names = set(list_blacklisted_profiles(sync_dir))
    candidates = [
        candidate for candidate in build_export_candidates(sync_dir)
        if candidate.name not in blacklisted_names and candidate.name not in local_only
    ]
    hidden_exported = export_hidden_profiles(sync_dir)
    if not candidates:
        if hidden_exported:
            console.print("[green]✓[/green] Exported hidden profile preferences")
        else:
            console.print("[dim]No hidden profile preferences to export.[/dim]")
        console.print("[dim]No local profiles stored to export.[/dim]")
        return

    exported = 0
    for candidate in candidates:
        if not _should_copy_profile("export", candidate, "local", "external"):
            continue
        export_profile(candidate.name, candidate.dest_path)
        exported += 1
        console.print(f"[green]✓[/green] Exported profile [bold]{candidate.name}[/bold]")

    if hidden_exported:
        console.print("[green]✓[/green] Exported hidden profile preferences")

    if exported == 0:
        console.print("[dim]No profiles exported.[/dim]")


def _should_copy_profile(
    action: str,
    candidate: SyncCandidate,
    source_label: str,
    destination_label: str,
) -> bool:
    comparison = candidate.comparison
    if comparison.winner == "identical":
        console.print(f"[dim]Profile {escape(candidate.name)} is already in sync.[/dim]")
        return False
    if comparison.winner == "destination":
        console.print(
            f"[dim]Skipped {action} of {escape(candidate.name)}: "
            f"keeping newer {destination_label} credentials.[/dim]"
        )
        return False
    if comparison.winner == "ambiguous":
        console.print(f"[yellow]{comparison.reason}[/yellow]")
        return _confirm_overwrite(candidate, source_label, destination_label)
    return True


def _merge_newer_external_profiles(sync_dir: Path) -> set[str]:
    """Bring newer external credentials into store before publishing local changes."""
    excluded_names = set(list_blacklisted_profiles(sync_dir)) | store.list_local_only_profiles()
    imported_names = set()
    for candidate in build_import_candidates(sync_dir):
        if candidate.name in excluded_names:
            continue
        # Ambiguous pairs are left for the export step to prompt exactly once.
        if candidate.comparison.winner == "source":
            import_profile(candidate.name, candidate.source_path)
            imported_names.add(candidate.name)
            console.print(
                f"[green]✓[/green] Imported profile [bold]{escape(candidate.name)}[/bold] "
                "from external credentials before export"
            )
    return imported_names


def _confirm_overwrite(
    candidate: SyncCandidate,
    source_label: str,
    destination_label: str,
) -> bool:
    for label, path, modified in (
        (source_label, candidate.source_path, candidate.source_modified),
        (destination_label, candidate.dest_path, candidate.dest_modified),
    ):
        console.print(
            f"  {label}: {credential_summary(read_profile(path))}"
        )
        console.print(f"    File modified: {format_modified(modified)}")
    return click.confirm(
        (
            f"Cannot automatically merge profile '{candidate.name}'. "
            f"Replace {destination_label} with {source_label} anyway?"
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


def _maybe_offer_push_after_reconcile(
    result, allow_prompt: bool, profile_name: str | None = None
) -> None:
    if not allow_prompt or not result or not result.store_updated_from_auth:
        return
    if (profile_name or get_active()) in store.list_local_only_profiles():
        return

    _maybe_offer_push_for_local_updates(
        header_lines=[
            "##### An app updated local auth.json       #####",
            "##### Updating local store now...          #####",
            "##### Successfully reconciled local store. #####",
        ],
        prompt="Reconciliation updated local store. Sync these changes now? [y/N]: ",
    )


def _maybe_offer_push_after_list_updates(
    reconcile_result,
    refreshed_profiles: list[str],
    allow_prompt: bool,
) -> None:
    if not allow_prompt:
        return

    if (
        reconcile_result
        and reconcile_result.store_updated_from_auth
        and get_active() not in store.list_local_only_profiles()
    ):
        _maybe_offer_push_for_local_updates(
            header_lines=[
                "##### Local store updated during list      #####",
                _format_push_banner_line("Updated active auth and stale tokens"),
                "##### Sync these local changes.   #####",
            ],
            prompt="List updated local store. Sync these changes now? [y/N]: ",
        )
        return

    _maybe_offer_push_after_refresh(refreshed_profiles, allow_prompt=True)


def _maybe_offer_push_after_refresh(refreshed_profiles: list[str], allow_prompt: bool) -> None:
    refreshed_profiles = [
        name for name in refreshed_profiles if name not in store.list_local_only_profiles()
    ]
    if not allow_prompt or not refreshed_profiles:
        return

    names = ", ".join(refreshed_profiles)
    profile_label = "profile" if len(refreshed_profiles) == 1 else "profiles"
    summary = f"Updated {profile_label}: {names}"
    _maybe_offer_push_for_local_updates(
        header_lines=[
            "##### Refreshed stale stored tokens        #####",
            _format_push_banner_line(summary),
            "##### Local store now has newer tokens.   #####",
        ],
        prompt=f"Refreshed stored tokens for {profile_label} {names}. Sync these changes now? [y/N]: ",
    )


def _format_push_banner_line(message: str) -> str:
    return f"##### {message[:36].ljust(36)} #####"


def _maybe_offer_push_for_local_updates(header_lines: list[str], prompt: str) -> None:
    sync_dir = get_sync_dir()
    if sync_dir is None:
        return

    banner = "#" * 48
    console.print(f"[bold red]{banner}[/bold red]")
    console.print(f"[bold red]{header_lines[0]}[/bold red]")
    console.print(f"[bold red]{banner}[/bold red]")
    for line in header_lines[1:]:
        console.print(line)
    if _confirm_yes_no(prompt):
        try:
            _sync_now(sync_dir)
        except (SyncError, SyncBusyError, OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc


@_locked_sync_action
def _push_sync_changes(sync_dir: Path) -> None:
    _run_preflight_reconciliation(prompt_on_unsafe=True)
    try:
        local_only = store.list_local_only_profiles()
        if local_only:
            sync_local_profile_excludes(sync_dir, local_only)
        pull_message = pull_sync_repo(sync_dir)
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except GitCommandError as e:
        raise click.ClickException(e.message)
    except ValueError as e:
        raise click.ClickException(str(e))

    console.print(f"[green]✓[/green] Pulled sync repo [bold]{sync_dir}[/bold]")
    if pull_message:
        console.print(f"[dim]{pull_message}[/dim]")

    imported_names = _merge_newer_external_profiles(sync_dir)
    _report_reconcile_result(reconcile_imported_active_profile(imported_names))
    _run_export(sync_dir)
    try:
        message = push_sync_repo(sync_dir)
    except FileNotFoundError as e:
        raise click.ClickException(str(e))
    except GitCommandError as e:
        raise click.ClickException(e.message)

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
