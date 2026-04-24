"""End-to-end CLI tests using click's CliRunner."""

import base64
import importlib
import json
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from click.testing import CliRunner

from codexauth.cli import cli
import codexauth.display as display_module
import codexauth.git_sync as git_sync_module
import codexauth.oauth as oauth_module
import codexauth.store as store_module
import codexauth.usage as usage_module
from codexauth.sync import format_modified

cli_module = importlib.import_module("codexauth.cli")


@pytest.fixture
def runner():
    return CliRunner()


def _jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}."


def test_help(runner):
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "add" in result.output
    assert "login" in result.output
    assert "list" in result.output
    assert "use" in result.output
    assert "remove" in result.output
    assert "pull" in result.output
    assert "push" in result.output
    assert "status" in result.output
    assert "  import" not in result.output
    assert "  export" not in result.output


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


def test_add_rejects_invalid_json(runner, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{not json")

    result = runner.invoke(cli, ["add", "work", "--file", str(auth_file)])

    assert result.exit_code != 0
    assert "Failed to parse" in result.output


def test_add_rejects_missing_auth_mode(runner, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"tokens": {"access_token": "abc"}}))

    result = runner.invoke(cli, ["add", "work", "--file", str(auth_file)])

    assert result.exit_code != 0
    assert "supported auth_mode" in result.output


def test_add_rejects_chatgpt_profile_without_access_token(runner, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {}}))

    result = runner.invoke(cli, ["add", "work", "--file", str(auth_file)])

    assert result.exit_code != 0
    assert "tokens.access_token" in result.output


def test_add_accepts_api_key_profile(runner, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "sk-test"}))

    result = runner.invoke(cli, ["add", "work", "--file", str(auth_file)])

    assert result.exit_code == 0
    assert store_module.list_profiles() == ["work"]


def test_add_default_preserves_source_mtime(runner, sample_profile):
    store_module.CODEX_AUTH.parent.mkdir(parents=True, exist_ok=True)
    store_module.CODEX_AUTH.write_text(json.dumps(sample_profile))
    os.utime(store_module.CODEX_AUTH, (1_600_000_000, 1_600_000_000))

    result = runner.invoke(cli, ["add", "work"])

    assert result.exit_code == 0
    saved_path = store_module.TOKENS_DIR / "work.json"
    assert int(saved_path.stat().st_mtime) == 1_600_000_000


def test_login_success_saves_profile_and_shows_list(runner, monkeypatch):
    callback_url = "http://127.0.0.1:1455/callback?code=abc123&state=state-1"
    pending_path = store_module.STORE_DIR / "pending-login.json"

    monkeypatch.setattr(oauth_module, "load_oauth_config", lambda: {
        "client_id": "client-123",
        "redirect_uri": "http://localhost:1455/auth/callback",
        "scope": "openid profile email offline_access",
        "originator": "codex_cli_rs",
    })
    monkeypatch.setattr(oauth_module.secrets, "token_urlsafe", lambda n: "state-1" if n == 32 else "verifier-1")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "id_token": "new-id",
            }

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            assert url == oauth_module.TOKEN_URL
            assert json["client_id"] == "client-123"
            assert json["grant_type"] == "authorization_code"
            assert json["code"] == "abc123"
            assert json["redirect_uri"] == "http://localhost:1455/auth/callback"
            assert json["code_verifier"] == "verifier-1"
            return FakeResponse()

    monkeypatch.setattr(oauth_module.httpx, "AsyncClient", FakeAsyncClient)

    result = runner.invoke(cli, ["login", "work"], input=f"{callback_url}\n")

    assert result.exit_code == 0
    assert "Open this URL in your browser:" in result.output
    assert "https://auth.openai.com/oauth/authorize?" in result.output
    assert "Saved profile work" in result.output
    assert "work" in result.output
    saved = store_module.load_profile("work")
    assert saved["tokens"]["access_token"] == "new-access"
    assert saved["tokens"]["refresh_token"] == "new-refresh"
    assert saved["tokens"]["id_token"] == "new-id"
    assert pending_path.exists() is False


def test_login_without_name_prompts_for_profile_name(runner, monkeypatch):
    callback_url = "http://127.0.0.1:1455/callback?code=abc123&state=state-1"
    pending_path = store_module.STORE_DIR / "pending-login.json"

    monkeypatch.setattr(oauth_module, "load_oauth_config", lambda: {
        "client_id": "client-123",
        "redirect_uri": "http://localhost:1455/auth/callback",
        "scope": "openid profile email offline_access",
        "originator": "codex_cli_rs",
    })
    monkeypatch.setattr(oauth_module.secrets, "token_urlsafe", lambda n: "state-1" if n == 32 else "verifier-1")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
            }

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            return FakeResponse()

    monkeypatch.setattr(oauth_module.httpx, "AsyncClient", FakeAsyncClient)

    result = runner.invoke(cli, ["login"], input=f"{callback_url}\npersonal\n")

    assert result.exit_code == 0
    assert "Profile name" in result.output
    assert "Saved profile personal" in result.output
    assert store_module.load_profile("personal")["tokens"]["access_token"] == "new-access"
    assert pending_path.exists() is False


