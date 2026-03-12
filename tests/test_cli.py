"""End-to-end CLI tests using click's CliRunner."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from codexauth.cli import cli
import codexauth.git_sync as git_sync_module
import codexauth.store as store_module


@pytest.fixture
def runner():
    return CliRunner()


def test_help(runner):
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "add" in result.output
    assert "import" in result.output
    assert "list" in result.output
    assert "use" in result.output
    assert "remove" in result.output
    assert "export" in result.output
    assert "pull" in result.output
    assert "push" in result.output
    assert "status" in result.output


def test_add_from_file(runner, sample_profile, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps(sample_profile))
    os.utime(auth_file, (1_700_000_000, 1_700_000_000))

    result = runner.invoke(cli, ["add", "work", "--file", str(auth_file)])
    assert result.exit_code == 0
    assert "Saved profile" in result.output
    assert store_module.list_profiles() == ["work"]
    assert int((store_module.TOKENS_DIR / "work.json").stat().st_mtime) == 1_700_000_000


def test_add_missing_file(runner):
    result = runner.invoke(cli, ["add", "work", "--file", "/nonexistent/auth.json"])
    assert result.exit_code != 0


def test_add_default_preserves_source_mtime(runner, sample_profile):
    store_module.CODEX_AUTH.parent.mkdir(parents=True, exist_ok=True)
    store_module.CODEX_AUTH.write_text(json.dumps(sample_profile))
    os.utime(store_module.CODEX_AUTH, (1_600_000_000, 1_600_000_000))

    result = runner.invoke(cli, ["add", "work"])

    assert result.exit_code == 0
    saved_path = store_module.TOKENS_DIR / "work.json"
    assert int(saved_path.stat().st_mtime) == 1_600_000_000


def test_status_none(runner):
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "No profile" in result.output


def test_use_and_status(runner, saved_profile):
    result = runner.invoke(cli, ["use", "work"])
    assert result.exit_code == 0
    assert "Activated" in result.output

    result = runner.invoke(cli, ["status"])
    assert "work" in result.output


def test_use_not_found(runner):
    result = runner.invoke(cli, ["use", "ghost"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_remove(runner, saved_profile):
    result = runner.invoke(cli, ["remove", "work"])
    assert result.exit_code == 0
    assert store_module.list_profiles() == []


def test_remove_clears_active(runner, saved_profile):
    store_module.set_active("work")
    runner.invoke(cli, ["remove", "work"])
    assert store_module.get_active() is None


def test_remove_not_found(runner):
    result = runner.invoke(cli, ["remove", "ghost"])
    assert result.exit_code != 0


def test_list_no_profiles(runner):
    result = runner.invoke(cli, ["list", "--no-usage"])
    assert result.exit_code == 0
    assert "No profiles" in result.output


def test_list_shows_profiles(runner, saved_profile):
    result = runner.invoke(cli, ["list", "--no-usage", "--no-interactive"])
    assert result.exit_code == 0
    assert "work" in result.output


def test_import_requires_sync_dir(runner, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["import"])

    assert result.exit_code != 0
    assert "CODEXAUTH_SYNC_DIR" in result.output


def test_import_selected_profiles(runner, sample_profile, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    work_path = sync_dir / "work.json"
    work_path.write_text(json.dumps(sample_profile))
    personal = dict(sample_profile)
    personal["tokens"] = dict(sample_profile["tokens"])
    personal["tokens"]["account_id"] = "personal-account-id"
    personal_path = sync_dir / "personal.json"
    personal_path.write_text(json.dumps(personal))
    os.utime(personal_path, (1_700_000_000, 1_700_000_000))
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["import"], input="1\n")

    assert result.exit_code == 0
    assert "Imported profile personal" in result.output
    assert store_module.list_profiles() == ["personal"]
    imported_path = store_module.TOKENS_DIR / "personal.json"
    assert int(imported_path.stat().st_mtime) == 1_700_000_000


def test_import_overwrite_shows_timestamps_and_can_skip(
    runner, sample_profile, monkeypatch, tmp_path
):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    external = dict(sample_profile)
    external["tokens"] = dict(sample_profile["tokens"])
    external["tokens"]["account_id"] = "external-account-id"
    external_path = sync_dir / "work.json"
    external_path.write_text(json.dumps(external))
    os.utime(external_path, (1_700_000_000, 1_700_000_000))

    store_module.save_profile("work", sample_profile)
    local_path = store_module.TOKENS_DIR / "work.json"
    os.utime(local_path, (1_600_000_000, 1_600_000_000))

    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["import"], input="1\nn\n")

    assert result.exit_code == 0
    assert "overwrites local" in result.output
    assert "2023-11-14" in result.output
    assert "2020-09-13" in result.output
    assert "No profiles imported" in result.output
    assert store_module.load_profile("work")["tokens"]["account_id"] == "fake-account-id"


def test_export_selected_profiles_creates_external_copy(
    runner, saved_profile, monkeypatch, tmp_path
):
    sync_dir = tmp_path / "sync"
    local_path = store_module.TOKENS_DIR / "work.json"
    os.utime(local_path, (1_700_000_000, 1_700_000_000))
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["export"], input="1\n")

    assert result.exit_code == 0
    assert "Exported profile work" in result.output
    exported = sync_dir / "work.json"
    assert exported.exists()
    assert json.loads(exported.read_text())["auth_mode"] == "chatgpt"
    assert stat.S_IMODE(exported.stat().st_mode) == 0o600
    assert int(exported.stat().st_mtime) == 1_700_000_000


def test_export_overwrite_can_confirm(runner, saved_profile, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    existing = sync_dir / "work.json"
    existing.write_text(json.dumps({"auth_mode": "old"}))
    os.utime(existing, (1_600_000_000, 1_600_000_000))

    local_path = store_module.TOKENS_DIR / "work.json"
    os.utime(local_path, (1_700_000_000, 1_700_000_000))

    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["export"], input="1\ny\n")

    assert result.exit_code == 0
    assert "overwrites external" in result.output
    assert "2023-11-14" in result.output
    assert "2020-09-13" in result.output
    assert json.loads(existing.read_text())["auth_mode"] == "chatgpt"


def test_pull_requires_git_repo(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["pull"])

    assert result.exit_code != 0
    assert "rev-parse --is-inside-work-tree failed" in result.output


def test_pull_success(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    calls = []

    def fake_run(cmd, cwd, capture_output, text, check):
        calls.append((cmd, cwd))
        if cmd == ["git", "rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
        if cmd == ["git", "pull"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="Already up to date.\n", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(git_sync_module.subprocess, "run", fake_run)

    result = runner.invoke(cli, ["pull"])

    assert result.exit_code == 0
    assert "Pulled sync repo" in result.output
    assert "Already up to date." in result.output
    assert calls == [
        (["git", "rev-parse", "--is-inside-work-tree"], sync_dir),
        (["git", "pull"], sync_dir),
    ]


def test_push_no_changes_is_success(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    calls = []

    def fake_run(cmd, cwd, capture_output, text, check):
        calls.append((cmd, cwd))
        if cmd == ["git", "rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
        if cmd == ["git", "add", "."]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "diff", "--cached", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(git_sync_module.subprocess, "run", fake_run)

    result = runner.invoke(cli, ["push"])

    assert result.exit_code == 0
    assert "No changes to commit." in result.output
    assert "Pushed sync repo" not in result.output


def test_push_success(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    calls = []

    def fake_run(cmd, cwd, capture_output, text, check):
        calls.append((cmd, cwd))
        if cmd == ["git", "rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
        if cmd == ["git", "add", "."]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "diff", "--cached", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd == ["git", "commit", "-m", "Update exported codexauth profiles"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[main abc123] Update exported codexauth profiles\n", stderr="")
        if cmd == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="pushed\n", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(git_sync_module.subprocess, "run", fake_run)

    result = runner.invoke(cli, ["push"])

    assert result.exit_code == 0
    assert "Pushed sync repo" in result.output
    assert "pushed" in result.output
    assert calls == [
        (["git", "rev-parse", "--is-inside-work-tree"], sync_dir),
        (["git", "add", "."], sync_dir),
        (["git", "diff", "--cached", "--quiet"], sync_dir),
        (["git", "commit", "-m", "Update exported codexauth profiles"], sync_dir),
        (["git", "push"], sync_dir),
    ]


def test_push_commit_failure_stops_before_push(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    calls = []

    def fake_run(cmd, cwd, capture_output, text, check):
        calls.append((cmd, cwd))
        if cmd == ["git", "rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
        if cmd == ["git", "add", "."]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "diff", "--cached", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd == ["git", "commit", "-m", "Update exported codexauth profiles"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="missing user.email\n")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(git_sync_module.subprocess, "run", fake_run)

    result = runner.invoke(cli, ["push"])

    assert result.exit_code != 0
    assert "git commit -m Update exported codexauth profiles failed" in result.output
    assert "missing user.email" in result.output
    assert (["git", "push"], sync_dir) not in calls
