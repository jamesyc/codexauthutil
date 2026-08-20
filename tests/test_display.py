"""Tests for codexauth.display."""

from datetime import datetime, timezone

from rich.console import Console

import codexauth.display as display_module
from codexauth.display import render_table
from codexauth.usage import UsageCredits, UsageResetCredit, UsageResult, UsageWindow


def test_render_table_shows_usage_and_time_left_columns(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 13, 15, 4, 5, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)

    table = render_table(
        profiles=["work"],
        profile_data={"work": {"auth_mode": "chatgpt"}},
        usage_map={
            "work": UsageResult(
                primary_pct=74,
                secondary_pct=38,
                plan_type="pro",
                primary_reset_at=datetime(2026, 3, 13, 19, 16, 5, tzinfo=timezone.utc),
                secondary_reset_at=datetime(2026, 3, 15, 18, 4, 5, tzinfo=timezone.utc),
            )
        },
        active="work",
    )

    console = Console(record=True, width=160)
    console.print(table)
    output = console.export_text()

    assert "5h Used" in output
    assert "5h Left" in output
    assert "Weekly" in output
    assert "Weekly Left" in output
    assert "Tier" in output
    assert "Pro 20x" in output
    assert "Plus-Eq Left" in output
    assert "12.40" in output
    assert "74%" in output
    assert "38%" in output
    assert "4h 12m" in output
    assert "2d 3h" in output


def test_render_table_shows_all_comparable_plan_tiers():
    names = ("alice", "bob", "charlie")
    table = render_table(
        profiles=list(names),
        profile_data={name: {"auth_mode": "chatgpt"} for name in names},
        usage_map={
            "alice": UsageResult(secondary_pct=39, plan_type="pro"),
            "bob": UsageResult(secondary_pct=93, plan_type="prolite"),
            "charlie": UsageResult(secondary_pct=100, plan_type="plus"),
        },
        active=None,
        width=220,
    )

    console = Console(record=True, width=220)
    console.print(table)
    output = console.export_text()

    assert "Pro 20x" in output
    assert "12.20" in output
    assert "Pro 5x" in output
    assert "0.35" in output
    assert "Plus" in output
    assert "0.00" in output


def test_render_table_shows_available_usage_resets_and_expirations(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 7, 4, 36, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)

    table = render_table(
        profiles=["work", "personal"],
        profile_data={
            "work": {"auth_mode": "chatgpt"},
            "personal": {"auth_mode": "chatgpt"},
        },
        usage_map={
            "work": UsageResult(
                reset_count=2,
                reset_credits=[
                    UsageResetCredit(
                        id="credit-1",
                        expires_at=datetime(2026, 7, 17, 9, 39, tzinfo=timezone.utc),
                    ),
                    UsageResetCredit(id="credit-2", expires_at=None),
                ],
            ),
            "personal": UsageResult(reset_count=0, reset_credits=[]),
        },
        active=None,
        width=200,
    )

    console = Console(record=True, width=200)
    console.print(table)
    output = console.export_text()

    assert "Reset Expires" in output
    assert "2 available" not in output
    assert "10d  5h  3m" in output
    assert "Jul 17" not in output
    assert "2026-07-17" not in output
    assert "Does not expire" in output
    assert table.columns[-2].justify == "right"


def test_render_table_shows_rounded_credit_balance():
    table = render_table(
        profiles=["alice", "unlimited", "none"],
        profile_data={
            "alice": {"auth_mode": "chatgpt"},
            "unlimited": {"auth_mode": "chatgpt"},
            "none": {"auth_mode": "chatgpt"},
        },
        usage_map={
            "alice": UsageResult(
                credits=UsageCredits(
                    has_credits=True,
                    unlimited=False,
                    balance="2311.1173137500",
                )
            ),
            "unlimited": UsageResult(
                credits=UsageCredits(has_credits=True, unlimited=True)
            ),
            "none": UsageResult(
                credits=UsageCredits(has_credits=False, unlimited=False)
            ),
        },
        active=None,
        width=200,
    )

    console = Console(record=True, width=200)
    console.print(table)
    output = console.export_text()

    assert "Credits" in output
    assert "2311" in output
    assert "2311.1173137500" not in output
    assert "Unlimited" in output
    assert "—" in output


def test_credit_balance_display_matches_codex_states():
    assert display_module._credit_balance_text(
        UsageCredits(has_credits=True, unlimited=False, balance="17.5")
    ) == "18"
    assert display_module._credit_balance_text(
        UsageCredits(has_credits=True, unlimited=False, balance=None)
    ) == "Available"
    assert display_module._credit_balance_text(
        UsageCredits(has_credits=True, unlimited=False, balance="invalid")
    ) == "Available"
    assert display_module._credit_balance_text(
        UsageCredits(has_credits=False, unlimited=False, balance="0")
    ) == "—"


