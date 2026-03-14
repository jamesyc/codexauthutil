"""Tests for codexauth.display."""

from datetime import datetime, timezone

from rich.console import Console

import codexauth.display as display_module
from codexauth.display import render_table
from codexauth.usage import UsageResult


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
