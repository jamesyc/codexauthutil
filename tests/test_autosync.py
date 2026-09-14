"""Exercise unattended sync with real local Git repositories and no live credentials."""

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from codexauth import store
from codexauth.autosync import SyncError, SyncRepo, SyncReport, sync_once
from codexauth.cli import cli
from codexauth.locking import SyncBusyError, sync_lock
from codexauth.usage import UsageFetchSummary, UsageResult

autosync = importlib.import_module("codexauth.autosync")
cli_module = importlib.import_module("codexauth.cli")


def git(directory, *args, check=True):
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={os.devnull}", *args],
        cwd=directory, capture_output=True, text=True, check=check,
    )


def profile(version):
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "account_id": "test-account",
            "access_token": f"access-{version}",
            "refresh_token": f"refresh-{version}",
            "id_token": f"id-{version}",
        },
        "last_refresh": f"2026-09-0{version}T12:00:00Z",
    }


def save_external(repo, name, data):
    (repo / f"{name}.json").write_text(json.dumps(data, indent=2))


def commit(repo):
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Test credentials")


@pytest.fixture
def repos(tmp_path, monkeypatch):
    remote, local, other = (tmp_path / name for name in ("remote.git", "local", "other"))
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    git(tmp_path, "clone", str(remote), str(local))
    git(local, "config", "user.name", "Sync Test")
    git(local, "config", "user.email", "sync@example.test")
    save_external(local, "work", profile(1))
    (local / "README.md").write_text("Shared credential repository\n")
    commit(local)
    git(local, "push", "-u", "origin", "main")
    git(tmp_path, "clone", str(remote), str(other))
    git(other, "config", "user.name", "Sync Test")
    git(other, "config", "user.email", "sync@example.test")
    monkeypatch.setattr(cli_module, "get_sync_dir", lambda: local)
    monkeypatch.setattr(autosync, "sleep", lambda seconds: None)
    return local, other, remote


