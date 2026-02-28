"""Rich-based table rendering and interactive menu."""

from rich import box
from rich.console import Console
from rich.table import Table

from codexauth.usage import UsageResult

console = Console()


def _bar(pct: float, width: int = 5) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _fmt_pct(pct: float | None, error: str | None) -> str:
    if error == "expired":
        return "[red]expired[/red]"
    if error or pct is None:
        return "[dim]N/A[/dim]"
    color = "green" if pct < 60 else ("yellow" if pct < 85 else "red")
    return f"[{color}]{_bar(pct)} {pct:.0f}%[/{color}]"


def render_table(
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
    table.add_column("Weekly", min_width=13)
    table.add_column("", width=2)

    for i, name in enumerate(profiles, 1):
        u = usage_map.get(name, UsageResult(error="n/a"))
        mode = profile_data.get(name, {}).get("auth_mode", "?")
        active_marker = "[green]●[/green]" if name == active else ""
        table.add_row(
            str(i),
            name,
            mode,
            _fmt_pct(u.primary_pct, u.error),
            _fmt_pct(u.secondary_pct, u.error),
            active_marker,
        )
    return table


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
