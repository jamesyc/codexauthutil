"""Tests for codexauth.usage."""

import pytest
import respx
import httpx

from codexauth.usage import fetch_usage, fetch_all_usage, UsageResult, USAGE_URL

FRESH_PROFILE = {
    "auth_mode": "chatgpt",
    "tokens": {
        "access_token": "fake-access",
        "account_id": "fake-account-id",
    },
    "last_refresh": "2099-01-01T00:00:00+00:00",  # far future → no refresh needed
}

USAGE_RESPONSE = {
    "plan_type": "plus",
    "rate_limit": {
        "primary_window": {"used_percent": 45, "reset_at": 9999999999, "limit_window_seconds": 18000},
        "secondary_window": {"used_percent": 74, "reset_at": 9999999999, "limit_window_seconds": 604800},
    },
}


@pytest.mark.asyncio
@respx.mock
async def test_fetch_usage_success():
    respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json=USAGE_RESPONSE))

    name, result = await fetch_usage("work", FRESH_PROFILE)

    assert name == "work"
    assert result.primary_pct == 45
    assert result.secondary_pct == 74
    assert result.error is None


@pytest.mark.asyncio
@respx.mock
async def test_fetch_usage_expired():
    respx.get(USAGE_URL).mock(return_value=httpx.Response(401))

    _, result = await fetch_usage("work", FRESH_PROFILE)
    assert result.error == "expired"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_usage_server_error():
    respx.get(USAGE_URL).mock(return_value=httpx.Response(500))

    _, result = await fetch_usage("work", FRESH_PROFILE)
    assert result.error == "n/a"


@pytest.mark.asyncio
async def test_fetch_usage_api_key_mode():
    profile = {"auth_mode": "api_key", "OPENAI_API_KEY": "sk-test"}
    _, result = await fetch_usage("apikey", profile)
    assert result.error == "n/a"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_all_usage():
    respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json=USAGE_RESPONSE))

    profiles = {"work": FRESH_PROFILE, "personal": FRESH_PROFILE}
    results = await fetch_all_usage(profiles)

    assert set(results.keys()) == {"work", "personal"}
    assert results["work"].primary_pct == 45