def test_login_uses_default_redirect_uri(runner, monkeypatch):
    monkeypatch.setattr(oauth_module, "load_dotenv", lambda path=None: {})

    config = oauth_module.load_oauth_config()

    assert config["redirect_uri"] == oauth_module.DEFAULT_REDIRECT_URI
    assert config["originator"] == oauth_module.DEFAULT_ORIGINATOR


def test_begin_login_uses_codex_style_authorize_params(monkeypatch):
    monkeypatch.setattr(oauth_module, "load_oauth_config", lambda: {
        "client_id": "client-123",
        "redirect_uri": "http://localhost:1455/auth/callback",
        "scope": "openid profile email offline_access",
        "originator": "codex_cli_rs",
    })
    monkeypatch.setattr(oauth_module.secrets, "token_urlsafe", lambda n: "state-1" if n == 32 else "verifier-1")

    auth_url = oauth_module.begin_login("work")
    parsed = urlparse(auth_url)
    params = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.openai.com"
    assert parsed.path == "/oauth/authorize"
    assert params["redirect_uri"] == ["http://localhost:1455/auth/callback"]
    assert params["id_token_add_organizations"] == ["true"]
    assert params["codex_cli_simplified_flow"] == ["true"]
    assert params["originator"] == ["codex_cli_rs"]


def test_login_rejects_bad_callback_state(runner, monkeypatch):
    monkeypatch.setattr(oauth_module, "load_oauth_config", lambda: {
        "client_id": "client-123",
        "redirect_uri": "http://localhost:1455/auth/callback",
        "scope": "openid profile email offline_access",
        "originator": "codex_cli_rs",
    })
    monkeypatch.setattr(oauth_module.secrets, "token_urlsafe", lambda n: "expected-state" if n == 32 else "verifier-1")

    result = runner.invoke(
        cli,
        ["login", "work"],
        input="http://127.0.0.1:1455/callback?code=abc123&state=wrong-state\n",
    )

    assert result.exit_code != 0
    assert "Callback state did not match" in result.output
    assert store_module.list_profiles() == []


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


def test_use_prompts_on_unsafe_reconciliation(runner, monkeypatch):
    monkeypatch.setattr(cli_module, "get_sync_dir", lambda: None)
    local = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "not-a-jwt",
            "access_token": "stored-access",
            "refresh_token": "refresh",
        },
    }
    auth = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "also-not-a-jwt",
            "access_token": "auth-access",
            "refresh_token": "refresh",
        },
    }
    store_module.save_profile("work", local)
    store_module.save_profile("other", local)
    store_module.set_active("work")
    store_module.save_codex_auth(auth)

    result = runner.invoke(cli, ["use", "other"], input="auth\n")

    assert result.exit_code == 0
    assert "Choose which copy should win for 'work'" in result.output
    assert "Activated" in result.output
    assert store_module.load_profile("work")["tokens"]["access_token"] == "auth-access"


def test_use_offers_push_after_reconciliation_when_sync_configured(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    profile = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _jwt({"iss": "https://auth.example", "sub": "user-1"}),
            "access_token": "stored-access",
            "refresh_token": "refresh",
            "account_id": "acct-1",
        },
        "last_refresh": "2025-01-01T00:00:00+00:00",
    }
    store_module.save_profile("work", profile)
    store_module.save_profile("other", profile)
    store_module.set_active("work")
    auth = json.loads(json.dumps(profile))
    auth["tokens"]["access_token"] = "auth-access"
    store_module.save_codex_auth(auth)

    pushed = []

    def fake_push(sync_path):
        pushed.append(sync_path)

    monkeypatch.setattr(cli_module, "_push_sync_changes", fake_push)

    result = runner.invoke(cli, ["use", "other"], input="y\n")

    assert result.exit_code == 0
    assert "##### Successfully reconciled local store. #####" in result.output
    assert "Push these changes now?" in result.output
    assert pushed == [sync_dir]


