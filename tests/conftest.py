"""Shared fixtures for all tests."""

import pytest
import codexauth.store as store_module


SAMPLE_PROFILE = {
    "auth_mode": "chatgpt",
    "OPENAI_API_KEY": None,
    "tokens": {
        "id_token": "fake.id.token",
        "access_token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
        "account_id": "fake-account-id",
    },
    "last_refresh": "2025-01-01T00:00:00+00:00",
}


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Redirect all store paths to a temporary directory for each test."""
    store_dir = tmp_path / ".codexauth"
    tokens_dir = store_dir / "tokens"
    active_file = store_dir / "active"
    codex_auth = tmp_path / ".codex" / "auth.json"
    codex_backup = store_dir / "auth.json.bak"

    monkeypatch.setattr(store_module, "STORE_DIR", store_dir)
    monkeypatch.setattr(store_module, "TOKENS_DIR", tokens_dir)
    monkeypatch.setattr(store_module, "ACTIVE_FILE", active_file)
    monkeypatch.setattr(store_module, "CODEX_AUTH", codex_auth)
    monkeypatch.setattr(store_module, "CODEX_AUTH_BACKUP", codex_backup)

    return store_dir


@pytest.fixture
def sample_profile():
    return dict(SAMPLE_PROFILE)


@pytest.fixture
def saved_profile(sample_profile):
    """Save a 'work' profile and return its data."""
    store_module.save_profile("work", sample_profile)
    return sample_profile
