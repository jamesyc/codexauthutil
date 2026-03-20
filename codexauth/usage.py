"""Fetch Codex quota usage from the OpenAI API."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from codexauth.refresh import needs_refresh, refresh_tokens
from codexauth.store import save_profile

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


@dataclass
class UsageWindow:
    key: str
    used_pct: float | None = None
    reset_at: datetime | None = None
    label: str | None = None
    short_label: str | None = None


class UsageResult:
    def __init__(
        self,
        primary_pct=None,
        secondary_pct=None,
        primary_reset_at=None,
        secondary_reset_at=None,
        windows=None,
        error=None,
    ):
        resolved_windows = dict(windows or {})
        if "primary_window" not in resolved_windows and (
            primary_pct is not None or primary_reset_at is not None
        ):
            resolved_windows["primary_window"] = UsageWindow(
                key="primary_window",
                used_pct=primary_pct,
                reset_at=primary_reset_at,
            )
        if "secondary_window" not in resolved_windows and (
            secondary_pct is not None or secondary_reset_at is not None
        ):
            resolved_windows["secondary_window"] = UsageWindow(
                key="secondary_window",
                used_pct=secondary_pct,
                reset_at=secondary_reset_at,
            )
        self.windows = resolved_windows
        self.error = error                # None | "expired" | "n/a"

    @property
    def primary_pct(self):
        return self.windows.get("primary_window", UsageWindow("primary_window")).used_pct

    @property
    def secondary_pct(self):
        return self.windows.get("secondary_window", UsageWindow("secondary_window")).used_pct

    @property
    def primary_reset_at(self):
        return self.windows.get("primary_window", UsageWindow("primary_window")).reset_at

    @property
    def secondary_reset_at(self):
        return self.windows.get("secondary_window", UsageWindow("secondary_window")).reset_at


@dataclass
class UsageFetchSummary:
    usage_map: dict[str, UsageResult]
    refreshed_profiles: list[str]


def _parse_reset_at(value):
    if value is None:
        return None
    try:
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_usage_windows(rate_limit: dict) -> dict[str, UsageWindow]:
    windows: dict[str, UsageWindow] = {}
    for key, value in rate_limit.items():
        if not key.endswith("_window") or not isinstance(value, dict):
            continue
        windows[key] = UsageWindow(
            key=key,
            used_pct=value.get("used_percent"),
            reset_at=_parse_reset_at(value.get("reset_at")),
        )
    return windows


def _slugify_label(value: str) -> str:
    chars = []
    for ch in value.lower():
        if ch.isalnum():
            chars.append(ch)
        elif chars and chars[-1] != "_":
            chars.append("_")
    return "".join(chars).strip("_") or "additional"


def _parse_additional_rate_limits(items) -> dict[str, UsageWindow]:
    windows: dict[str, UsageWindow] = {}
    if not isinstance(items, list):
        return windows
    for item in items:
        if not isinstance(item, dict):
            continue
        limit_name = item.get("limit_name")
        rate_limit = item.get("rate_limit")
        if not limit_name or not isinstance(rate_limit, dict):
            continue

        label_base = str(limit_name)
        short_label = label_base[:3]
        prefix = _slugify_label(label_base)
        for key, value in rate_limit.items():
            if not key.endswith("_window") or not isinstance(value, dict):
                continue
            window_label = label_base if key == "primary_window" else f"{label_base} Weekly"
            window_short_label = short_label if key == "primary_window" else f"{short_label} W"
            window_key = f"additional_{prefix}_{key}"
            windows[window_key] = UsageWindow(
                key=window_key,
                used_pct=value.get("used_percent"),
                reset_at=_parse_reset_at(value.get("reset_at")),
                label=window_label,
                short_label=window_short_label,
            )
    return windows


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
        windows = _parse_usage_windows(rl)
        windows.update(_parse_additional_rate_limits(data.get("additional_rate_limits", [])))
        return (
            name,
            UsageResult(windows=windows),
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