def test_sync_merges_every_account_and_is_noop_when_repeated(repos):
    local, other, remote = repos
    store.save_profile("work", profile(3))
    store.save_profile("local-only", profile(2))
    store.set_active("work")
    store.save_codex_auth(profile(3))
    save_external(other, "work", profile(2))
    save_external(other, "remote-only", profile(2))
    commit(other)
    git(other, "push")

    report = sync_once(local)

    for name, version in (("work", 3), ("local-only", 2), ("remote-only", 2)):
        assert store.load_profile(name) == profile(version)
        assert json.loads((local / f"{name}.json").read_text()) == profile(version)
        assert json.loads(git(remote, "show", f"main:{name}.json").stdout) == profile(version)
    assert json.loads(store.CODEX_AUTH.read_text()) == profile(3)
    assert report.published
    head = git(local, "rev-parse", "HEAD").stdout
    second = sync_once(local)
    assert not second.published
    assert not second.imported and not second.exported and not second.skipped
    assert git(local, "rev-parse", "HEAD").stdout == head
    assert git(local, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize("command", [[], ["watch"]])
def test_default_and_watch_sync_before_usage_and_publish_refreshed_tokens(
    repos, monkeypatch, command
):
    local, other, remote = repos
    store.save_profile("work", profile(1))
    store.set_active("work")
    store.save_codex_auth(profile(1))
    save_external(other, "work", profile(3))
    save_external(other, "shared", profile(2))
    commit(other)
    git(other, "push")
    events = []
    real_sync = cli_module.sync_once
    real_render = cli_module.render_table

    def sync_profiles(directory, **kwargs):
        events.append("sync")
        return real_sync(directory, **kwargs)

    async def fetch_usage(profiles):
        events.append("usage")
        assert profiles == {"shared": profile(2), "work": profile(3)}
        assert json.loads(store.CODEX_AUTH.read_text()) == profile(3)
        store.save_profile("work", profile(4))
        return UsageFetchSummary(
            usage_map={name: UsageResult(secondary_pct=25) for name in profiles},
            refreshed_profiles=["work"],
        )

    def render(profiles, *args, **kwargs):
        events.append("table")
        assert profiles == ["shared", "work"]
        return real_render(profiles, *args, **kwargs)

    def activate_prompt(profiles):
        events.append("activation")
        assert not command, "watch must not prompt"
        assert profiles == ["shared", "work"]

    def sleep(seconds):
        events.append("sleep")
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "sync_once", sync_profiles)
    monkeypatch.setattr(cli_module, "fetch_all_usage", fetch_usage)
    monkeypatch.setattr(cli_module, "render_table", render)
    monkeypatch.setattr(cli_module, "interactive_prompt", activate_prompt)
    monkeypatch.setattr(cli_module, "sleep", sleep)
    monkeypatch.setattr(cli_module, "monotonic", lambda: 0)
    monkeypatch.setattr(cli_module, "_confirm_yes_no", lambda *a, **k: pytest.fail("no sync prompts"))
    monkeypatch.setattr(cli_module.click, "confirm", lambda *a, **k: pytest.fail("no sync prompts"))

    result = CliRunner().invoke(cli, command, terminal_width=160)

    assert result.exit_code == 0, result.output
    assert events == ["sync", "usage", "table", "sync", "sleep" if command else "activation"]
    assert "shared" in result.output and "work" in result.output and "25%" in result.output
    assert store.load_profile("work") == profile(4)
    assert json.loads(store.CODEX_AUTH.read_text()) == profile(4)
    assert json.loads(git(remote, "show", "main:work.json").stdout) == profile(4)
    assert git(local, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize("command", [[], ["list"]])
def test_display_without_automatic_sync_keeps_activation_available(
    saved_profile, tmp_path, monkeypatch, command
):
    # A bare invocation works without configuration; explicit list remains a local view.
    monkeypatch.setattr(cli_module, "get_sync_dir", lambda: tmp_path if command else None)
    monkeypatch.setattr(cli_module, "sync_once", lambda *a, **k: pytest.fail("unexpected sync"))
    activated = []

    async def fetch_usage(profiles):
        return UsageFetchSummary(
            usage_map={"work": UsageResult(secondary_pct=25)}, refreshed_profiles=[]
        )

    monkeypatch.setattr(cli_module, "fetch_all_usage", fetch_usage)
    monkeypatch.setattr(cli_module, "interactive_prompt", lambda profiles: activated.append(profiles))

    result = CliRunner().invoke(cli, command, terminal_width=160)

    assert result.exit_code == 0, result.output
    assert activated == [["work"]]
    assert "work" in result.output and "25%" in result.output
    assert "Sync needs attention" not in result.output
    assert "Missing CODEXAUTH_SYNC_DIR" not in result.output


@pytest.mark.parametrize("error_type", [SyncError, SyncBusyError, OSError, ValueError])
def test_default_shows_local_table_and_allows_activation_after_sync_failure(
    saved_profile, tmp_path, monkeypatch, error_type
):
    calls = []

    def sync_profiles(directory, **kwargs):
        calls.append(directory)
        raise error_type("Unavailable")

    async def fetch_usage(profiles):
        assert calls == [tmp_path]
        return UsageFetchSummary(
            usage_map={"work": UsageResult(secondary_pct=25)}, refreshed_profiles=["work"]
        )

    monkeypatch.setattr(cli_module, "get_sync_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_module, "sync_once", sync_profiles)
    monkeypatch.setattr(cli_module, "fetch_all_usage", fetch_usage)
    monkeypatch.setattr(cli_module, "interactive_prompt", lambda profiles: "work")
    monkeypatch.setattr(cli_module, "_confirm_yes_no", lambda *a, **k: pytest.fail("no sync prompts"))

    result = CliRunner().invoke(cli, [], terminal_width=160)

    assert result.exit_code == 0, result.output
    assert calls == [tmp_path], "a failed sync must not immediately be repeated after usage lookup"
    assert store.get_active() == "work"
    assert "Unavailable" in result.output and "Showing local profiles" in result.output
    assert "work" in result.output and "25%" in result.output


def test_default_still_shows_table_when_sync_skips_a_profile(saved_profile, tmp_path, monkeypatch):
    async def fetch_usage(profiles):
        return UsageFetchSummary(
            usage_map={"work": UsageResult(secondary_pct=25)}, refreshed_profiles=[]
        )

    monkeypatch.setattr(cli_module, "get_sync_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_module, "sync_once", lambda *a, **k: SyncReport(skipped={"work": "Tied dates"}))
    monkeypatch.setattr(cli_module, "fetch_all_usage", fetch_usage)
    monkeypatch.setattr(cli_module, "interactive_prompt", lambda profiles: None)

    result = CliRunner().invoke(cli, [], terminal_width=160)

    assert result.exit_code == 0, result.output
    assert "Skipped work: Tied dates" in result.output
    assert "work" in result.output and "25%" in result.output


def test_rejected_push_refetches_and_merges_whole_newer_credentials(repos, monkeypatch):
    local, other, remote = repos
    store.save_profile("work", profile(2))
    store.set_active("work")
    store.save_codex_auth(profile(2))
    real_git = SyncRepo.git
    pushes = []
    notices = []

    def racing_git(self, *args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
            if len(pushes) == 1:
                save_external(other, "work", profile(4))
                (other / "README.md").write_text("An unrelated remote update\n")
                commit(other)
                git(other, "push")
        return real_git(self, *args, **kwargs)

    monkeypatch.setattr(SyncRepo, "git", racing_git)
    report = sync_once(local, notify=notices.append)

    assert report.attempts == 2
    assert len(pushes) == 2
    assert any("Retrying sync" in notice for notice in notices)
    assert store.load_profile("work") == profile(4)
    assert json.loads(store.CODEX_AUTH.read_text()) == profile(4)
    assert json.loads((local / "work.json").read_text()) == profile(4)
    assert json.loads(git(remote, "show", "main:work.json").stdout) == profile(4)
    assert (local / "README.md").read_text() == "An unrelated remote update\n"
    assert git(local, "status", "--porcelain").stdout == ""


def test_sync_skips_ambiguous_profile_and_publishes_other_accounts(repos, monkeypatch):
    local, _, remote = repos
    ambiguous = profile(1)
    ambiguous["tokens"]["refresh_token"] = "different-but-undated"
    store.save_profile("work", ambiguous)
    store.save_profile("good", profile(3))
    monkeypatch.setattr(cli_module.click, "prompt", lambda *a, **k: pytest.fail("must not prompt"))
    monkeypatch.setattr(cli_module.click, "confirm", lambda *a, **k: pytest.fail("must not prompt"))

    result = CliRunner().invoke(cli, ["sync"])

    assert result.exit_code == 2, result.output
    assert "Skipped work" in result.output
    assert "need attention" in " ".join(result.output.split())
    assert store.load_profile("work") == ambiguous
    assert json.loads((local / "work.json").read_text()) == profile(1)
    assert json.loads(git(remote, "show", "main:good.json").stdout) == profile(3)
    assert "different-but-undated" not in result.output


def test_ambiguous_git_versions_are_preserved_without_starting_a_merge(repos):
    local, other, _ = repos
    left, right = profile(1), profile(1)
    left["tokens"]["refresh_token"] = "left-version"
    right["tokens"]["refresh_token"] = "right-version"
    save_external(local, "work", left)
    commit(local)
    save_external(other, "work", right)
    commit(other)
    git(other, "push")
    head = git(local, "rev-parse", "HEAD").stdout

    with pytest.raises(SyncError, match="needs attention"):
        sync_once(local)

    assert json.loads((local / "work.json").read_text()) == left
    assert git(local, "rev-parse", "HEAD").stdout == head
    assert git(local, "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode != 0
    assert git(local, "status", "--porcelain").stdout == ""


def test_sync_merges_hidden_changes_in_both_directions(repos):
    local, other, remote = repos
    store.save_profile("work", profile(1))
    store.save_profile("personal", profile(2))
    store.hide_profile("work")
    sync_once(local)
    git(other, "pull", "--ff-only")
    (other / "hidden").write_text("personal\nwork\n")
    commit(other)
    git(other, "push")
    store.unhide_profile("work")

    sync_once(local)

    assert store.list_hidden_profiles() == {"personal"}
    assert git(remote, "show", "main:hidden").stdout == "personal\n"
    sync_once(local)
    assert store.list_hidden_profiles() == {"personal"}


def test_sync_respects_remote_blacklist(repos):
    local, other, remote = repos
    store.save_profile("work", profile(3))
    store.set_active("work")
    store.save_codex_auth(profile(3))
    (other / ".gitignore").write_text("work.json\n")
    commit(other)
    git(other, "push")

    report = sync_once(local)

    assert report.removed == {"work"}
    assert store.list_profiles() == []
    assert store.get_active() is None
    assert json.loads(git(remote, "show", "main:work.json").stdout) == profile(1)


@pytest.mark.parametrize("blacklisted", [True, False])
@pytest.mark.parametrize("history", ["local-ahead", "remote-ahead", "diverged"])
def test_unchanged_unsupported_git_profile_does_not_block_other_accounts(
    repos, blacklisted, history
):
    local, other, remote = repos
    legacy = {"auth_mode": "apikey", "OPENAI_API_KEY": "fake-legacy-key"}
    save_external(local, "gloriaold", legacy)
    # Reproduce a tracked historical profile that was subsequently blacklisted.
    git(local, "add", "gloriaold.json")
    if blacklisted:
        (local / ".gitignore").write_text("gloriaold.json\n")
    commit(local)
    git(local, "push")
    git(other, "pull", "--ff-only")
    if history != "remote-ahead":
        save_external(local, "work", profile(2))
        commit(local)
    if history != "local-ahead":
        save_external(other, "work", profile(3))
        commit(other)
        git(other, "push")
    store.save_profile("work", profile(4))

    report = sync_once(local)

    assert store.load_profile("work") == profile(4)
    assert json.loads(git(remote, "show", "main:work.json").stdout) == profile(4)
    assert store.list_profiles() == ["work"]
    assert ("gloriaold" in report.skipped) is not blacklisted
    assert json.loads((local / "gloriaold.json").read_text()) == legacy
    assert json.loads(git(remote, "show", "main:gloriaold.json").stdout) == legacy
    assert git(local, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize("blacklist_source", ["local", "remote"])
def test_git_merge_ignores_changed_profile_blacklisted_on_either_branch(repos, blacklist_source):
    local, other, remote = repos
    save_external(local, "retired", profile(1))
    commit(local)
    git(local, "push")
    git(other, "pull", "--ff-only")
    blacklist_repo = local if blacklist_source == "local" else other
    (blacklist_repo / ".gitignore").write_text("retired.json\n")
    commit(blacklist_repo)
    legacy = {"auth_mode": "apikey", "OPENAI_API_KEY": "fake-legacy-key"}
    save_external(other, "retired", legacy)
    save_external(other, "work", profile(2))
    commit(other)
    git(other, "push")
    store.save_profile("retired", profile(1))
    store.save_profile("work", profile(3))

    report = sync_once(local)

    assert report.removed == {"retired"}
    assert not report.skipped
    assert store.list_profiles() == ["work"]
    assert json.loads(git(remote, "show", "main:work.json").stdout) == profile(3)
    assert json.loads((local / "retired.json").read_text()) == legacy
    assert git(local, "status", "--porcelain").stdout == ""


def test_sync_does_not_stage_unrelated_working_files(repos):
    local, _, remote = repos
    store.save_profile("work", profile(2))
    (local / "notes.txt").write_text("private local note")
    (local / "README.md").write_text("unfinished edit")

    sync_once(local)

    assert (local / "notes.txt").read_text() == "private local note"
    assert (local / "README.md").read_text() == "unfinished edit"
    assert git(remote, "show", "main:README.md").stdout == "Shared credential repository\n"
    assert git(remote, "show", "main:notes.txt", check=False).returncode != 0


def test_sync_leaves_unrelated_staged_changes_untouched(repos):
    local, _, _ = repos
    (local / "README.md").write_text("staged user edit")
    git(local, "add", "README.md")
    before = git(local, "diff", "--cached").stdout

    with pytest.raises(SyncError, match="Unrelated staged"):
        sync_once(local)

    assert git(local, "diff", "--cached").stdout == before


def test_transient_fetch_failure_is_retried_and_bounded(repos, monkeypatch):
    local, _, _ = repos
    real_git = SyncRepo.git
    attempts, delays = [], []

    def offline(self, *args, **kwargs):
        if args[0] == "fetch":
            attempts.append(args)
            raise SyncError("Network unavailable", retryable=True)
        return real_git(self, *args, **kwargs)

    monkeypatch.setattr(SyncRepo, "git", offline)
    monkeypatch.setattr(autosync, "sleep", delays.append)

    with pytest.raises(SyncError, match="Network unavailable"):
        sync_once(local)
    assert len(attempts) == 3
    assert delays == [1, 2]


def test_git_calls_cannot_wait_for_terminal_input(tmp_path, monkeypatch):
    repo = object.__new__(SyncRepo)
    repo.root = tmp_path
    captured = []

    def run(*args, **kwargs):
        captured.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(autosync.subprocess, "run", run)
    repo.git("fetch", "origin")

    _, options = captured[0]
    assert options["stdin"] == subprocess.DEVNULL
    assert options["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert options["env"]["GCM_INTERACTIVE"] == "Never"
    assert options["timeout"] == 30


def test_sync_lock_excludes_another_process_and_releases_after_interrupt(tmp_path):
    script = """
from pathlib import Path
from codexauth import store
from codexauth.locking import SyncBusyError, sync_lock
import sys
store.STORE_DIR = Path(sys.argv[1])
try:
    with sync_lock():
        print('acquired')
except SyncBusyError:
    print('busy')
"""
    def child():
        return subprocess.run(
            [sys.executable, "-c", script, str(store.STORE_DIR)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True,
        ).stdout.strip()

    with pytest.raises(KeyboardInterrupt):
        with sync_lock():
            with sync_lock():
                assert child() == "busy"
            raise KeyboardInterrupt
    assert child() == "acquired"


@pytest.mark.parametrize("option", ["--watch", "--interval"])
def test_sync_has_no_watch_options(option):
    result = CliRunner().invoke(cli, ["sync", option])
    assert result.exit_code == 2
    assert f"No such option: {option}" in result.output


def test_local_refresh_during_publication_is_synced_on_retry(repos, monkeypatch):
    local, _, remote = repos
    store.save_profile("work", profile(2))
    real_git = SyncRepo.git
    pushes = []

    def refreshing_git(self, *args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
            if len(pushes) == 1:
                store.save_profile("work", profile(3))
        return real_git(self, *args, **kwargs)

    monkeypatch.setattr(SyncRepo, "git", refreshing_git)
    report = sync_once(local)

    assert report.attempts == 2
    assert len(pushes) == 2
    assert store.load_profile("work") == profile(3)
    assert json.loads(git(remote, "show", "main:work.json").stdout) == profile(3)


def test_changed_destination_is_recompared_instead_of_overwritten(repos, monkeypatch):
    local, _, remote = repos
    store.save_profile("work", profile(2))
    real_write = autosync._write
    changed = []

    def concurrent_write(path, raw, expected):
        if path == local / "work.json" and not changed:
            changed.append(path)
            save_external(local, "work", profile(4))
        return real_write(path, raw, expected)

    monkeypatch.setattr(autosync, "_write", concurrent_write)
    report = sync_once(local)

    assert report.attempts == 2
    assert store.load_profile("work") == profile(4)
    assert json.loads(git(remote, "show", "main:work.json").stdout) == profile(4)


@pytest.mark.parametrize("existing_merge", [True, False])
def test_unrelated_git_conflict_preserves_user_data(repos, existing_merge):
    local, other, _ = repos
    (local / "README.md").write_text("Local README\n")
    commit(local)
    (other / "README.md").write_text("Remote README\n")
    commit(other)
    git(other, "push")
    if existing_merge:
        git(local, "fetch", "origin")
        assert git(local, "merge", "--no-commit", "origin/main", check=False).returncode != 0
    before = (local / "README.md").read_bytes()
    status = git(local, "status", "--porcelain").stdout
    head = git(local, "rev-parse", "HEAD").stdout

    with pytest.raises(SyncError):
        sync_once(local)

    assert (local / "README.md").read_bytes() == before
    assert git(local, "status", "--porcelain").stdout == status
    assert git(local, "rev-parse", "HEAD").stdout == head
    has_merge = git(local, "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode == 0
    assert has_merge is existing_merge


def test_ctrl_c_aborts_only_the_merge_started_by_sync(repos, monkeypatch):
    local, other, _ = repos
    save_external(local, "work", profile(2))
    commit(local)
    save_external(other, "work", profile(3))
    commit(other)
    git(other, "push")
    real_git = SyncRepo.git

    def interrupted_merge(self, *args, **kwargs):
        result = real_git(self, *args, **kwargs)
        if args[:2] == ("merge", "--no-commit"):
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(SyncRepo, "git", interrupted_merge)
    with pytest.raises(KeyboardInterrupt):
        sync_once(local)

    assert git(local, "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode != 0
    assert json.loads((local / "work.json").read_text()) == profile(2)
    assert git(local, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize("invalid", [b"not json", b"[]"])
def test_invalid_active_auth_does_not_block_other_profiles(repos, invalid):
    local, _, remote = repos
    store.save_profile("work", profile(1))
    store.save_profile("good", profile(3))
    store.set_active("work")
    store.CODEX_AUTH.parent.mkdir(parents=True, exist_ok=True)
    store.CODEX_AUTH.write_bytes(invalid)

    report = sync_once(local)

    assert "work" in report.skipped
    assert store.CODEX_AUTH.read_bytes() == invalid
    assert json.loads(git(remote, "show", "main:good.json").stdout) == profile(3)


def test_sync_supports_a_subdirectory_and_literal_profile_names(repos):
    local, _, remote = repos
    directory = local / "nested" / "profiles"
    directory.mkdir(parents=True)
    name = "work[1]"
    store.save_profile(name, profile(2))

    report = sync_once(directory)

    assert not report.skipped
    assert store.list_profiles() == [name]
    assert json.loads(git(remote, "show", f"main:nested/profiles/{name}.json").stdout) == profile(2)
    assert json.loads(git(remote, "show", "main:work.json").stdout) == profile(1)