def test_use_push_prompt_reprompts_on_empty_or_q(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    profile = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _jwt({"iss": "https://auth.example", "sub": "user-1"}),
            "access_token": "stored-access",
            "refresh_token": "refresh",
            "account_id": "acct-1",
        },
        "last_refresh": "2025-01-01T00:00:00+00:00",
    }
    store_module.save_profile("work", profile)
    store_module.save_profile("other", profile)
    store_module.set_active("work")
    auth = json.loads(json.dumps(profile))
    auth["tokens"]["access_token"] = "auth-access"
    store_module.save_codex_auth(auth)

    pushed = []

    def fake_push(sync_path):
        pushed.append(sync_path)

    monkeypatch.setattr(cli_module, "_push_sync_changes", fake_push)

    result = runner.invoke(cli, ["use", "other"], input="\nq\ny\n")

    assert result.exit_code == 0
    assert result.output.count("Reconciliation updated local store. Push these changes now? [y/N]:") == 3
    assert pushed == [sync_dir]


def test_use_push_prompt_accepts_uppercase_and_yes_no_variants(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    profile = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _jwt({"iss": "https://auth.example", "sub": "user-1"}),
            "access_token": "stored-access",
            "refresh_token": "refresh",
            "account_id": "acct-1",
        },
        "last_refresh": "2025-01-01T00:00:00+00:00",
    }
    store_module.save_profile("work", profile)
    store_module.save_profile("other", profile)
    store_module.set_active("work")
    auth = json.loads(json.dumps(profile))
    auth["tokens"]["access_token"] = "auth-access"
    store_module.save_codex_auth(auth)

    pushed = []

    def fake_push(sync_path):
        pushed.append(sync_path)

    monkeypatch.setattr(cli_module, "_push_sync_changes", fake_push)

    result = runner.invoke(cli, ["use", "other"], input="YES\n")

    assert result.exit_code == 0
    assert pushed == [sync_dir]


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


