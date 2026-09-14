"""Sync merges credentials without letting filesystem times cause a rollback."""

import copy
import importlib
import json
import os

import pytest
from click.testing import CliRunner

from codexauth import store
from codexauth.cli import cli

cli_module = importlib.import_module("codexauth.cli")


def _profile(version):
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "account_id": "same-account",
            "access_token": f"access-secret-{version}",
            "refresh_token": f"refresh-secret-{version}",
            "id_token": f"id-secret-{version}",
        },
        "last_refresh": f"2026-09-0{version}T12:00:00Z",
    }


@pytest.fixture
def sync_dir(tmp_path, monkeypatch):
    directory = tmp_path / "sync"
    directory.mkdir()
    monkeypatch.setattr(cli_module, "get_sync_dir", lambda: directory)
    return directory


@pytest.mark.parametrize("action", ["import", "export"])
@pytest.mark.parametrize("source_newer", [True, False])
def test_sync_orders_credentials_despite_reversed_file_times(sync_dir, action, source_newer):
    older, newer = _profile(1), _profile(2)
    source_data, dest_data = (newer, older) if source_newer else (older, newer)
    local_path = store.TOKENS_DIR / "work.json"
    external_path = sync_dir / "work.json"
    source, destination = (external_path, local_path) if action == "import" else (local_path, external_path)
    store.save_profile("work", source_data if action == "export" else dest_data)
    external_path.write_text(json.dumps(source_data if action == "import" else dest_data))
    # Simulate Git checking out older credentials later than the actual refresh.
    os.utime(source, (1_600_000_000 if source_newer else 1_700_000_000,) * 2)
    os.utime(destination, (1_700_000_000 if source_newer else 1_600_000_000,) * 2)
    dest_stat = destination.stat()

    result = CliRunner().invoke(cli, [action])

    assert result.exit_code == 0, result.output
    assert json.loads(source.read_text()) == source_data
    assert json.loads(destination.read_text()) == newer
    assert "anyway?" not in result.output
    assert destination.stat().st_ino == dest_stat.st_ino
    if source_newer:
        assert destination.stat().st_mtime_ns == source.stat().st_mtime_ns
    else:
        assert destination.stat().st_mtime_ns == dest_stat.st_mtime_ns
        assert "keeping newer" in result.output


@pytest.mark.parametrize("action", ["import", "export"])
def test_sync_skips_identical_json_without_touching_files(sync_dir, action):
    profile = _profile(1)
    store.save_profile("work", profile)
    local_path = store.TOKENS_DIR / "work.json"
    external_path = sync_dir / "work.json"
    external_path.write_text(json.dumps(profile, sort_keys=True))
    os.utime(local_path, (1_600_000_000,) * 2)
    os.utime(external_path, (1_700_000_000,) * 2)
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in (local_path, external_path)]

    result = CliRunner().invoke(cli, [action])

    assert result.exit_code == 0, result.output
    assert "already in sync" in result.output
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in (local_path, external_path)] == before


@pytest.mark.parametrize("active_version", [1, 3])
def test_push_merges_both_directions_and_active_auth_before_publication(sync_dir, monkeypatch, active_version):
    store.save_profile("work", _profile(1))
    store.save_profile("personal", _profile(3))
    store.set_active("work")
    store.save_codex_auth(_profile(active_version))
    store.hide_profile("personal")
    (sync_dir / "hidden").write_text("work\n")
    expected_work = _profile(max(2, active_version))
    published = []

    def pull(directory):
        assert directory == sync_dir
        for name, version in (("work", 2), ("personal", 1), ("remote-only", 2)):
            external = directory / f"{name}.json"
            external.write_text(json.dumps(_profile(version)))
            os.utime(external, (1_800_000_000,) * 2)
        return "Fetched."

    def publish(directory):
        assert store.load_profile("work") == expected_work
        assert json.loads(store.CODEX_AUTH.read_text()) == expected_work
        for name, expected in (("work", expected_work), ("personal", _profile(3)), ("remote-only", _profile(2))):
            assert store.load_profile(name) == json.loads((directory / f"{name}.json").read_text()) == expected
        assert (directory / "hidden").read_text() == "personal\n"
        published.append(directory)
        return "Pushed changes."

    def unexpected_prompt(*args, **kwargs):
        pytest.fail("Clearly ordered credentials must merge without confirmation")

    monkeypatch.setattr(cli_module, "pull_sync_repo", pull)
    monkeypatch.setattr(cli_module, "push_sync_repo", publish)
    monkeypatch.setattr(cli_module.click, "prompt", unexpected_prompt)
    monkeypatch.setattr(cli_module.click, "confirm", unexpected_prompt)

    result = CliRunner().invoke(cli, ["push"])

    assert result.exit_code == 0, result.exception
    assert published == [sync_dir]
    assert store.get_active() == "work"
    assert "access-secret" not in result.output
    assert "refresh-secret" not in result.output


@pytest.mark.parametrize("answer", ["y", "n"])
def test_push_asks_once_for_ambiguous_credentials(sync_dir, monkeypatch, answer):
    local = _profile(1)
    external = copy.deepcopy(local)
    external["tokens"]["refresh_token"] = "other-refresh-secret"
    store.save_profile("work", local)
    external_path = sync_dir / "work.json"
    external_path.write_text(json.dumps(external))
    monkeypatch.setattr(cli_module, "pull_sync_repo", lambda directory: "Already up to date.")
    monkeypatch.setattr(cli_module, "push_sync_repo", lambda directory: "Pushed changes.")

    result = CliRunner().invoke(cli, ["push"], input=f"{answer}\n")

    assert result.exit_code == 0, result.output
    assert result.output.count("Replace external with local anyway?") == 1
    assert json.loads(external_path.read_text()) == (local if answer == "y" else external)
    assert store.load_profile("work") == local
    assert "refresh-secret" not in result.output


def test_push_does_not_merge_or_export_blacklisted_profiles(sync_dir, monkeypatch):
    local, external = _profile(1), _profile(2)
    store.save_profile("banned", local)
    (sync_dir / "banned.json").write_text(json.dumps(external))
    (sync_dir / "external-banned.json").write_text(json.dumps(external))
    (sync_dir / ".gitignore").write_text("banned.json\nexternal-banned.json\n")
    monkeypatch.setattr(cli_module, "pull_sync_repo", lambda directory: "Already up to date.")
    monkeypatch.setattr(cli_module, "push_sync_repo", lambda directory: "Pushed changes.")

    result = CliRunner().invoke(cli, ["push"])

    assert result.exit_code == 0, result.output
    assert store.load_profile("banned") == local
    assert json.loads((sync_dir / "banned.json").read_text()) == external
    assert store.list_profiles() == ["banned"]
