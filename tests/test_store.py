"""Tests for codexauth.store."""

import json
import os

import pytest
import codexauth.store as store
from codexauth.store import ProfileNotFoundError


def test_list_profiles_empty():
    assert store.list_profiles() == []


def test_save_and_list(sample_profile):
    store.save_profile("work", sample_profile)
    store.save_profile("personal", sample_profile)
    assert store.list_profiles() == ["personal", "work"]


def test_load_profile(sample_profile):
    store.save_profile("work", sample_profile)
    loaded = store.load_profile("work")
    assert loaded["auth_mode"] == "chatgpt"
    assert loaded["tokens"]["account_id"] == "fake-account-id"


def test_load_profile_not_found():
    with pytest.raises(ProfileNotFoundError):
        store.load_profile("nonexistent")


def test_delete_profile(sample_profile):
    store.save_profile("work", sample_profile)
    store.delete_profile("work")
    assert store.list_profiles() == []


def test_delete_profile_not_found():
    with pytest.raises(ProfileNotFoundError):
        store.delete_profile("nonexistent")


def test_active_roundtrip():
    assert store.get_active() is None
    store.set_active("work")
    assert store.get_active() == "work"


def test_activate_copies_to_codex_auth(sample_profile, tmp_path):
    store.save_profile("work", sample_profile)
    store.activate("work")

    dest = store.CODEX_AUTH
    assert dest.exists()
    data = json.loads(dest.read_text())
    assert data["auth_mode"] == "chatgpt"
    assert store.get_active() == "work"


def test_activate_backs_up_existing(sample_profile, tmp_path):
    # Pre-create an existing auth.json
    store.CODEX_AUTH.parent.mkdir(parents=True, exist_ok=True)
    store.CODEX_AUTH.write_text(json.dumps({"auth_mode": "old"}))

    store.save_profile("work", sample_profile)
    store.activate("work")

    backup = store.CODEX_AUTH_BACKUP
    assert backup.exists()
    assert json.loads(backup.read_text())["auth_mode"] == "old"


def test_activate_not_found():
    with pytest.raises(ProfileNotFoundError):
        store.activate("ghost")


def test_profile_file_permissions(sample_profile):
    store.save_profile("work", sample_profile)
    path = store.TOKENS_DIR / "work.json"
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_save_profile_from_file_preserves_mtime(sample_profile, tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(sample_profile))
    os.utime(source, (1_700_000_000, 1_700_000_000))

    store.save_profile_from_file("work", source, preserve_mtime=True)

    dest = store.TOKENS_DIR / "work.json"
    assert int(dest.stat().st_mtime) == 1_700_000_000


def test_activate_preserves_inode_and_updates_hard_link(sample_profile, tmp_path):
    existing = {"auth_mode": "chatgpt", "tokens": {"access_token": "old-access"}}
    store.CODEX_AUTH.parent.mkdir(parents=True, exist_ok=True)
    store.CODEX_AUTH.write_text(json.dumps(existing))
    linked = tmp_path / "auth-hardlink.json"
    os.link(store.CODEX_AUTH, linked)
    original_inode = store.CODEX_AUTH.stat().st_ino

    store.save_profile("work", sample_profile)
    store.activate("work")

    assert store.CODEX_AUTH.stat().st_ino == original_inode
    assert linked.stat().st_ino == original_inode
    assert json.loads(linked.read_text())["tokens"]["access_token"] == "fake-access-token"


def test_save_codex_auth_preserves_inode_and_updates_hard_link(tmp_path):
    initial = {"auth_mode": "chatgpt", "tokens": {"access_token": "old-access"}}
    updated = {"auth_mode": "chatgpt", "tokens": {"access_token": "new-access"}}

    store.CODEX_AUTH.parent.mkdir(parents=True, exist_ok=True)
    store.CODEX_AUTH.write_text(json.dumps(initial))
    linked = tmp_path / "auth-hardlink.json"
    os.link(store.CODEX_AUTH, linked)
    original_inode = store.CODEX_AUTH.stat().st_ino

    store.save_codex_auth(updated)

    assert store.CODEX_AUTH.stat().st_ino == original_inode
    assert linked.stat().st_ino == original_inode
    assert json.loads(linked.read_text())["tokens"]["access_token"] == "new-access"
