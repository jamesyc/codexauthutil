"""Rich-based rendering and interactive menu."""

from datetime import datetime, timezone

from rich import box
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from codexauth.usage import UsageResult

console = Console()


def _bar(pct: float, width: int = 5) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _pct_color(pct: float) -> str:
    return "green" if pct < 60 else ("yellow" if pct < 85 else "red")


def _fmt_pct(pct: float | None, error: str | None) -> str:
    if error == "expired":
        return "[red]expired[/red]"
    if error or pct is None:
        return "[dim]N/A[/dim]"
    color = _pct_color(pct)
    return f"[{color}]{_bar(pct)} {pct:.0f}%[/{color}]"


def _time_left_text(reset_at, error: str | None, now: datetime | None = None) -> tuple[str, str]:
    if error == "expired":
        return "expired", "red"
    if error or reset_at is None:
        return "N/A", "dim"

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    seconds = int((reset_at - current).total_seconds())
    if seconds <= 0:
        return "expired", "red"

    days, rem = divmod(seconds, 24 * 3600)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    if days > 0:
        return f"{days}d {hours}h", ""
    if hours > 0:
        return f"{hours}h {minutes}m", ""
    return f"{minutes}m", ""


def _fmt_time_left(reset_at, error: str | None, now: datetime | None = None) -> str:
    text, style = _time_left_text(reset_at, error, now=now)
    if style:
        return f"[{style}]{text}[/{style}]"
    return text


def _fmt_pct_narrow(pct: float | None, error: str | None) -> str:
    if error == "expired":
        return "[red]   expired[/red]"
    if error or pct is None:
        return "[dim]       N/A[/dim]"
    color = _pct_color(pct)
    return f"[{color}]{_bar(pct)} {pct:>3.0f}%[/{color}]"


def _fmt_time_left_narrow(reset_at, error: str | None, now: datetime | None = None) -> str:
    text, style = _time_left_text(reset_at, error, now=now)
    padded = f"{text:<7}"
    if style:
        return f"[{style}]{padded}[/{style}]"
    return padded


def _active_marker(name: str, active: str | None) -> str:
    return "[green]●[/green]" if name == active else ""


def _render_full_table(
    profiles: list[str],
    profile_data: dict[str, dict],
    usage_map: dict[str, UsageResult],
    active: str | None,
) -> Table:
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=3)
    table.add_column("Name", style="bold")
    table.add_column("Mode", style="dim")
    table.add_column("5h Used", min_width=13)
    table.add_column("5h Left", min_width=10)
    table.add_column("Weekly", min_width=13)
    table.add_column("Weekly Left", min_width=12)
    table.add_column("", width=2)

    for i, name in enumerate(profiles, 1):
        u = usage_map.get(name, UsageResult(error="n/a"))
        mode = profile_data.get(name, {}).get("auth_mode", "?")
        table.add_row(
            str(i),
            name,
            mode,
            _fmt_pct(u.primary_pct, u.error),
            _fmt_time_left(u.primary_reset_at, u.error),
            _fmt_pct(u.secondary_pct, u.error),
            _fmt_time_left(u.secondary_reset_at, u.error),
            _active_marker(name, active),
        )
    return table


def _render_compact_table(
    profiles: list[str],
    profile_data: dict[str, dict],
    usage_map: dict[str, UsageResult],
    active: str | None,
) -> Table:
    table = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="bold",
        padding=(0, 0),
        pad_edge=False,
    )
    table.add_column("#", style="dim", width=1)
    table.add_column("Name", style="bold", min_width=6)
    table.add_column("Md", style="dim", max_width=5)
    table.add_column("5h", min_width=10)
    table.add_column("5h L", min_width=5)
    table.add_column("Wk", min_width=10)
    table.add_column("Wk L", min_width=5)
    table.add_column("", width=1)

    for i, name in enumerate(profiles, 1):
        u = usage_map.get(name, UsageResult(error="n/a"))
        mode = profile_data.get(name, {}).get("auth_mode", "?")
        table.add_row(
            str(i),
            name,
            mode,
            _fmt_pct(u.primary_pct, u.error),
            _fmt_time_left(u.primary_reset_at, u.error),
            _fmt_pct(u.secondary_pct, u.error),
            _fmt_time_left(u.secondary_reset_at, u.error),
            _active_marker(name, active),
        )
    return table


def _render_narrow_profiles(
    profiles: list[str],
    profile_data: dict[str, dict],
    usage_map: dict[str, UsageResult],
    active: str | None,
) -> Group:
    renders: list[Text] = []

    for i, name in enumerate(profiles, 1):
        u = usage_map.get(name, UsageResult(error="n/a"))
        mode = profile_data.get(name, {}).get("auth_mode", "?")

        title = Text()
        title.append(f"{i}. {name}", style="bold")
        if name == active:
            title.append(" ●", style="green")
        title.append(f"  {mode}", style="dim")

        usage = Text.from_markup(
            f"[bold]5h[/bold] {_fmt_pct_narrow(u.primary_pct, u.error)}"
            f"/{_fmt_time_left_narrow(u.primary_reset_at, u.error)}"
            f" [bold]wk[/bold] {_fmt_pct_narrow(u.secondary_pct, u.error)}"
            f"/{_fmt_time_left_narrow(u.secondary_reset_at, u.error)}"
        )

        renders.extend([title, usage, Text("")])

    return Group(*renders)


def render_table(
    profiles: list[str],
    profile_data: dict[str, dict],
    usage_map: dict[str, UsageResult],
    active: str | None,
    width: int | None = None,
):
    if width is not None and width < 80:
        return _render_narrow_profiles(profiles, profile_data, usage_map, active)
    if width is not None and width < 110:
        return _render_compact_table(profiles, profile_data, usage_map, active)
    return _render_full_table(profiles, profile_data, usage_map, active)


def interactive_prompt(profiles: list[str]) -> str | None:
    """Show a numbered prompt and return the chosen profile name, or None."""
    try:
        choice = input("\nActivate token (enter number, or q to quit): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if choice.lower() in ("q", ""):
        return None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(profiles):
            return profiles[idx]
        console.print("[red]Number out of range.[/red]")
    except ValueError:
        console.print("[red]Invalid input.[/red]")
    return None
