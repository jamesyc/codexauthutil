"""Fetch Codex quota usage from the OpenAI API."""

import asyncio
import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from codexauth.refresh import needs_refresh, refresh_tokens
from codexauth.store import save_profile

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
RESET_CREDITS_TIMEOUT_SECONDS = 5
DEFAULT_USAGE_CONCURRENCY = 8
SHORT_WINDOW_SECONDS = 5 * 60 * 60
WEEKLY_WINDOW_SECONDS = 7 * 24 * 60 * 60

# ChatGPT's authenticated usage response currently identifies the two Pro
# subscriptions as ``prolite`` ($100) and ``pro`` ($200). Expressing their
# allowances relative to Plus makes percentages comparable across profiles.
PLAN_MULTIPLIERS = {
    "plus": 1,
    "prolite": 5,
    "pro": 20,
}


@dataclass
class UsageWindow:
    key: str
    used_pct: float | None = None
    reset_at: datetime | None = None
    label: str | None = None
    short_label: str | None = None
    limit_window_seconds: int | None = None
    reset_after_seconds: int | None = None


@dataclass
class UsageResetCredit:
    id: str
    expires_at: datetime | None = None
    granted_at: datetime | None = None
    title: str | None = None
    reset_type: str | None = None


@dataclass
class UsageCredits:
    has_credits: bool
    unlimited: bool
    balance: str | None = None


class UsageResult:
    def __init__(
        self,
        primary_pct=None,
        secondary_pct=None,
        primary_reset_at=None,
        secondary_reset_at=None,
        windows=None,
        reset_count=None,
        reset_credits=None,
        credits=None,
        error=None,
        plan_type=None,
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
        self.reset_count = reset_count
        self.reset_credits = reset_credits
        self.credits = credits
        self.plan_type = plan_type
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

    @property
    def plan_multiplier(self) -> int | None:
        if not isinstance(self.plan_type, str):
            return None
        return PLAN_MULTIPLIERS.get(self.plan_type.lower())

    @property
    def weekly_plus_equivalent_left(self) -> float | None:
        multiplier = self.plan_multiplier
        weekly_pct = self.secondary_pct
        if multiplier is None or not isinstance(weekly_pct, (int, float)):
            return None
        remaining_fraction = (100 - min(100, max(0, weekly_pct))) / 100
        return multiplier * remaining_fraction


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


def _decode_jwt_payload(token: str) -> dict:
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _plan_type_from_profile(profile: dict) -> str | None:
    tokens = profile.get("tokens", {})
    if not isinstance(tokens, dict):
        return None
    for token_key in ("access_token", "id_token"):
        claims = _decode_jwt_payload(tokens.get(token_key, ""))
        auth_claims = claims.get("https://api.openai.com/auth", {})
        if not isinstance(auth_claims, dict):
            continue
        plan_type = auth_claims.get("chatgpt_plan_type")
        if isinstance(plan_type, str) and plan_type.strip():
            return plan_type.strip().lower()
    return None


def _parse_plan_type(value, profile: dict) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return _plan_type_from_profile(profile)


def _parse_rfc3339(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _parse_available_count(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _parse_reset_credit_summary(value) -> int | None:
    if not isinstance(value, dict):
        return None
    return _parse_available_count(value.get("available_count"))


def _parse_credits(value) -> UsageCredits | None:
    if not isinstance(value, dict):
        return None
    has_credits = value.get("has_credits")
    unlimited = value.get("unlimited")
    balance = value.get("balance")
    if not isinstance(has_credits, bool) or not isinstance(unlimited, bool):
        return None
    if balance is not None and not isinstance(balance, str):
        return None
    return UsageCredits(
        has_credits=has_credits,
        unlimited=unlimited,
        balance=balance,
    )


def _parse_reset_credit_details(value) -> tuple[int, list[UsageResetCredit]] | None:
    if not isinstance(value, dict) or not isinstance(value.get("credits"), list):
        return None

    available_count = _parse_available_count(value.get("available_count"))
    if available_count is None:
        return None

    credits: list[UsageResetCredit] = []
    for item in value["credits"]:
        if not isinstance(item, dict) or item.get("status") != "available":
            continue

        credit_id = item.get("id")
        granted_at = _parse_rfc3339(item.get("granted_at"))
        if not isinstance(credit_id, str) or not credit_id or granted_at is None:
            return None

        raw_expires_at = item.get("expires_at")
        expires_at = _parse_rfc3339(raw_expires_at)
        if raw_expires_at is not None and expires_at is None:
            return None

        title = item.get("title")
        reset_type = item.get("reset_type")
        credits.append(
            UsageResetCredit(
                id=credit_id,
                expires_at=expires_at,
                granted_at=granted_at,
                title=title if isinstance(title, str) else None,
                reset_type=reset_type if isinstance(reset_type, str) else None,
            )
        )

    credits.sort(
        key=lambda credit: credit.expires_at or datetime.max.replace(tzinfo=timezone.utc)
    )
    return available_count, credits


def _parse_limit_window_seconds(value):
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _parse_reset_after_seconds(value):
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


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
        reset_after_seconds=window.reset_after_seconds,
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
            reset_after_seconds=_parse_reset_after_seconds(value.get("reset_after_seconds")),
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
                reset_after_seconds=_parse_reset_after_seconds(value.get("reset_after_seconds")),
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
                reset_after_seconds=window.reset_after_seconds,
            )
    return windows


async def _fetch_usage_and_reset_credits(
    client: httpx.AsyncClient,
    headers: dict[str, str],
) -> tuple[httpx.Response, tuple[int, list[UsageResetCredit]] | None]:
    async def fetch_reset_credits():
        try:
            response = await asyncio.wait_for(
                client.get(RESET_CREDITS_URL, headers=headers),
                timeout=RESET_CREDITS_TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                return None
            return _parse_reset_credit_details(response.json())
        except Exception:
            return None

    usage_response, reset_credit_details = await asyncio.gather(
        client.get(USAGE_URL, headers=headers),
        fetch_reset_credits(),
    )
    return usage_response, reset_credit_details


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
                resp, reset_credit_details = await _fetch_usage_and_reset_credits(
                    owned_client, headers
                )
        else:
            resp, reset_credit_details = await _fetch_usage_and_reset_credits(
                usage_client, headers
            )
        if resp.status_code in (401, 403):
            return name, UsageResult(error="expired"), refreshed
        if resp.status_code != 200:
            return name, UsageResult(error="n/a"), refreshed
        data = resp.json()
        rl = data.get("rate_limit", {})
        windows = _parse_usage_windows(rl)
        windows.update(_parse_additional_rate_limits(data.get("additional_rate_limits", [])))
        reset_count = _parse_reset_credit_summary(data.get("rate_limit_reset_credits"))
        credits = _parse_credits(data.get("credits"))
        reset_credits = None
        if reset_credit_details is not None:
            reset_count, reset_credits = reset_credit_details
        return (
            name,
            UsageResult(
                windows=windows,
                reset_count=reset_count,
                reset_credits=reset_credits,
                credits=credits,
                plan_type=_parse_plan_type(data.get("plan_type"), profile),
            ),
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
