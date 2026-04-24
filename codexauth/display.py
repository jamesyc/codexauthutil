"""Rich-based rendering and interactive menu."""

from datetime import datetime, timezone

from rich import box
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from codexauth.usage import UsageResult, UsageWindow

console = Console()

WINDOW_SPECS = {
    "primary_window": {
        "full_pct": "5h Used",
        "full_left": "5h Left",
        "compact_pct": "5h",
        "compact_left": "5h L",
        "narrow": "5h",
    },
    "secondary_window": {
        "full_pct": "Weekly",
        "full_left": "Weekly Left",
        "compact_pct": "Wk",
        "compact_left": "Wk L",
        "narrow": "wk",
    },
}


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


def _is_standard_window_depleted(window: UsageWindow) -> bool:
    return window.used_pct is not None and window.used_pct >= 100


def _is_standard_window_unavailable(window: UsageWindow) -> bool:
    return window.used_pct is None and window.reset_at is None


def _is_profile_depleted(usage: UsageResult) -> bool:
    standard_windows = [_get_window(usage, key) for key in ("primary_window", "secondary_window")]
    return any(_is_standard_window_depleted(window) for window in standard_windows) or all(
        _is_standard_window_unavailable(window) for window in standard_windows
    ) or _is_standard_window_unavailable(_get_window(usage, "primary_window"))


def _profile_name_text(name: str, usage: UsageResult, hidden: bool = False) -> Text:
    if hidden:
        text = Text(name, style="dim")
        text.append(" (hidden)", style="dim")
        return text
    return Text(name, style="bold red" if _is_profile_depleted(usage) else "bold")


def _window_spec(key: str) -> dict[str, str]:
    if key in WINDOW_SPECS:
        return WINDOW_SPECS[key]

    label = key.removesuffix("_window").replace("_", " ").title() or "Usage"
    compact = label[:3]
    return {
        "full_pct": label,
        "full_left": f"{label} Left",
        "compact_pct": compact,
        "compact_left": f"{compact} L",
        "narrow": compact.lower(),
    }


def _usage_window_keys(usage_map: dict[str, UsageResult]) -> list[str]:
    ordered_keys = ["primary_window", "secondary_window"]
    seen = set(ordered_keys)
    extras: list[str] = []

    for usage in usage_map.values():
        for key in usage.windows:
            if key in seen or key in extras:
                continue
            extras.append(key)

    spark_keys = [key for key in extras if "spark" in key.lower()]
    other_keys = [key for key in extras if "spark" not in key.lower()]
    return ordered_keys + spark_keys + sorted(other_keys)


def _get_window(usage: UsageResult, key: str) -> UsageWindow:
    return usage.windows.get(key, UsageWindow(key=key))


def _resolved_window_spec(window: UsageWindow) -> dict[str, str]:
    if window.label:
        label = window.label
        compact = window.short_label or label[:3]
        if label == "GPT-5.3-Codex-Spark":
            label = "Spark"
            compact = "Spk 5h"
        elif label == "GPT-5.3-Codex-Spark Weekly":
            label = "Spark Weekly"
            compact = "Spk wk"
        return {
            "full_pct": label,
            "full_left": f"{label} Left",
            "compact_pct": compact,
            "compact_left": f"{compact} L",
            "narrow": compact,
        }
    return _window_spec(window.key)


def _spec_for_key(usage_map: dict[str, UsageResult], key: str) -> dict[str, str]:
    for usage in usage_map.values():
        window = usage.windows.get(key)
        if window is not None:
            return _resolved_window_spec(window)
    return _window_spec(key)


def _narrow_label(usage_map: dict[str, UsageResult], key: str, width: int = 6) -> str:
    return f"{_spec_for_key(usage_map, key)['narrow']:<{width}}"


