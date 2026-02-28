"""Token refresh logic for ChatGPT OAuth credentials."""

from datetime import datetime, timezone

import httpx

REFRESH_URL = "https://auth.openai.com/oauth/token"
REFRESH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REFRESH_MAX_AGE_DAYS = 8


def needs_refresh(profile: dict) -> bool:
    last_str = profile.get("last_refresh")
    if not last_str:
        return True
    try:
        last = datetime.fromisoformat(last_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last).days >= REFRESH_MAX_AGE_DAYS
    except (ValueError, TypeError):
        return True


async def refresh_tokens(profile: dict) -> dict:
    """Return an updated profile dict with refreshed tokens, or the original on failure."""
    tokens = profile.get("tokens", {})
    refresh_token = tokens.get("refresh_token", "")
    if not refresh_token:
        return profile
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(REFRESH_URL, json={
                "client_id": REFRESH_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": "openid profile email",
            })
        if resp.status_code != 200:
            return profile
        data = resp.json()
        new_tokens = dict(tokens)
        for key in ("access_token", "refresh_token", "id_token"):
            if key in data:
                new_tokens[key] = data[key]
        return {
            **profile,
            "tokens": new_tokens,
            "last_refresh": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        return profile
