"""Fetch Codex quota usage from the OpenAI API."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from codexauth.refresh import needs_refresh, refresh_tokens
from codexauth.store import save_profile

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


class UsageResult:
    def __init__(
        self,
        primary_pct=None,
        secondary_pct=None,
        primary_reset_at=None,
        secondary_reset_at=None,
        error=None,
    ):
        self.primary_pct = primary_pct    # float | None
        self.secondary_pct = secondary_pct
        self.primary_reset_at = primary_reset_at
        self.secondary_reset_at = secondary_reset_at
        self.error = error                # None | "expired" | "n/a"


@dataclass
class UsageFetchSummary:
    usage_map: dict[str, UsageResult]
    refreshed_profiles: list[str]


def _parse_reset_at(value):
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


async def fetch_usage(name: str, profile: dict) -> tuple[str, UsageResult, bool]:
    """Fetch usage for a single profile. Returns (name, UsageResult, refreshed)."""
    if profile.get("auth_mode") != "chatgpt":
        return name, UsageResult(error="n/a"), False

    tokens = profile.get("tokens", {})
    access_token = tokens.get("access_token", "")
    account_id = tokens.get("account_id", "")

    if not access_token:
        return name, UsageResult(error="n/a"), False

    refreshed = False
    if needs_refresh(profile):
        previous_profile = profile
        profile = await refresh_tokens(profile)
        if profile != previous_profile:
            save_profile(name, profile)
            refreshed = True
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
            return name, UsageResult(error="expired"), refreshed
        if resp.status_code != 200:
            return name, UsageResult(error="n/a"), refreshed
        data = resp.json()
        rl = data.get("rate_limit", {})
        primary_window = rl.get("primary_window", {})
        secondary_window = rl.get("secondary_window", {})
        primary = primary_window.get("used_percent")
        secondary = secondary_window.get("used_percent")
        return (
            name,
            UsageResult(
                primary_pct=primary,
                secondary_pct=secondary,
                primary_reset_at=_parse_reset_at(primary_window.get("reset_at")),
                secondary_reset_at=_parse_reset_at(secondary_window.get("reset_at")),
            ),
            refreshed,
        )
    except Exception:
        return name, UsageResult(error="n/a"), refreshed


async def fetch_all_usage(profiles: dict[str, dict]) -> UsageFetchSummary:
    """Fetch usage for all profiles concurrently."""
    results = await asyncio.gather(*[fetch_usage(n, d) for n, d in profiles.items()])
    return UsageFetchSummary(
        usage_map={name: usage for name, usage, _ in results},
        refreshed_profiles=[name for name, _, refreshed in results if refreshed],
    )