def test_list_shows_profiles(runner, saved_profile, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 13, 15, 4, 5, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(cli_module, "datetime", FrozenDateTime)

    result = runner.invoke(cli, ["list", "--no-usage", "--no-interactive"])

    assert result.exit_code == 0
    assert "2026-03-13" in result.output
    assert "work" in result.output


def test_hide_excludes_profile_from_default_list(runner, saved_profile, sample_profile):
    personal = dict(sample_profile)
    personal["tokens"] = dict(sample_profile["tokens"])
    personal["tokens"]["account_id"] = "personal-account-id"
    store_module.save_profile("personal", personal)

    hide_result = runner.invoke(cli, ["hide", "work"])
    list_result = runner.invoke(cli, ["list", "--no-usage", "--no-interactive"])

    assert hide_result.exit_code == 0
    assert "Hidden profile work" in hide_result.output
    assert list_result.exit_code == 0
    assert "personal" in list_result.output
    assert "work" not in list_result.output
    assert store_module.list_profiles() == ["personal", "work"]


def test_list_all_includes_hidden_profile(runner, saved_profile):
    store_module.hide_profile("work")

    result = runner.invoke(cli, ["list", "--all", "--no-usage", "--no-interactive"])

    assert result.exit_code == 0
    assert "work" in result.output
    assert "hidden" in result.output


def test_unhide_restores_profile_to_default_list(runner, saved_profile):
    store_module.hide_profile("work")

    unhide_result = runner.invoke(cli, ["unhide", "work"])
    list_result = runner.invoke(cli, ["list", "--no-usage", "--no-interactive"])

    assert unhide_result.exit_code == 0
    assert "Unhidden profile work" in unhide_result.output
    assert list_result.exit_code == 0
    assert "work" in list_result.output


def test_list_when_all_profiles_hidden_points_to_all_flag(runner, saved_profile):
    store_module.hide_profile("work")

    result = runner.invoke(cli, ["list", "--no-usage", "--no-interactive"])

    assert result.exit_code == 0
    assert "All profiles are hidden" in result.output
    assert "list --all" in result.output


def test_use_hidden_profile_still_activates(runner, saved_profile):
    store_module.hide_profile("work")

    result = runner.invoke(cli, ["use", "work"])

    assert result.exit_code == 0
    assert store_module.get_active() == "work"


def test_remove_clears_hidden_entry(runner, saved_profile):
    store_module.hide_profile("work")

    result = runner.invoke(cli, ["remove", "work"])

    assert result.exit_code == 0
    assert store_module.list_hidden_profiles() == set()


def test_list_places_table_immediately_after_timestamp(runner, saved_profile, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 13, 15, 4, 5, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(cli_module, "datetime", FrozenDateTime)

    result = runner.invoke(cli, ["list", "--no-usage", "--no-interactive"], terminal_width=140)

    assert result.exit_code == 0
    lines = result.output.splitlines()
    timestamp_index = next(i for i, line in enumerate(lines) if "2026-03-13" in line)
    assert lines[timestamp_index + 1].strip()


def test_list_shows_usage_reset_columns(runner, saved_profile, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 13, 15, 4, 5, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    async def fake_fetch_all_usage(profiles):
        return usage_module.UsageFetchSummary(
            usage_map={
                "work": cli_module.UsageResult(
                    primary_pct=74,
                    secondary_pct=38,
                    primary_reset_at=datetime(2026, 3, 13, 19, 16, 5, tzinfo=timezone.utc),
                    secondary_reset_at=datetime(2026, 3, 15, 18, 4, 5, tzinfo=timezone.utc),
                )
            },
            refreshed_profiles=[],
        )

    monkeypatch.setattr(cli_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(cli_module, "fetch_all_usage", fake_fetch_all_usage)

    result = runner.invoke(cli, ["list", "--no-interactive"], terminal_width=140)

    assert result.exit_code == 0
    assert "5h Left" in result.output
    assert "74%" in result.output
    assert "38%" in result.output
    assert "4h 12m" in result.output
    assert "2d 3h" in result.output


def test_list_uses_narrow_layout_on_small_terminal(runner, saved_profile, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 13, 15, 4, 5, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    async def fake_fetch_all_usage(profiles):
        return usage_module.UsageFetchSummary(
            usage_map={
                "work": cli_module.UsageResult(
                    primary_pct=74,
                    secondary_pct=38,
                    primary_reset_at=datetime(2026, 3, 13, 19, 16, 5, tzinfo=timezone.utc),
                    secondary_reset_at=datetime(2026, 3, 15, 18, 4, 5, tzinfo=timezone.utc),
                )
            },
            refreshed_profiles=[],
        )

    monkeypatch.setattr(cli_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(cli_module, "fetch_all_usage", fake_fetch_all_usage)

    result = runner.invoke(cli, ["list", "--no-interactive"], terminal_width=50)

    assert result.exit_code == 0
    assert "1. work" in result.output
    assert "chatgpt" in result.output
    assert "5h     ████░  74%/4h 12m" in result.output
    assert "wk" in result.output
    assert "wk     ██░░░  38%/2d 3h" in result.output
    assert "4h 12m" in result.output
    assert "2d 3h" in result.output


def test_list_uses_compact_table_on_medium_terminal(runner, saved_profile, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 3, 13, 15, 4, 5, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    async def fake_fetch_all_usage(profiles):
        return usage_module.UsageFetchSummary(
            usage_map={
                "work": cli_module.UsageResult(
                    primary_pct=74,
                    secondary_pct=38,
                    primary_reset_at=datetime(2026, 3, 13, 19, 16, 5, tzinfo=timezone.utc),
                    secondary_reset_at=datetime(2026, 3, 15, 18, 4, 5, tzinfo=timezone.utc),
                )
            },
            refreshed_profiles=[],
        )

    monkeypatch.setattr(cli_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(display_module, "datetime", FrozenDateTime)
    monkeypatch.setattr(cli_module, "fetch_all_usage", fake_fetch_all_usage)

    result = runner.invoke(cli, ["list", "--no-interactive"], terminal_width=90)

    assert result.exit_code == 0
    assert "Name" in result.output
    assert "Md" in result.output
    assert "5h L" in result.output
    assert "Wk L" in result.output
    assert "work" in result.output
    assert "4h 12m" in result.output
    assert "2d 3h" in result.output


def test_list_no_usage_reconciles_active_profile(runner, monkeypatch):
    monkeypatch.setattr(cli_module, "get_sync_dir", lambda: None)
    profile = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _jwt({"iss": "https://auth.example", "sub": "user-1"}),
            "access_token": "stored-access",
            "refresh_token": "refresh",
            "account_id": "acct-1",
        },
        "last_refresh": "2025-01-01T00:00:00+00:00",
    }
    store_module.save_profile("work", profile)
    store_module.set_active("work")
    auth = json.loads(json.dumps(profile))
    auth["tokens"]["access_token"] = "auth-access"
    store_module.save_codex_auth(auth)

    result = runner.invoke(cli, ["list", "--no-usage", "--no-interactive"])

    assert result.exit_code == 0
    assert "Reconciled active profile 'work'" in result.output
    assert store_module.load_profile("work")["tokens"]["access_token"] == "auth-access"


def test_list_no_interactive_skips_push_prompt_after_reconciliation(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    profile = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _jwt({"iss": "https://auth.example", "sub": "user-1"}),
            "access_token": "stored-access",
            "refresh_token": "refresh",
            "account_id": "acct-1",
        },
        "last_refresh": "2025-01-01T00:00:00+00:00",
    }
    store_module.save_profile("work", profile)
    store_module.set_active("work")
    auth = json.loads(json.dumps(profile))
    auth["tokens"]["access_token"] = "auth-access"
    store_module.save_codex_auth(auth)

    monkeypatch.setattr(cli_module.click, "confirm", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("confirm should not be called")))

    result = runner.invoke(cli, ["list", "--no-usage", "--no-interactive"])

    assert result.exit_code == 0


def test_list_prompts_to_push_after_refresh(runner, saved_profile, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    prompts = []
    push_calls = []

    async def fake_fetch_all_usage(profiles):
        return usage_module.UsageFetchSummary(
            usage_map={"work": cli_module.UsageResult(error="n/a")},
            refreshed_profiles=["work"],
        )

    def fake_confirm(prompt):
        prompts.append(prompt)
        return False

    def fake_push(sync_dir_arg):
        push_calls.append(sync_dir_arg)

    monkeypatch.setattr(cli_module, "fetch_all_usage", fake_fetch_all_usage)
    monkeypatch.setattr(cli_module, "_confirm_yes_no", fake_confirm)
    monkeypatch.setattr(cli_module, "_push_sync_changes", fake_push)
    monkeypatch.setattr(cli_module, "interactive_prompt", lambda profiles: None)

    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0
    assert "Refreshed stale stored tokens" in result.output
    assert prompts == ["Refreshed stored tokens for profile work. Push these changes now? [y/N]: "]
    assert push_calls == []


def test_list_no_interactive_skips_push_prompt_after_refresh(runner, saved_profile, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    async def fake_fetch_all_usage(profiles):
        return usage_module.UsageFetchSummary(
            usage_map={"work": cli_module.UsageResult(error="n/a")},
            refreshed_profiles=["work"],
        )

    monkeypatch.setattr(cli_module, "fetch_all_usage", fake_fetch_all_usage)
    monkeypatch.setattr(
        cli_module,
        "_confirm_yes_no",
        lambda prompt: (_ for _ in ()).throw(AssertionError("_confirm_yes_no should not be called")),
    )

    result = runner.invoke(cli, ["list", "--no-interactive"])

    assert result.exit_code == 0
    assert "Refreshed stale stored tokens" not in result.output


def test_list_prompts_only_once_when_reconcile_and_refresh_both_update_store(
    runner,
    monkeypatch,
    tmp_path,
):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    profile = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _jwt({"iss": "https://auth.example", "sub": "user-1"}),
            "access_token": "stored-access",
            "refresh_token": "refresh",
            "account_id": "acct-1",
        },
        "last_refresh": "2025-01-01T00:00:00+00:00",
    }
    store_module.save_profile("work", profile)
    store_module.set_active("work")
    auth = json.loads(json.dumps(profile))
    auth["tokens"]["access_token"] = "auth-access"
    store_module.save_codex_auth(auth)

    prompts = []

    async def fake_fetch_all_usage(profiles):
        return usage_module.UsageFetchSummary(
            usage_map={"work": cli_module.UsageResult(error="n/a")},
            refreshed_profiles=["work"],
        )

    monkeypatch.setattr(cli_module, "fetch_all_usage", fake_fetch_all_usage)
    monkeypatch.setattr(cli_module, "interactive_prompt", lambda profiles: None)
    monkeypatch.setattr(cli_module, "_push_sync_changes", lambda sync_dir_arg: None)
    monkeypatch.setattr(cli_module, "_confirm_yes_no", lambda prompt: prompts.append(prompt) or False)

    result = runner.invoke(cli, ["list"])

    assert result.exit_code == 0
    assert "Reconciled active profile 'work'" in result.output
    assert "Local store updated during list" in result.output
    assert prompts == ["List updated local store. Push these changes now? [y/N]: "]


def test_import_requires_sync_dir(runner, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["import"])

    assert result.exit_code != 0
    assert "CODEXAUTH_SYNC_DIR" in result.output


def test_export_requires_sync_dir(runner, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["export"])

    assert result.exit_code != 0
    assert "CODEXAUTH_SYNC_DIR" in result.output


def test_import_imports_all_profiles_by_default(runner, sample_profile, monkeypatch, tmp_path):
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

    result = runner.invoke(cli, ["import"])

    assert result.exit_code == 0
    assert "Imported profile work" in result.output
    assert "Imported profile personal" in result.output
    assert store_module.list_profiles() == ["personal", "work"]
    imported_path = store_module.TOKENS_DIR / "personal.json"
    assert int(imported_path.stat().st_mtime) == 1_700_000_000


def test_import_newer_external_overwrites_without_prompt(
    runner, sample_profile, monkeypatch, tmp_path
):
    external_timestamp = 1_700_000_000
    local_timestamp = 1_600_000_000
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    external = dict(sample_profile)
    external["tokens"] = dict(sample_profile["tokens"])
    external["tokens"]["account_id"] = "external-account-id"
    external_path = sync_dir / "work.json"
    external_path.write_text(json.dumps(external))
    os.utime(external_path, (external_timestamp, external_timestamp))

    store_module.save_profile("work", sample_profile)
    local_path = store_module.TOKENS_DIR / "work.json"
    os.utime(local_path, (local_timestamp, local_timestamp))

    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["import"])

    assert result.exit_code == 0
    assert "Import profile 'work' from external modified" not in result.output
    assert "Imported profile work" in result.output
    assert store_module.load_profile("work")["tokens"]["account_id"] == "external-account-id"


def test_import_removes_blacklisted_local_profile(runner, saved_profile, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / ".gitignore").write_text("work.json\n")
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["import"])

    assert result.exit_code == 0
    assert "Removed blacklisted profile work" in result.output
    assert store_module.list_profiles() == []


def test_import_blacklist_clears_active_marker(runner, saved_profile, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / ".gitignore").write_text("/work.json\n")
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)
    store_module.set_active("work")

    result = runner.invoke(cli, ["import"])

    assert result.exit_code == 0
    assert "Removed blacklisted profile work" in result.output
    assert store_module.get_active() is None


def test_import_skips_blacklisted_sync_file(runner, sample_profile, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / "work.json").write_text(json.dumps(sample_profile))
    (sync_dir / ".gitignore").write_text("work.json\n")
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["import"])

    assert result.exit_code == 0
    assert "Imported profile work" not in result.output
    assert store_module.list_profiles() == []


