import base64
import json
import os

import click
import pytest

import codexauth.store as store_module
from codexauth.reconcile import reconcile_active_to_store, reconcile_imported_active_profile


def _jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}."


def _profile(account_id: str | None = "acct-1", iss: str = "https://auth.example", sub: str = "user-1") -> dict:
    tokens = {
        "access_token": "access",
        "refresh_token": "refresh",
        "id_token": _jwt({"iss": iss, "sub": sub}),
    }
    if account_id is not None:
        tokens["account_id"] = account_id
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": tokens,
        "last_refresh": "2025-01-01T00:00:00+00:00",
    }


def test_reconcile_active_to_store_updates_matching_profile():
    store_module.save_profile("work", _profile(account_id="acct-1"))
    store_module.set_active("work")
    store_module.save_codex_auth(_profile(account_id="acct-1"))
    auth_data = _profile(account_id="acct-1")
    auth_data["tokens"]["access_token"] = "new-access"
    store_module.save_codex_auth(auth_data)

    result = reconcile_active_to_store()

    assert result.status == "updated"
    assert store_module.load_profile("work")["tokens"]["access_token"] == "new-access"


def test_reconcile_active_to_store_uses_iss_sub_when_account_missing():
    store_module.save_profile("work", _profile(account_id=None))
    store_module.set_active("work")
    auth_data = _profile(account_id=None)
    auth_data["tokens"]["access_token"] = "new-access"
    store_module.save_codex_auth(auth_data)

    result = reconcile_active_to_store()

    assert result.status == "updated"
    assert store_module.load_profile("work")["tokens"]["access_token"] == "new-access"


def test_reconcile_active_to_store_is_noop_when_equal():
    profile = _profile(account_id="acct-1")
    store_module.save_profile("work", profile)
    path = store_module.TOKENS_DIR / "work.json"
    os.utime(path, (1_700_000_000, 1_700_000_000))
    store_module.set_active("work")
    store_module.save_codex_auth(profile)

    result = reconcile_active_to_store()

    assert result.status == "noop"
    assert int(path.stat().st_mtime) == 1_700_000_000


def test_reconcile_active_to_store_is_unsafe_on_unconfirmed_identity():
    local = _profile(account_id=None)
    local["tokens"]["id_token"] = "not-a-jwt"
    auth = _profile(account_id=None)
    auth["tokens"]["id_token"] = "also-not-a-jwt"
    auth["tokens"]["access_token"] = "new-access"
    store_module.save_profile("work", local)
    store_module.set_active("work")
    store_module.save_codex_auth(auth)

    result = reconcile_active_to_store()

    assert result.status == "unsafe"


def test_reconcile_imported_active_profile_updates_auth_from_newer_store():
    profile = _profile(account_id="acct-1")
    store_module.save_profile("work", profile)
    store_path = store_module.TOKENS_DIR / "work.json"
    os.utime(store_path, (1_700_000_000, 1_700_000_000))
    store_module.set_active("work")

    auth = _profile(account_id="acct-1")
    auth["tokens"]["access_token"] = "old-access"
    store_module.save_codex_auth(auth)
    os.utime(store_module.CODEX_AUTH, (1_600_000_000, 1_600_000_000))

    result = reconcile_imported_active_profile({"work"})

    assert result.status == "updated"
    assert json.loads(store_module.CODEX_AUTH.read_text())["tokens"]["access_token"] == "access"


def test_reconcile_imported_active_profile_prompts_when_ambiguous(monkeypatch):
    profile = _profile(account_id="acct-1")
    store_module.save_profile("work", profile)
    store_path = store_module.TOKENS_DIR / "work.json"
    os.utime(store_path, (1_700_000_000, 1_700_000_000))
    store_module.set_active("work")

    auth = _profile(account_id="acct-1")
    auth["tokens"]["access_token"] = "local-access"
    store_module.save_codex_auth(auth)
    os.utime(store_module.CODEX_AUTH, (1_700_000_000, 1_700_000_000))

    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: "auth")

    result = reconcile_imported_active_profile({"work"})

    assert result.status == "updated"
    assert store_module.load_profile("work")["tokens"]["access_token"] == "local-access"


def test_reconcile_imported_active_profile_prompts_when_recency_signals_disagree(monkeypatch):
    store_profile = _profile(account_id="acct-1")
    store_profile["last_refresh"] = "2025-01-03T00:00:00+00:00"
    store_module.save_profile("work", store_profile)
    store_path = store_module.TOKENS_DIR / "work.json"
    os.utime(store_path, (1_600_000_000, 1_600_000_000))
    store_module.set_active("work")

    auth = _profile(account_id="acct-1")
    auth["tokens"]["access_token"] = "local-access"
    auth["last_refresh"] = "2025-01-02T00:00:00+00:00"
    store_module.save_codex_auth(auth)
    os.utime(store_module.CODEX_AUTH, (1_700_000_000, 1_700_000_000))

    monkeypatch.setattr(click, "prompt", lambda *args, **kwargs: "store")

    result = reconcile_imported_active_profile({"work"})

    assert result.status == "updated"
    assert json.loads(store_module.CODEX_AUTH.read_text())["tokens"]["access_token"] == "access"