def test_reset_expiration_duration_keeps_units_aligned():
    now = datetime(2026, 7, 7, tzinfo=timezone.utc)

    assert display_module._reset_credit_expiry_text(
        now.replace(day=12, hour=9, minute=3), now=now
    ) == " 5d  9h  3m"
    assert display_module._reset_credit_expiry_text(
        now.replace(hour=9, minute=3), now=now
    ) == "     9h  3m"


def test_usage_reset_expiration_color_reflects_urgency(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 7, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)
    usage = UsageResult(
        reset_count=4,
        reset_credits=[
            UsageResetCredit(
                id="default",
                expires_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
            ),
            UsageResetCredit(
                id="yellow",
                expires_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
            ),
            UsageResetCredit(
                id="red",
                expires_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
            ),
            UsageResetCredit(id="never", expires_at=None),
        ],
    )

    output = display_module._fmt_usage_resets(usage)

    assert output.splitlines() == [
        " 8d  0h  0m",
        "[yellow] 7d  0h  0m[/yellow]",
        "[red] 1d  0h  0m[/red]",
        "Does not expire",
    ]


def test_render_compact_table_right_aligns_reset_expiration():
    table = render_table(
        profiles=["work"],
        profile_data={"work": {"auth_mode": "chatgpt"}},
        usage_map={"work": UsageResult(reset_count=0, reset_credits=[])},
        active=None,
        width=100,
    )

    assert table.columns[-2].header == "Reset Exp."
    assert table.columns[-2].justify == "right"


def test_render_table_shows_reset_expiry_fallback_on_narrow_width():
    table = render_table(
        profiles=["work"],
        profile_data={"work": {"auth_mode": "chatgpt"}},
        usage_map={"work": UsageResult(reset_count=3, reset_credits=None)},
        active=None,
        width=50,
    )

    console = Console(record=True, width=50)
    console.print(table)
    output = console.export_text()

    assert "reset  Unavailable" in output
    assert "credits N/A" in output


def test_render_table_uses_stacked_layout_on_narrow_width(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 13, 15, 4, 5, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)

    table = render_table(
        profiles=["work"],
        profile_data={"work": {"auth_mode": "chatgpt"}},
        usage_map={
            "work": UsageResult(
                primary_pct=74,
                secondary_pct=38,
                primary_reset_at=datetime(2026, 3, 13, 19, 16, 5, tzinfo=timezone.utc),
                secondary_reset_at=datetime(2026, 3, 15, 18, 4, 5, tzinfo=timezone.utc),
            )
        },
        active="work",
        width=50,
    )

    console = Console(record=True, width=50)
    console.print(table)
    output = console.export_text()

    assert "1. work" in output
    assert "chatgpt" in output
    assert "5h     ████░  74%/4h 12m" in output
    assert "wk" in output
    assert "wk     ██░░░  38%/2d 3h" in output
    assert "4h 12m" in output
    assert "2d 3h" in output


def test_render_table_uses_compact_table_on_medium_width(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 13, 15, 4, 5, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)

    table = render_table(
        profiles=["work"],
        profile_data={"work": {"auth_mode": "chatgpt"}},
        usage_map={
            "work": UsageResult(
                primary_pct=74,
                secondary_pct=38,
                primary_reset_at=datetime(2026, 3, 13, 19, 16, 5, tzinfo=timezone.utc),
                secondary_reset_at=datetime(2026, 3, 15, 18, 4, 5, tzinfo=timezone.utc),
            )
        },
        active="work",
        width=90,
    )

    console = Console(record=True, width=90)
    console.print(table)
    output = console.export_text()

    assert "Name" in output
    assert "Md" in output
    assert "5h L" in output
    assert "Wk L" in output
    assert "work" in output
    assert "4h 12m" in output
    assert "2d 3h" in output