def test_import_older_external_shows_timestamps_and_can_skip(
    runner, sample_profile, monkeypatch, tmp_path
):
    external_timestamp = 1_600_000_000
    local_timestamp = 1_700_000_000
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    external = dict(sample_profile)
    external["tokens"] = dict(sample_profile["tokens"])
    external["tokens"]["account_id"] = "external-account-id"
    external_path = sync_dir / "work.json"
    external_path.write_text(json.dumps(external))
    os.utime(external_path, (external_timestamp, external_timestamp))

    store_module.save_profile("work", sample_profile)
    local_path = store_module.TOKENS_DIR / "work.json"
    os.utime(local_path, (local_timestamp, local_timestamp))

    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["import"], input="n\n")

    assert result.exit_code == 0
    assert "Import profile 'work' from external modified" in result.output
    assert "over local modified" in result.output
    assert format_modified(datetime.fromtimestamp(external_timestamp)) in result.output
    assert format_modified(datetime.fromtimestamp(local_timestamp)) in result.output
    assert "No profiles imported" in result.output
    assert store_module.load_profile("work")["tokens"]["account_id"] == "fake-account-id"


def test_export_exports_all_profiles_by_default(
    runner, saved_profile, monkeypatch, tmp_path
):
    sync_dir = tmp_path / "sync"
    local_path = store_module.TOKENS_DIR / "work.json"
    os.utime(local_path, (1_700_000_000, 1_700_000_000))
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["export"])

    assert result.exit_code == 0
    assert "Exported profile work" in result.output
    exported = sync_dir / "work.json"
    assert exported.exists()
    assert json.loads(exported.read_text())["auth_mode"] == "chatgpt"
    assert stat.S_IMODE(exported.stat().st_mode) == 0o600
    assert int(exported.stat().st_mtime) == 1_700_000_000