def _render_full_table(
    profiles: list[str],
    profile_data: dict[str, dict],
    usage_map: dict[str, UsageResult],
    active: str | None,
    hidden_profiles: set[str] | None = None,
) -> Table:
    table = Table(
        box=box.SIMPLE,
        show_header=True,
        show_edge=False,
        header_style="bold",
        padding=(0, 1),
        pad_edge=False,
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("Name", style="bold", min_width=12, ratio=2)
    table.add_column("Mode", style="dim", width=7)

    for key in _usage_window_keys(usage_map):
        spec = _spec_for_key(usage_map, key)
        table.add_column(spec["full_pct"], min_width=9, max_width=10)
        table.add_column(spec["full_left"], min_width=10, max_width=12)
    table.add_column("", width=2)

    for i, name in enumerate(profiles, 1):
        u = usage_map.get(name, UsageResult(error="n/a"))
        mode = profile_data.get(name, {}).get("auth_mode", "?")
        row = [
            str(i),
            _profile_name_text(name, u, hidden=name in (hidden_profiles or set())),
            mode,
        ]
        for key in _usage_window_keys(usage_map):
            window = _get_window(u, key)
            row.extend(
                [
                    _fmt_pct(window.used_pct, u.error),
                    _fmt_time_left(window.reset_at, u.error),
                ]
            )
        row.append(_active_marker(name, active))
        table.add_row(*row)
    return table


def _render_compact_table(
    profiles: list[str],
    profile_data: dict[str, dict],
    usage_map: dict[str, UsageResult],
    active: str | None,
    hidden_profiles: set[str] | None = None,
) -> Table:
    table = Table(
        box=box.SIMPLE,
        show_header=True,
        show_edge=False,
        header_style="bold",
        padding=(0, 0),
        pad_edge=False,
    )
    table.add_column("#", style="dim", width=1)
    table.add_column("Name", style="bold", min_width=6)
    table.add_column("Md", style="dim", max_width=5)
    for key in _usage_window_keys(usage_map):
        spec = _spec_for_key(usage_map, key)
        table.add_column(spec["compact_pct"], min_width=10)
        table.add_column(spec["compact_left"], min_width=5)
    table.add_column("", width=1)

    for i, name in enumerate(profiles, 1):
        u = usage_map.get(name, UsageResult(error="n/a"))
        mode = profile_data.get(name, {}).get("auth_mode", "?")
        row = [
            str(i),
            _profile_name_text(name, u, hidden=name in (hidden_profiles or set())),
            mode,
        ]
        for key in _usage_window_keys(usage_map):
            window = _get_window(u, key)
            row.extend(
                [
                    _fmt_pct(window.used_pct, u.error),
                    _fmt_time_left(window.reset_at, u.error),
                ]
            )
        row.append(_active_marker(name, active))
        table.add_row(*row)
    return table


def _render_narrow_profiles(
    profiles: list[str],
    profile_data: dict[str, dict],
    usage_map: dict[str, UsageResult],
    active: str | None,
    hidden_profiles: set[str] | None = None,
) -> Group:
    renders: list[Text] = []
    window_keys = _usage_window_keys(usage_map)

    for i, name in enumerate(profiles, 1):
        u = usage_map.get(name, UsageResult(error="n/a"))
        mode = profile_data.get(name, {}).get("auth_mode", "?")

        title = Text()
        title.append(f"{i}. ", style="bold")
        if name in (hidden_profiles or set()):
            title.append(name, style="dim")
            title.append(" (hidden)", style="dim")
        else:
            title.append(name, style="bold red" if _is_profile_depleted(u) else "bold")
        if name == active:
            title.append(" ●", style="green")
        title.append(f"  {mode}", style="dim")

        renders.append(title)
        for key in window_keys:
            window = _get_window(u, key)
            label = _narrow_label(usage_map, key)
            usage = Text.from_markup(
                f"[bold]{label}[/bold] {_fmt_pct_narrow(window.used_pct, u.error)}"
                f"/{_fmt_time_left_narrow(window.reset_at, u.error)}"
            )
            renders.append(usage)
        renders.append(Text(""))

    return Group(*renders)


def render_table(
    profiles: list[str],
    profile_data: dict[str, dict],
    usage_map: dict[str, UsageResult],
    active: str | None,
    width: int | None = None,
    hidden_profiles: set[str] | None = None,
):
    window_count = len(_usage_window_keys(usage_map))
    narrow_threshold = 80 + max(window_count - 2, 0) * 18
    compact_threshold = 140 + max(window_count - 2, 0) * 10

    if width is not None and width < narrow_threshold:
        return _render_narrow_profiles(
            profiles, profile_data, usage_map, active, hidden_profiles=hidden_profiles
        )
    if width is not None and width < compact_threshold:
        return _render_compact_table(
            profiles, profile_data, usage_map, active, hidden_profiles=hidden_profiles
        )
    return _render_full_table(
        profiles, profile_data, usage_map, active, hidden_profiles=hidden_profiles
    )


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
