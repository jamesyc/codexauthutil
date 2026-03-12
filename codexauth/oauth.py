"""Manual OAuth bootstrap helpers for ChatGPT-backed profiles."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from codexauth.config import load_dotenv
from codexauth.store import STORE_DIR

AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_SCOPE = "openid profile email offline_access"
DEFAULT_REDIRECT_URI = "http://localhost:1455/auth/callback"
DEFAULT_ORIGINATOR = "codex_cli_rs"
PENDING_LOGIN_MAX_AGE_MINUTES = 10

OAUTH_SCOPE_ENV = "CODEXAUTH_OAUTH_SCOPE"
OAUTH_ORIGINATOR_ENV = "CODEXAUTH_OAUTH_ORIGINATOR"


class OAuthError(Exception):
    """Raised when the manual OAuth flow cannot continue safely."""


def _pending_login_path() -> Path:
    return STORE_DIR / "pending-login.json"


def _ensure_store_dir() -> None:
    STORE_DIR.mkdir(mode=0o700, exist_ok=True)


def _urlsafe_b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def load_oauth_config() -> dict[str, str]:
    """Load OAuth settings, keeping the working client and redirect fixed."""
    dotenv = load_dotenv()
    scope = os.environ.get(OAUTH_SCOPE_ENV) or dotenv.get(OAUTH_SCOPE_ENV) or DEFAULT_SCOPE
    originator = (
        os.environ.get(OAUTH_ORIGINATOR_ENV)
        or dotenv.get(OAUTH_ORIGINATOR_ENV)
        or DEFAULT_ORIGINATOR
    )

    return {
        "client_id": DEFAULT_CLIENT_ID,
        "redirect_uri": DEFAULT_REDIRECT_URI,
        "scope": scope,
        "originator": originator,
    }


def begin_login(name: str | None = None) -> str:
    """Create pending login state and return the authorization URL."""
    config = load_oauth_config()
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _urlsafe_b64(hashlib.sha256(code_verifier.encode("ascii")).digest())

    pending = {
        "name": name,
        "state": state,
        "code_verifier": code_verifier,
        "redirect_uri": config["redirect_uri"],
        "created_at": _utcnow().isoformat(),
    }

    _ensure_store_dir()
    pending_path = _pending_login_path()
    pending_path.write_text(json.dumps(pending, indent=2))
    pending_path.chmod(0o600)

    query = urlencode(
        {
            "client_id": config["client_id"],
            "redirect_uri": config["redirect_uri"],
            "response_type": "code",
            "scope": config["scope"],
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": config["originator"],
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _load_pending_login() -> dict:
    pending_path = _pending_login_path()
    if not pending_path.exists():
        raise OAuthError("No pending login found. Start again with `codexauth login`.")

    pending = json.loads(pending_path.read_text())
    created_at = pending.get("created_at")
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        clear_pending_login()
        raise OAuthError("Pending login state is invalid. Start again with `codexauth login`.")

    if _utcnow() - created > timedelta(minutes=PENDING_LOGIN_MAX_AGE_MINUTES):
        clear_pending_login()
        raise OAuthError("Pending login state expired. Start again with `codexauth login`.")

    return pending


def clear_pending_login() -> None:
    _pending_login_path().unlink(missing_ok=True)


def parse_callback(callback_url: str) -> tuple[str, dict]:
    """Validate the callback URL and return (code, pending_state)."""
    pending = _load_pending_login()
    parsed = urlparse(callback_url.strip())
    params = parse_qs(parsed.query)

    if "error" in params:
        detail = params.get("error_description", params["error"])[0]
        raise OAuthError(f"OAuth provider returned an error: {detail}")

    code = params.get("code", [None])[0]
    returned_state = params.get("state", [None])[0]
    if not code or not returned_state:
        raise OAuthError("Callback URL must include both code and state query parameters.")
    if returned_state != pending.get("state"):
        raise OAuthError("Callback state did not match the pending login. Start again with `codexauth login`.")

    return code, pending


async def exchange_code(callback_url: str) -> dict:
    """Exchange the callback code for tokens and map them into auth.json shape."""
    code, pending = parse_callback(callback_url)
    config = load_oauth_config()

    payload = {
        "client_id": config["client_id"],
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending["redirect_uri"],
        "code_verifier": pending["code_verifier"],
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(TOKEN_URL, json=payload)
    except Exception as exc:
        raise OAuthError(f"Token exchange failed: {exc}") from exc

    if resp.status_code != 200:
        detail = resp.text.strip() or f"HTTP {resp.status_code}"
        raise OAuthError(f"Token exchange failed: {detail}")

    data = resp.json()
    tokens = {
        "access_token": data["access_token"],
    }
    for key in ("refresh_token", "id_token", "account_id"):
        if key in data and data[key]:
            tokens[key] = data[key]

    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": tokens,
        "last_refresh": _utcnow().isoformat(),
    }