def test_export_includes_hidden_profiles(runner, saved_profile, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    store_module.hide_profile("work")
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["export"])

    assert result.exit_code == 0
    assert "Exported profile work" in result.output
    assert (sync_dir / "work.json").exists()


def test_export_newer_local_overwrites_without_prompt(
    runner, saved_profile, monkeypatch, tmp_path
):
    external_timestamp = 1_600_000_000
    local_timestamp = 1_700_000_000
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    existing = sync_dir / "work.json"
    existing.write_text(json.dumps({"auth_mode": "old"}))
    os.utime(existing, (external_timestamp, external_timestamp))

    local_path = store_module.TOKENS_DIR / "work.json"
    os.utime(local_path, (local_timestamp, local_timestamp))

    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["export"])

    assert result.exit_code == 0
    assert "Export profile 'work' from local modified" not in result.output
    assert "Exported profile work" in result.output
    assert json.loads(existing.read_text())["auth_mode"] == "chatgpt"


def test_export_older_local_shows_timestamps_and_can_confirm(
    runner, saved_profile, monkeypatch, tmp_path
):
    external_timestamp = 1_700_000_000
    local_timestamp = 1_600_000_000
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    existing = sync_dir / "work.json"
    existing.write_text(json.dumps({"auth_mode": "old"}))
    os.utime(existing, (external_timestamp, external_timestamp))

    local_path = store_module.TOKENS_DIR / "work.json"
    os.utime(local_path, (local_timestamp, local_timestamp))

    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["export"], input="y\n")

    assert result.exit_code == 0
    assert "Export profile 'work' from local modified" in result.output
    assert "over external modified" in result.output
    assert format_modified(datetime.fromtimestamp(local_timestamp)) in result.output
    assert format_modified(datetime.fromtimestamp(external_timestamp)) in result.output
    assert json.loads(existing.read_text())["auth_mode"] == "chatgpt"


