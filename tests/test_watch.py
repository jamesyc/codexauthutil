"""Watch command behavior with isolated profiles and a simulated polling clock."""

import asyncio
import importlib
from io import StringIO

import pytest
from click.testing import CliRunner
from rich.console import Console

import codexauth.store as store
from codexauth.autosync import SyncError, SyncReport
from codexauth.cli import cli
from codexauth.locking import SyncBusyError
from codexauth.usage import UsageFetchSummary, UsageResult

cli_module = importlib.import_module("codexauth.cli")


@pytest.fixture(autouse=True)
def no_sync_config(monkeypatch):
    """Watch tests opt into a fake sync directory instead of the user's .env."""
    monkeypatch.setattr(cli_module, "get_sync_dir", lambda: None)


def test_watch_reloads_all_accounts_without_prompting(saved_profile, monkeypatch, tmp_path):
    store.hide_profile("work")
    store.save_profile("retired", saved_profile)
    store.set_active("work")
    now = 0.0
    checks = []
    syncs = []
    waits = []

    def sync_profiles(directory, **kwargs):
        nonlocal now
        assert directory == tmp_path
        syncs.append(now)
        now += 1
        if len(syncs) == 2:
            store.delete_profile("retired")
            store.save_profile("personal", saved_profile)
            updated = store.load_profile("work")
            updated["tokens"]["access_token"] = "updated-access"
            store.save_profile("work", updated)
            store.set_active("personal")
        return SyncReport()

    async def fetch_usage(profiles):
        nonlocal now
        checks.append((now, sorted(profiles), profiles["work"]["tokens"]["access_token"]))
        now += 2
        return UsageFetchSummary(
            usage_map={
                name: UsageResult(error="n/a") if len(checks) == 1
                else UsageResult(primary_pct=12, secondary_pct=37)
                for name in profiles
            },
            refreshed_profiles=[],
        )

    def sleep(seconds):
        nonlocal now
        waits.append(seconds)
        if len(waits) == 2:
            raise KeyboardInterrupt
        now += seconds

    def unexpected_prompt(*args, **kwargs):
        pytest.fail("watch must not prompt")

    monkeypatch.setattr(cli_module, "sync_once", sync_profiles)
    monkeypatch.setattr(cli_module, "fetch_all_usage", fetch_usage)
    monkeypatch.setattr(cli_module, "monotonic", lambda: now)
    monkeypatch.setattr(cli_module, "sleep", sleep)
    monkeypatch.setattr(cli_module, "get_sync_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_module, "interactive_prompt", unexpected_prompt)
    monkeypatch.setattr(cli_module, "_confirm_yes_no", unexpected_prompt)
    monkeypatch.setattr(cli_module.click, "confirm", unexpected_prompt)

    result = CliRunner().invoke(cli, ["watch"], terminal_width=160)

    assert result.exit_code == 0, result.output
    assert syncs == [0, 10]
    assert checks == [
        (1, ["retired", "work"], "fake-access-token"),
        (11, ["personal", "work"], "updated-access"),
    ]
    assert waits == [7, 7]
    first_snapshot, second_snapshot = result.output.split("Watching all profiles")[1:]
    assert "retired" in first_snapshot
    assert "N/A" in first_snapshot
    assert "personal" in second_snapshot
    assert "retired" not in second_snapshot
    assert "hidden" in second_snapshot
    assert "12%" in second_snapshot
    assert "37%" in second_snapshot
    assert store.get_active() == "personal"
    assert "Stopped watching profiles." in result.output
    assert "\x1b[2J" not in result.output


def test_watch_syncs_accounts_into_an_empty_store(saved_profile, monkeypatch, tmp_path):
    store.delete_profile("work")
    checked = []
    syncs = []
    waits = []

    def sync_profiles(directory, **kwargs):
        syncs.append(directory)
        if len(syncs) == 2:
            store.save_profile("work", saved_profile)
        return SyncReport()

    async def fetch_usage(profiles):
        checked.append(sorted(profiles))
        return UsageFetchSummary(
            usage_map={name: UsageResult(secondary_pct=25) for name in profiles},
            refreshed_profiles=[],
        )

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "get_sync_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_module, "sync_once", sync_profiles)
    monkeypatch.setattr(cli_module, "fetch_all_usage", fetch_usage)
    monkeypatch.setattr(cli_module, "monotonic", lambda: 0)
    monkeypatch.setattr(cli_module, "sleep", sleep)

    result = CliRunner().invoke(cli, ["watch"], terminal_width=160)

    assert result.exit_code == 0, result.output
    assert syncs == [tmp_path, tmp_path]
    assert waits == [10, 10]
    assert checked == [["work"]]
    assert "No profiles stored" in result.output
    assert "work" in result.output
    assert "25%" in result.output


def test_watch_waits_for_slow_checks_to_finish(saved_profile, monkeypatch):
    now = 0.0
    starts = []
    waits = []

    async def fetch_usage(profiles):
        nonlocal now
        starts.append(now)
        now += 13
        return UsageFetchSummary(
            usage_map={name: UsageResult(error="n/a") for name in profiles},
            refreshed_profiles=[],
        )

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "fetch_all_usage", fetch_usage)
    monkeypatch.setattr(cli_module, "monotonic", lambda: now)
    monkeypatch.setattr(cli_module, "sleep", sleep)

    result = CliRunner().invoke(cli, ["watch"])

    assert result.exit_code == 0, result.output
    assert starts == [0, 13]
    assert waits == [0, 0]


