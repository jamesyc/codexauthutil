"""Fetch Codex quota usage from the OpenAI API."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from codexauth.refresh import needs_refresh, refresh_tokens
from codexauth.store import save_profile

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DEFAULT_USAGE_CONCURRENCY = 8
SHORT_WINDOW_SECONDS = 5 * 60 * 60
WEEKLY_WINDOW_SECONDS = 7 * 24 * 60 * 60


@dataclass
class UsageWindow:
    key: str
    used_pct: float | None = None
    reset_at: datetime | None = None
    label: str | None = None
    short_label: str | None = None
    limit_window_seconds: int | None = None


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


def _parse_limit_window_seconds(value):
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _canonical_window_key(limit_window_seconds: int | None) -> str | None:
    if limit_window_seconds == SHORT_WINDOW_SECONDS:
        return "primary_window"
    if limit_window_seconds == WEEKLY_WINDOW_SECONDS:
        return "secondary_window"
    return None


def _copy_window(window: UsageWindow, key: str) -> UsageWindow:
    return UsageWindow(
        key=key,
        used_pct=window.used_pct,
        reset_at=window.reset_at,
        label=window.label,
        short_label=window.short_label,
        limit_window_seconds=window.limit_window_seconds,
    )


def _extra_window_key(base_key: str, windows: dict[str, UsageWindow]) -> str:
    candidate = f"extra_{base_key}"
    if candidate not in windows:
        return candidate

    index = 2
    while f"{candidate}_{index}" in windows:
        index += 1
    return f"{candidate}_{index}"


def _normalize_standard_windows(raw_windows: dict[str, UsageWindow]) -> dict[str, UsageWindow]:
    windows: dict[str, UsageWindow] = {}

    for fallback_key in ("primary_window", "secondary_window"):
        window = raw_windows.get(fallback_key)
        if window is None:
            continue

        canonical_key = _canonical_window_key(window.limit_window_seconds)
        if canonical_key is not None:
            target_key = canonical_key
        elif window.limit_window_seconds is None:
            target_key = fallback_key
        else:
            target_key = _extra_window_key(window.key, windows)

        if target_key not in windows:
            windows[target_key] = _copy_window(window, target_key)
            continue

        extra_key = _extra_window_key(window.key, windows)
        windows[extra_key] = _copy_window(window, extra_key)

    for raw_key, window in raw_windows.items():
        if raw_key in ("primary_window", "secondary_window"):
            continue
        target_key = raw_key if raw_key not in windows else _extra_window_key(raw_key, windows)
        windows[target_key] = _copy_window(window, target_key)

    return windows


def _parse_usage_windows(rate_limit: dict) -> dict[str, UsageWindow]:
    raw_windows: dict[str, UsageWindow] = {}
    for key, value in rate_limit.items():
        if not key.endswith("_window") or not isinstance(value, dict):
            continue
        raw_windows[key] = UsageWindow(
            key=key,
            used_pct=value.get("used_percent"),
            reset_at=_parse_reset_at(value.get("reset_at")),
            limit_window_seconds=_parse_limit_window_seconds(value.get("limit_window_seconds")),
        )
    return _normalize_standard_windows(raw_windows)


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
        raw_windows: dict[str, UsageWindow] = {}
        for key, value in rate_limit.items():
            if not key.endswith("_window") or not isinstance(value, dict):
                continue
            raw_windows[key] = UsageWindow(
                key=key,
                used_pct=value.get("used_percent"),
                reset_at=_parse_reset_at(value.get("reset_at")),
                label=label_base if key == "primary_window" else f"{label_base} Weekly",
                short_label=short_label if key == "primary_window" else f"{short_label} W",
                limit_window_seconds=_parse_limit_window_seconds(value.get("limit_window_seconds")),
            )

        for normalized_key, window in _normalize_standard_windows(raw_windows).items():
            semantic_key = normalized_key.removeprefix("extra_")
            window_label = label_base if semantic_key == "primary_window" else f"{label_base} Weekly"
            window_short_label = short_label if semantic_key == "primary_window" else f"{short_label} W"
            window_key = f"additional_{prefix}_{normalized_key}"
            windows[window_key] = UsageWindow(
                key=window_key,
                used_pct=window.used_pct,
                reset_at=window.reset_at,
                label=window_label,
                short_label=window_short_label,
                limit_window_seconds=window.limit_window_seconds,
            )
    return windows


async def fetch_usage(
    name: str,
    profile: dict,
    *,
    usage_client: httpx.AsyncClient | None = None,
    refresh_client: httpx.AsyncClient | None = None,
) -> tuple[str, UsageResult, bool]:
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
        profile = await refresh_tokens(profile, client=refresh_client)
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
        if usage_client is None:
            async with httpx.AsyncClient(timeout=15) as owned_client:
                resp = await owned_client.get(USAGE_URL, headers=headers)
        else:
            resp = await usage_client.get(USAGE_URL, headers=headers)
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


async def _fetch_usage_with_limit(
    semaphore: asyncio.Semaphore,
    name: str,
    profile: dict,
    *,
    usage_client: httpx.AsyncClient,
    refresh_client: httpx.AsyncClient,
) -> tuple[str, UsageResult, bool]:
    async with semaphore:
        return await fetch_usage(
            name,
            profile,
            usage_client=usage_client,
            refresh_client=refresh_client,
        )


async def fetch_all_usage(
    profiles: dict[str, dict],
    *,
    max_concurrency: int = DEFAULT_USAGE_CONCURRENCY,
) -> UsageFetchSummary:
    """Fetch usage for all profiles concurrently."""
    if not profiles:
        return UsageFetchSummary(usage_map={}, refreshed_profiles=[])

    concurrency = max(1, min(max_concurrency, len(profiles)))
    semaphore = asyncio.Semaphore(concurrency)

    async with (
        httpx.AsyncClient(timeout=15) as usage_client,
        httpx.AsyncClient(timeout=30) as refresh_client,
    ):
        results = await asyncio.gather(
            *[
                _fetch_usage_with_limit(
                    semaphore,
                    name,
                    profile,
                    usage_client=usage_client,
                    refresh_client=refresh_client,
                )
                for name, profile in profiles.items()
            ]
        )
    return UsageFetchSummary(
        usage_map={name: usage for name, usage, _ in results},
        refreshed_profiles=[name for name, _, refreshed in results if refreshed],
    )