def test_render_table_shows_spark_window_when_present(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 13, 15, 4, 5, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)

    table = render_table(
        profiles=["work"],
        profile_data={"work": {"auth_mode": "chatgpt"}},
        usage_map={
            "work": UsageResult(
                windows={
                    "primary_window": UsageWindow(
                        key="primary_window",
                        used_pct=74,
                        reset_at=datetime(2026, 3, 13, 19, 16, 5, tzinfo=timezone.utc),
                    ),
                    "secondary_window": UsageWindow(
                        key="secondary_window",
                        used_pct=38,
                        reset_at=datetime(2026, 3, 15, 18, 4, 5, tzinfo=timezone.utc),
                    ),
                    "additional_gpt_5_3_codex_spark_primary_window": UsageWindow(
                        key="additional_gpt_5_3_codex_spark_primary_window",
                        used_pct=12,
                        reset_at=datetime(2026, 3, 13, 16, 34, 5, tzinfo=timezone.utc),
                        label="GPT-5.3-Codex-Spark",
                        short_label="GPT",
                    ),
                    "additional_gpt_5_3_codex_spark_secondary_window": UsageWindow(
                        key="additional_gpt_5_3_codex_spark_secondary_window",
                        used_pct=9,
                        reset_at=datetime(2026, 3, 15, 16, 34, 5, tzinfo=timezone.utc),
                        label="GPT-5.3-Codex-Spark Weekly",
                        short_label="GPW",
                    ),
                }
            )
        },
        active="work",
        width=260,
    )

    console = Console(record=True, width=260)
    console.print(table)
    output = console.export_text()

    assert "Spark" in output
    assert "Spark Left" in output
    assert "Spark Weekly" in output
    assert "12%" in output
    assert "1h 30m" in output


def test_render_table_shows_weekly_only_usage_in_weekly_columns(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 24, 23, 43, 6, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)

    table = render_table(
        profiles=["redstarlynx"],
        profile_data={"redstarlynx": {"auth_mode": "chatgpt"}},
        usage_map={
            "redstarlynx": UsageResult(
                windows={
                    "secondary_window": UsageWindow(
                        key="secondary_window",
                        used_pct=100,
                        reset_at=datetime(2026, 3, 28, 5, 36, 33, tzinfo=timezone.utc),
                        limit_window_seconds=604800,
                    )
                }
            )
        },
        active=None,
        width=160,
    )

    console = Console(record=True, width=160)
    console.print(table)
    output = console.export_text()

    assert "5h Used" in output
    assert "Weekly" in output
    assert "N/A" in output
    assert "100%" in output
    assert "3d 5h" in output


def test_render_table_marks_name_red_when_primary_usage_depleted(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 13, 15, 4, 5, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)

    table = render_table(
        profiles=["user@example.com"],
        profile_data={"user@example.com": {"auth_mode": "chatgpt"}},
        usage_map={
            "user@example.com": UsageResult(
                primary_pct=100,
                secondary_pct=38,
                primary_reset_at=datetime(2026, 3, 13, 19, 16, 5, tzinfo=timezone.utc),
                secondary_reset_at=datetime(2026, 3, 15, 18, 4, 5, tzinfo=timezone.utc),
            )
        },
        active=None,
        width=160,
    )

    console = Console(record=True, width=160)
    console.print(table)
    output = console.export_text(styles=True)

    assert "\x1b[1;31muser@example.com\x1b[0m" in output


def test_render_table_marks_name_red_when_secondary_usage_depleted_compact(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 13, 15, 4, 5, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)

    table = render_table(
        profiles=["user@example.com"],
        profile_data={"user@example.com": {"auth_mode": "chatgpt"}},
        usage_map={
            "user@example.com": UsageResult(
                primary_pct=74,
                secondary_pct=100,
                primary_reset_at=datetime(2026, 3, 13, 19, 16, 5, tzinfo=timezone.utc),
                secondary_reset_at=datetime(2026, 3, 15, 18, 4, 5, tzinfo=timezone.utc),
            )
        },
        active=None,
        width=90,
    )

    console = Console(record=True, width=90)
    console.print(table)
    output = console.export_text(styles=True)

    assert "\x1b[1;31muser@example.com\x1b[0m" in output


def test_render_table_marks_hidden_name_red_when_weekly_usage_is_99(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 13, 15, 4, 5, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)

    table = render_table(
        profiles=["user@example.com"],
        profile_data={"user@example.com": {"auth_mode": "chatgpt"}},
        usage_map={
            "user@example.com": UsageResult(
                primary_pct=12,
                secondary_pct=99,
                primary_reset_at=datetime(2026, 3, 13, 19, 16, 5, tzinfo=timezone.utc),
                secondary_reset_at=datetime(2026, 3, 15, 18, 4, 5, tzinfo=timezone.utc),
            )
        },
        active=None,
        width=160,
        hidden_profiles={"user@example.com"},
    )

    console = Console(record=True, width=160)
    console.print(table)
    output = console.export_text(styles=True)

    assert "\x1b[1;31muser@example.com\x1b[0m" in output
    assert "(hidden)" in output


