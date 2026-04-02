"""Tests for codexauth.display."""

from datetime import datetime, timezone

from rich.console import Console

import codexauth.display as display_module
from codexauth.display import render_table
from codexauth.usage import UsageResult, UsageWindow


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
    assert "74%" in output
    assert "38%" in output
    assert "4h 12m" in output
    assert "2d 3h" in output


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


def test_render_table_marks_name_red_when_both_standard_windows_are_na_full(monkeypatch):
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

    assert "\x1b[1;31muser@example.com\x1b[0m" in output


def test_render_table_marks_name_red_when_both_standard_windows_are_missing_narrow(monkeypatch):
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

    assert "\x1b[1;31muser@example.com\x1b[0m" in output


def test_render_table_marks_name_red_when_only_primary_window_is_na(monkeypatch):
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

    assert "\x1b[1;31muser@example.com\x1b[0m" in output