@pytest.mark.parametrize("terminal", [False, True])
def test_watch_stops_cleanly_during_a_lookup(saved_profile, monkeypatch, terminal):
    async def interrupted_fetch(profiles):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "fetch_all_usage", interrupted_fetch)
    monkeypatch.setattr(cli_module, "console", Console(force_terminal=terminal, width=160))

    result = CliRunner().invoke(cli, ["watch"])

    assert result.exit_code == 0, result.output
    assert "\x1b[2J" not in result.output
    assert "Stopped watching profiles." in result.output
    assert "Aborted" not in result.output


@pytest.mark.parametrize("interrupt_during", [None, "sync", "usage"])
def test_watch_keeps_last_snapshot_visible_while_syncing_and_fetching(
    saved_profile, monkeypatch, tmp_path, interrupt_during
):
    class RecordingOutput(StringIO):
        def __init__(self):
            super().__init__()
            self.writes = []

        def write(self, text):
            if text:
                self.writes.append(text)
            return super().write(text)

    output = RecordingOutput()
    checks = 0
    syncs = 0

    def sync_profiles(directory, *, notify):
        nonlocal syncs
        syncs += 1
        previous = output.getvalue()
        if syncs == 1:
            assert previous == ""
        else:
            assert "13%" in previous
            assert "42%" not in previous
            assert len(output.writes) == 1
        notify("Retrying sync")
        assert output.getvalue() == previous
        if syncs == 2 and interrupt_during == "sync":
            raise KeyboardInterrupt
        return SyncReport()

    async def fetch_usage(profiles):
        nonlocal checks
        checks += 1
        previous = output.getvalue()
        if checks == 1:
            assert previous == ""
        else:
            assert "13%" in previous
            assert "42%" not in previous
            assert len(output.writes) == 1
        await asyncio.sleep(0)
        assert output.getvalue() == previous
        if checks == 2 and interrupt_during == "usage":
            raise KeyboardInterrupt
        return UsageFetchSummary(
            usage_map={
                name: UsageResult(secondary_pct=13 if checks == 1 else 42)
                for name in profiles
            },
            refreshed_profiles=[],
        )

    def sleep(seconds):
        if checks == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "fetch_all_usage", fetch_usage)
    monkeypatch.setattr(cli_module, "get_sync_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_module, "sync_once", sync_profiles)
    monkeypatch.setattr(cli_module, "monotonic", lambda: 0)
    monkeypatch.setattr(cli_module, "sleep", sleep)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(
        cli_module, "console", Console(file=output, force_terminal=True, width=160)
    )

    result = CliRunner().invoke(cli, ["watch"], terminal_width=160)

    assert result.exit_code == 0, result.exception
    snapshots = output.writes[:-1]
    assert len(snapshots) == (1 if interrupt_during else 2)
    for snapshot in snapshots:
        assert snapshot.startswith("\x1b[2J\x1b[H")
        assert "Watching all profiles" in snapshot
        assert "work" in snapshot
    assert ("42%" in output.getvalue()) is (interrupt_during is None)
    assert "Fetching usage" not in output.getvalue()
    assert "Stopped watching profiles." in output.writes[-1]


@pytest.mark.parametrize("error_type", [SyncError, SyncBusyError, OSError, ValueError])
def test_watch_shows_local_usage_on_sync_failure_and_recovers(
    saved_profile, tmp_path, monkeypatch, error_type
):
    calls, lookups, waits = [], [], []

    def sync_profiles(directory, **kwargs):
        calls.append(directory)
        if len(calls) == 2:
            raise error_type("Offline")
        return SyncReport()

    async def fetch_usage(profiles):
        lookups.append(len(calls))
        return UsageFetchSummary(
            usage_map={"work": UsageResult(secondary_pct=25)}, refreshed_profiles=[]
        )

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            raise KeyboardInterrupt

    def unexpected_prompt(*args, **kwargs):
        pytest.fail("watch must not prompt")

    monkeypatch.setattr(cli_module, "get_sync_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_module, "sync_once", sync_profiles)
    monkeypatch.setattr(cli_module, "fetch_all_usage", fetch_usage)
    monkeypatch.setattr(cli_module, "monotonic", lambda: 0)
    monkeypatch.setattr(cli_module, "sleep", sleep)
    monkeypatch.setattr(cli_module, "interactive_prompt", unexpected_prompt)
    monkeypatch.setattr(cli_module, "_confirm_yes_no", unexpected_prompt)

    result = CliRunner().invoke(cli, ["watch"], terminal_width=160)

    assert result.exit_code == 0, result.output
    assert waits == [10, 10, 10]
    assert calls == [tmp_path] * 3
    assert lookups == [1, 2, 3]
    snapshots = result.output.split("Watching all profiles")[1:]
    assert len(snapshots) == 3
    assert all("work" in snapshot and "25%" in snapshot for snapshot in snapshots)
    assert "Offline" in snapshots[1]
    assert "Showing local profiles" in snapshots[1]
    assert "Offline" not in snapshots[2]
    assert "Stopped watching profiles" in result.output


def test_watch_interval_controls_the_combined_check(saved_profile, tmp_path, monkeypatch):
    now = 0
    waits = []

    def sync_profiles(directory, **kwargs):
        nonlocal now
        now += 4
        return SyncReport()

    async def fetch_usage(profiles):
        nonlocal now
        now += 3
        return UsageFetchSummary(
            usage_map={"work": UsageResult(error="n/a")}, refreshed_profiles=[]
        )

    def sleep(seconds):
        waits.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "get_sync_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_module, "sync_once", sync_profiles)
    monkeypatch.setattr(cli_module, "fetch_all_usage", fetch_usage)
    monkeypatch.setattr(cli_module, "monotonic", lambda: now)
    monkeypatch.setattr(cli_module, "sleep", sleep)

    result = CliRunner().invoke(cli, ["watch", "--interval", "30"])

    assert result.exit_code == 0, result.output
    assert waits == [23]
    assert "every 30 seconds" in result.output


def test_watch_rejects_nonpositive_interval():
    result = CliRunner().invoke(cli, ["watch", "--interval", "0"])
    assert result.exit_code == 2
    assert "--interval" in result.output