def test_render_table_keeps_hidden_name_dim_when_weekly_usage_is_below_99(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 13, 15, 4, 5, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)

    table = render_table(
        profiles=["user@example.com"],
        profile_data={"user@example.com": {"auth_mode": "chatgpt"}},
        usage_map={
            "user@example.com": UsageResult(
                primary_pct=100,
                secondary_pct=98,
                primary_reset_at=datetime(2026, 3, 13, 19, 16, 5, tzinfo=timezone.utc),
                secondary_reset_at=datetime(2026, 3, 15, 18, 4, 5, tzinfo=timezone.utc),
            )
        },
        active=None,
        width=90,
        hidden_profiles={"user@example.com"},
    )

    console = Console(record=True, width=90)
    console.print(table)
    output = console.export_text(styles=True)

    assert "\x1b[1;31muser@example.com\x1b[0m" not in output
    assert "\x1b[1;2muser@example.com" in output


def test_render_table_marks_name_red_when_weekly_only_usage_depleted_narrow(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 24, 23, 43, 6, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)

    table = render_table(
        profiles=["user@example.com"],
        profile_data={"user@example.com": {"auth_mode": "chatgpt"}},
        usage_map={
            "user@example.com": UsageResult(
                windows={
                    "secondary_window": UsageWindow(
                        key="secondary_window",
                        used_pct=100,
                        reset_at=datetime(2026, 3, 28, 5, 36, 33, tzinfo=timezone.utc),
                        limit_window_seconds=604800,
                    )
                }
            )
        },
        active=None,
        width=50,
    )

    console = Console(record=True, width=50)
    console.print(table)
    output = console.export_text(styles=True)

    assert "\x1b[1;31muser@example.com\x1b[0m" in output


def test_render_table_does_not_mark_name_red_when_both_standard_windows_are_na_full(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 13, 15, 4, 5, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)

    table = render_table(
        profiles=["user@example.com"],
        profile_data={"user@example.com": {"auth_mode": "chatgpt"}},
        usage_map={"user@example.com": UsageResult(error="n/a")},
        active=None,
        width=160,
    )

    console = Console(record=True, width=160)
    console.print(table)
    output = console.export_text(styles=True)

    assert "\x1b[1;31muser@example.com\x1b[0m" not in output


def test_render_table_marks_name_red_when_usage_is_expired():
    table = render_table(
        profiles=["cindy"],
        profile_data={"cindy": {"auth_mode": "chatgpt"}},
        usage_map={"cindy": UsageResult(error="expired")},
        active=None,
        width=160,
    )

    console = Console(record=True, width=160)
    console.print(table)
    output = console.export_text(styles=True)

    assert "\x1b[1;31mcindy" in output


def test_render_table_marks_hidden_name_red_when_usage_is_expired():
    table = render_table(
        profiles=["cindy"],
        profile_data={"cindy": {"auth_mode": "chatgpt"}},
        usage_map={"cindy": UsageResult(error="expired")},
        active=None,
        width=160,
        hidden_profiles={"cindy"},
    )

    console = Console(record=True, width=160)
    console.print(table)
    output = console.export_text(styles=True)

    assert "\x1b[1;31mcindy" in output


def test_render_table_does_not_mark_name_red_when_both_windows_are_missing_narrow(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 13, 15, 4, 5, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)

    table = render_table(
        profiles=["user@example.com"],
        profile_data={"user@example.com": {"auth_mode": "chatgpt"}},
        usage_map={"user@example.com": UsageResult()},
        active=None,
        width=50,
    )

    console = Console(record=True, width=50)
    console.print(table)
    output = console.export_text(styles=True)

    assert "\x1b[1;31muser@example.com\x1b[0m" not in output


def test_render_table_does_not_mark_name_red_when_only_primary_window_is_na(monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 24, 23, 43, 6, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)

    table = render_table(
        profiles=["user@example.com"],
        profile_data={"user@example.com": {"auth_mode": "chatgpt"}},
        usage_map={
            "user@example.com": UsageResult(
                windows={
                    "secondary_window": UsageWindow(
                        key="secondary_window",
                        used_pct=38,
                        reset_at=datetime(2026, 3, 28, 5, 36, 33, tzinfo=timezone.utc),
                        limit_window_seconds=604800,
                    )
                }
            )
        },
        active=None,
        width=160,
    )

    console = Console(record=True, width=160)
    console.print(table)
    output = console.export_text(styles=True)

    assert "\x1b[1;31muser@example.com\x1b[0m" not in output
