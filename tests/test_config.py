"""Tests for codexauth.config."""

from pathlib import Path

from codexauth.config import get_sync_dir, load_dotenv


def test_load_dotenv_reads_simple_pairs(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nCODEXAUTH_SYNC_DIR=~/git/codexauthinfo\nOTHER=value\n"
    )

    values = load_dotenv(env_file)

    assert values["CODEXAUTH_SYNC_DIR"] == "~/git/codexauthinfo"
    assert values["OTHER"] == "value"


def test_get_sync_dir_expands_user(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("CODEXAUTH_SYNC_DIR=~/git/codexauthinfo\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    sync_dir = get_sync_dir(env_file)

    assert sync_dir == Path(tmp_path / "home" / "git" / "codexauthinfo")
