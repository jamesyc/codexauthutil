"""Rich-based rendering and interactive menu."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from rich import box
from rich.console import Console, Group
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from codexauth.usage import UsageCredits, UsageResult, UsageWindow

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


def _reset_credit_expiry_text(
    expires_at: datetime | None,
    now: datetime | None = None,
) -> str:
    if expires_at is None:
        return "Does not expire"

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    seconds = int((expires_at - current).total_seconds())
    if seconds <= 0:
        return "expired"

    days, remainder = divmod(seconds, 24 * 3600)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    day_text = f"{days:>2}d " if days else "    "
    return f"{day_text}{hours:>2}h {minutes:>2}m"


def _reset_credit_expiry_style(
    expires_at: datetime | None,
    now: datetime,
) -> str | None:
    if expires_at is None:
        return None
    seconds = (expires_at - now).total_seconds()
    if seconds <= 24 * 3600:
        return "red"
    if seconds <= 7 * 24 * 3600:
        return "yellow"
    return None


def _fmt_usage_resets(usage: UsageResult) -> str:
    if usage.error == "expired":
        return "[red]expired[/red]"
    if usage.error or usage.reset_count is None:
        return "[dim]N/A[/dim]"

    count = max(0, usage.reset_count)
    if count == 0:
        return "[dim]—[/dim]"

    if usage.reset_credits is None:
        return "[dim]Unavailable[/dim]"

    displayed_credits = usage.reset_credits[:count]
    now = datetime.now(timezone.utc)
    lines = []
    for credit in displayed_credits:
        text = _reset_credit_expiry_text(credit.expires_at, now=now)
        style = _reset_credit_expiry_style(credit.expires_at, now)
        lines.append(f"[{style}]{text}[/{style}]" if style else text)
    if len(displayed_credits) < count:
        lines.append("[dim]Unavailable[/dim]")
    return "\n".join(lines)


def _credit_balance_text(credits: UsageCredits) -> str:
    if credits.unlimited:
        return "Unlimited"
    if not credits.has_credits:
        return "—"
    if credits.balance is None:
        return "Available"
    try:
        balance = Decimal(credits.balance.strip())
    except InvalidOperation:
        return "Available"
    if not balance.is_finite() or balance <= 0:
        return "Available"
    return str(int(balance.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def _fmt_credits(usage: UsageResult) -> str:
    if usage.error == "expired":
        return "[red]expired[/red]"
    if usage.error or usage.credits is None:
        return "[dim]N/A[/dim]"
    text = _credit_balance_text(usage.credits)
    return f"[dim]{text}[/dim]" if text == "—" else text


def _plan_label(usage: UsageResult, *, compact: bool = False) -> str:
    labels = {
        "plus": "Plus",
        "prolite": "Pro 5x",
        "pro": "Pro 20x",
    }
    compact_labels = {
        "plus": "1x",
        "prolite": "5x",
        "pro": "20x",
    }
    if usage.error or not isinstance(usage.plan_type, str):
        return "[dim]N/A[/dim]"
    fallback = escape(usage.plan_type.replace("_", " ").title())
    normalized = usage.plan_type.lower()
    return (compact_labels if compact else labels).get(normalized, fallback)


def _fmt_plus_equivalent_left(usage: UsageResult) -> str:
    remaining = usage.weekly_plus_equivalent_left
    if usage.error == "expired":
        return "[red]expired[/red]"
    if remaining is None:
        return "[dim]N/A[/dim]"
    color = "red" if remaining == 0 else ("yellow" if remaining < 1 else "green")
    return f"[{color}]{remaining:.2f}[/{color}]"


def _active_marker(name: str, active: str | None) -> str:
    return "[green]●[/green]" if name == active else ""


def _is_standard_window_depleted(window: UsageWindow) -> bool:
    return window.used_pct is not None and window.used_pct >= 100


def _is_profile_depleted(usage: UsageResult) -> bool:
    if usage.error == "expired":
        return True
    standard_windows = [_get_window(usage, key) for key in ("primary_window", "secondary_window")]
    return any(_is_standard_window_depleted(window) for window in standard_windows)


def _is_hidden_profile_urgent(usage: UsageResult) -> bool:
    if usage.error == "expired":
        return True
    weekly_window = _get_window(usage, "secondary_window")
    return weekly_window.used_pct is not None and weekly_window.used_pct >= 99


def _profile_name_text(name: str, usage: UsageResult, hidden: bool = False) -> Text:
    if hidden:
        style = "bold red" if _is_hidden_profile_urgent(usage) else "dim"
        text = Text(name, style=style)
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
    show_details: bool = True,
) -> Table:
    window_keys = _usage_window_keys(usage_map)
    if not show_details:
        window_keys = [key for key in window_keys if key != "primary_window"]
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
    if show_details:
        table.add_column("Mode", style="dim", width=7)
    table.add_column("Tier", min_width=7, max_width=8)

    for key in window_keys:
        spec = _spec_for_key(usage_map, key)
        table.add_column(spec["full_pct"], min_width=9, max_width=10)
        table.add_column(spec["full_left"], min_width=10, max_width=12)
    table.add_column("Plus-Eq Left", min_width=12, max_width=12, justify="right")
    table.add_column("Credits", min_width=7, max_width=10, justify="right")
    table.add_column("Reset Expires", min_width=10, max_width=15, justify="right")
    table.add_column("", width=2)

    for i, name in enumerate(profiles, 1):
        u = usage_map.get(name, UsageResult(error="n/a"))
        mode = profile_data.get(name, {}).get("auth_mode", "?")
        row = [
            str(i),
            _profile_name_text(name, u, hidden=name in (hidden_profiles or set())),
        ]
        if show_details:
            row.append(mode)
        row.append(_plan_label(u))
        for key in window_keys:
            window = _get_window(u, key)
            row.extend(
                [
                    _fmt_pct(window.used_pct, u.error),
                    _fmt_time_left(window.reset_at, u.error),
                ]
            )
        row.append(_fmt_plus_equivalent_left(u))
        row.append(_fmt_credits(u))
        row.append(_fmt_usage_resets(u))
        row.append(_active_marker(name, active))
        table.add_row(*row)
    return table


def _render_compact_table(
    profiles: list[str],
    profile_data: dict[str, dict],
    usage_map: dict[str, UsageResult],
    active: str | None,
    hidden_profiles: set[str] | None = None,
    show_details: bool = True,
) -> Table:
    window_keys = _usage_window_keys(usage_map)
    if not show_details:
        window_keys = [key for key in window_keys if key != "primary_window"]
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
    if show_details:
        table.add_column("Md", style="dim", max_width=5)
    table.add_column("Tier", min_width=3, max_width=3)
    for key in window_keys:
        spec = _spec_for_key(usage_map, key)
        table.add_column(spec["compact_pct"], min_width=10)
        table.add_column(spec["compact_left"], min_width=5)
    table.add_column("Eq L", min_width=5, max_width=6, justify="right")
    table.add_column("Credits", min_width=7, max_width=10, justify="right")
    table.add_column("Reset Exp.", min_width=10, max_width=15, justify="right")
    table.add_column("", width=1)

    for i, name in enumerate(profiles, 1):
        u = usage_map.get(name, UsageResult(error="n/a"))
        mode = profile_data.get(name, {}).get("auth_mode", "?")
        row = [
            str(i),
            _profile_name_text(name, u, hidden=name in (hidden_profiles or set())),
        ]
        if show_details:
            row.append(mode)
        row.append(_plan_label(u, compact=True))
        for key in window_keys:
            window = _get_window(u, key)
            row.extend(
                [
                    _fmt_pct(window.used_pct, u.error),
                    _fmt_time_left(window.reset_at, u.error),
                ]
            )
        row.append(_fmt_plus_equivalent_left(u))
        row.append(_fmt_credits(u))
        row.append(_fmt_usage_resets(u))
        row.append(_active_marker(name, active))
        table.add_row(*row)
    return table


def _render_narrow_profiles(
    profiles: list[str],
    profile_data: dict[str, dict],
    usage_map: dict[str, UsageResult],
    active: str | None,
    hidden_profiles: set[str] | None = None,
    show_details: bool = True,
) -> Group:
    renders: list[Text] = []
    window_keys = _usage_window_keys(usage_map)
    if not show_details:
        window_keys = [key for key in window_keys if key != "primary_window"]

    for i, name in enumerate(profiles, 1):
        u = usage_map.get(name, UsageResult(error="n/a"))
        mode = profile_data.get(name, {}).get("auth_mode", "?")

        title = Text()
        title.append(f"{i}. ", style="bold")
        if name in (hidden_profiles or set()):
            title.append(name, style="bold red" if _is_hidden_profile_urgent(u) else "dim")
            title.append(" (hidden)", style="dim")
        else:
            title.append(name, style="bold red" if _is_profile_depleted(u) else "bold")
        if name == active:
            title.append(" ●", style="green")
        if show_details:
            title.append(f"  {mode}", style="dim")

        renders.append(title)
        tier = Text.from_markup(
            f"[bold]{'tier':<6}[/bold] {_plan_label(u)}"
        )
        renders.append(tier)
        for key in window_keys:
            window = _get_window(u, key)
            label = _narrow_label(usage_map, key)
            usage = Text.from_markup(
                f"[bold]{label}[/bold] {_fmt_pct_narrow(window.used_pct, u.error)}"
                f"/{_fmt_time_left_narrow(window.reset_at, u.error)}"
            )
            renders.append(usage)
        equivalent = Text.from_markup(
            f"[bold]{'eq left':<6}[/bold] {_fmt_plus_equivalent_left(u)}"
        )
        renders.append(equivalent)
        credits = Text.from_markup(
            f"[bold]{'credits':<6}[/bold] {_fmt_credits(u)}"
        )
        renders.append(credits)
        resets = Text.from_markup(
            f"[bold]{'reset':<6}[/bold] {_fmt_usage_resets(u)}"
        )
        renders.append(resets)
        renders.append(Text(""))

    return Group(*renders)


def render_table(
    profiles: list[str],
    profile_data: dict[str, dict],
    usage_map: dict[str, UsageResult],
    active: str | None,
    width: int | None = None,
    hidden_profiles: set[str] | None = None,
    show_details: bool = True,
):
    window_count = len(_usage_window_keys(usage_map))
    if not show_details and "primary_window" in _usage_window_keys(usage_map):
        window_count -= 1
    narrow_threshold = 80 + max(window_count - 2, 0) * 18
    compact_threshold = 150 + max(window_count - 2, 0) * 10

    if width is not None and width < narrow_threshold:
        return _render_narrow_profiles(
            profiles,
            profile_data,
            usage_map,
            active,
            hidden_profiles=hidden_profiles,
            show_details=show_details,
        )
    if width is not None and width < compact_threshold:
        return _render_compact_table(
            profiles,
            profile_data,
            usage_map,
            active,
            hidden_profiles=hidden_profiles,
            show_details=show_details,
        )
    return _render_full_table(
        profiles,
        profile_data,
        usage_map,
        active,
        hidden_profiles=hidden_profiles,
        show_details=show_details,
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