def test_pull_requires_git_repo(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["pull"])

    assert result.exit_code != 0
    assert "rev-parse --is-inside-work-tree failed" in result.output


def test_pull_requires_sync_dir(runner, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["pull"])

    assert result.exit_code != 0
    assert "CODEXAUTH_SYNC_DIR" in result.output


def test_pull_requires_existing_sync_dir(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "missing-sync"
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["pull"])

    assert result.exit_code != 0
    assert f"Sync directory does not exist: {sync_dir}" in result.output


def test_pull_success(runner, sample_profile, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    profile_path = sync_dir / "work.json"
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)
    profile_path.write_text(json.dumps(sample_profile))

    calls = []

    def fake_run(cmd, cwd, capture_output, text, check):
        calls.append((cmd, cwd))
        if cmd == ["git", "rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
        if cmd == ["git", "pull", "--no-rebase", "--no-edit"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="Already up to date.\n", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(git_sync_module.subprocess, "run", fake_run)

    result = runner.invoke(cli, ["pull"])

    assert result.exit_code == 0
    assert "Pulled sync repo" in result.output
    assert "Already up to date." in result.output
    assert "Imported profile work" in result.output
    assert store_module.list_profiles() == ["work"]
    assert calls == [
        (["git", "rev-parse", "--is-inside-work-tree"], sync_dir),
        (["git", "pull", "--no-rebase", "--no-edit"], sync_dir),
    ]


def test_pull_reconciles_active_profile_before_git_pull(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    profile = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _jwt({"iss": "https://auth.example", "sub": "user-1"}),
            "access_token": "stored-access",
            "refresh_token": "refresh",
            "account_id": "acct-1",
        },
        "last_refresh": "2025-01-01T00:00:00+00:00",
    }
    store_module.save_profile("work", profile)
    store_module.set_active("work")
    auth = json.loads(json.dumps(profile))
    auth["tokens"]["access_token"] = "auth-access"
    store_module.save_codex_auth(auth)

    def fake_run(cmd, cwd, capture_output, text, check):
        if cmd == ["git", "rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
        if cmd == ["git", "pull", "--no-rebase", "--no-edit"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="Already up to date.\n", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(git_sync_module.subprocess, "run", fake_run)

    result = runner.invoke(cli, ["pull"], input="n\n")

    assert result.exit_code == 0
    assert "Reconciled active profile 'work'" in result.output
    assert store_module.load_profile("work")["tokens"]["access_token"] == "auth-access"


def test_pull_updates_local_auth_from_newer_imported_active_profile(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    profile = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _jwt({"iss": "https://auth.example", "sub": "user-1"}),
            "access_token": "old-access",
            "refresh_token": "refresh",
            "account_id": "acct-1",
        },
        "last_refresh": "2025-01-01T00:00:00+00:00",
    }
    store_module.save_profile("work", profile)
    os.utime(store_module.TOKENS_DIR / "work.json", (1_600_000_000, 1_600_000_000))
    store_module.set_active("work")
    store_module.save_codex_auth(profile)
    os.utime(store_module.CODEX_AUTH, (1_600_000_000, 1_600_000_000))

    imported = json.loads(json.dumps(profile))
    imported["tokens"]["access_token"] = "new-access"
    imported_path = sync_dir / "work.json"
    imported_path.write_text(json.dumps(imported))
    os.utime(imported_path, (1_700_000_000, 1_700_000_000))

    def fake_run(cmd, cwd, capture_output, text, check):
        if cmd == ["git", "rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
        if cmd == ["git", "pull", "--no-rebase", "--no-edit"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="Fetched.\n", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(git_sync_module.subprocess, "run", fake_run)

    result = runner.invoke(cli, ["pull"])

    assert result.exit_code == 0
    assert "Updated ~/.codex/auth.json from imported active profile 'work'." in result.output
    assert json.loads(store_module.CODEX_AUTH.read_text())["tokens"]["access_token"] == "new-access"


def test_pull_removes_blacklisted_local_profile(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / ".gitignore").write_text("work.json\n")
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)
    store_module.save_profile("work", {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "fake.id.token",
            "access_token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "account_id": "fake-account-id",
        },
        "last_refresh": "2025-01-01T00:00:00+00:00",
    })

    def fake_run(cmd, cwd, capture_output, text, check):
        if cmd == ["git", "rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
        if cmd == ["git", "pull", "--no-rebase", "--no-edit"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="Already up to date.\n", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(git_sync_module.subprocess, "run", fake_run)

    result = runner.invoke(cli, ["pull"])

    assert result.exit_code == 0
    assert "Removed blacklisted profile work" in result.output
    assert store_module.list_profiles() == []


def test_pull_failure_surfaces_git_error(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)

    def fake_run(cmd, cwd, capture_output, text, check):
        if cmd == ["git", "rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")
        if cmd == ["git", "pull", "--no-rebase", "--no-edit"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="merge conflict\n")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(git_sync_module.subprocess, "run", fake_run)

    result = runner.invoke(cli, ["pull"])

    assert result.exit_code != 0
    assert "git pull --no-rebase --no-edit failed" in result.output
    assert "merge conflict" in result.output


def test_push_requires_sync_dir(runner, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["push"])

    assert result.exit_code != 0
    assert "CODEXAUTH_SYNC_DIR" in result.output


def test_push_no_changes_is_success(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)
    store_module.save_profile("work", {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "fake.id.token",
            "access_token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "account_id": "fake-account-id",
        },
        "last_refresh": "2025-01-01T00:00:00+00:00",
    })

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
    assert "Exported profile work" in result.output
    assert "No changes to commit." in result.output
    assert "Pushed sync repo" not in result.output
    assert (sync_dir / "work.json").exists()


def test_push_success(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)
    store_module.save_profile("work", {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "fake.id.token",
            "access_token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "account_id": "fake-account-id",
        },
        "last_refresh": "2025-01-01T00:00:00+00:00",
    })

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
        if cmd == ["git", "pull", "--no-rebase", "--no-edit"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="Already up to date.\n", stderr="")
        if cmd == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="pushed\n", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(git_sync_module.subprocess, "run", fake_run)

    result = runner.invoke(cli, ["push"])

    assert result.exit_code == 0
    assert "Exported profile work" in result.output
    assert "Pushed sync repo" in result.output
    assert "pushed" in result.output
    assert (sync_dir / "work.json").exists()
    assert calls == [
        (["git", "rev-parse", "--is-inside-work-tree"], sync_dir),
        (["git", "add", "."], sync_dir),
        (["git", "diff", "--cached", "--quiet"], sync_dir),
        (["git", "commit", "-m", "Update exported codexauth profiles"], sync_dir),
        (["git", "pull", "--no-rebase", "--no-edit"], sync_dir),
        (["git", "push"], sync_dir),
    ]


def test_push_commit_failure_stops_before_push(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)
    store_module.save_profile("work", {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "fake.id.token",
            "access_token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "account_id": "fake-account-id",
        },
        "last_refresh": "2025-01-01T00:00:00+00:00",
    })

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
    assert "Exported profile work" in result.output
    assert "git commit -m Update exported codexauth profiles failed" in result.output
    assert "missing user.email" in result.output
    assert (["git", "push"], sync_dir) not in calls


def test_push_failure_surfaces_git_error(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)
    store_module.save_profile("work", {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "fake.id.token",
            "access_token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "account_id": "fake-account-id",
        },
        "last_refresh": "2025-01-01T00:00:00+00:00",
    })

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
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="[main abc123] Update exported codexauth profiles\n",
                stderr="",
            )
        if cmd == ["git", "pull", "--no-rebase", "--no-edit"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="Merge made by the 'ort' strategy.\n", stderr="")
        if cmd == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="non-fast-forward\n")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(git_sync_module.subprocess, "run", fake_run)

    result = runner.invoke(cli, ["push"])

    assert result.exit_code != 0
    assert "Exported profile work" in result.output
    assert "git push failed" in result.output
    assert "non-fast-forward" in result.output
    assert (["git", "pull", "--no-rebase", "--no-edit"], sync_dir) in calls
    assert (["git", "push"], sync_dir) in calls


def test_push_pull_failure_surfaces_git_error(runner, monkeypatch, tmp_path):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (tmp_path / ".env").write_text(f"CODEXAUTH_SYNC_DIR={sync_dir}\n")
    monkeypatch.chdir(tmp_path)
    store_module.save_profile("work", {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": "fake.id.token",
            "access_token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "account_id": "fake-account-id",
        },
        "last_refresh": "2025-01-01T00:00:00+00:00",
    })

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
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="[main abc123] Update exported codexauth profiles\n",
                stderr="",
            )
        if cmd == ["git", "pull", "--no-rebase", "--no-edit"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="merge conflict\n")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(git_sync_module.subprocess, "run", fake_run)

    result = runner.invoke(cli, ["push"])

    assert result.exit_code != 0
    assert "Exported profile work" in result.output
    assert "git pull --no-rebase --no-edit failed" in result.output
    assert "merge conflict" in result.output
    assert (["git", "push"], sync_dir) not in calls
