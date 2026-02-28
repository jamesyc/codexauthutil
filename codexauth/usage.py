"""Fetch Codex quota usage from the OpenAI API."""

import asyncio

import httpx

from codexauth.refresh import needs_refresh, refresh_tokens
from codexauth.store import save_profile

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


class UsageResult:
    def __init__(self, primary_pct=None, secondary_pct=None, error=None):
        self.primary_pct = primary_pct    # float | None
        self.secondary_pct = secondary_pct
        self.error = error                # None | "expired" | "n/a"


async def fetch_usage(name: str, profile: dict) -> tuple[str, UsageResult]:
    """Fetch usage for a single profile. Returns (name, UsageResult)."""
    if profile.get("auth_mode") != "chatgpt":
        return name, UsageResult(error="n/a")

    tokens = profile.get("tokens", {})
    access_token = tokens.get("access_token", "")
    account_id = tokens.get("account_id", "")

    if not access_token:
        return name, UsageResult(error="n/a")

    if needs_refresh(profile):
        profile = await refresh_tokens(profile)
        save_profile(name, profile)
        access_token = profile.get("tokens", {}).get("access_token", access_token)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(USAGE_URL, headers=headers)
        if resp.status_code in (401, 403):
            return name, UsageResult(error="expired")
        if resp.status_code != 200:
            return name, UsageResult(error="n/a")
        data = resp.json()
        rl = data.get("rate_limit", {})
        primary = rl.get("primary_window", {}).get("used_percent")
        secondary = rl.get("secondary_window", {}).get("used_percent")
        return name, UsageResult(primary_pct=primary, secondary_pct=secondary)
    except Exception:
        return name, UsageResult(error="n/a")


async def fetch_all_usage(profiles: dict[str, dict]) -> dict[str, UsageResult]:
    """Fetch usage for all profiles concurrently."""
    results = await asyncio.gather(*[fetch_usage(n, d) for n, d in profiles.items()])
    return dict(results)
