"""Tests for codexauth.refresh."""

from datetime import datetime, timedelta, timezone

import pytest
import respx
import httpx

from codexauth.refresh import needs_refresh, refresh_tokens, REFRESH_URL


def _profile_with_refresh(days_ago: int) -> dict:
    last = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "id_token": "old-id",
        },
        "last_refresh": last.isoformat(),
    }


def test_needs_refresh_fresh():
    assert needs_refresh(_profile_with_refresh(days_ago=0)) is False


def test_needs_refresh_stale():
    assert needs_refresh(_profile_with_refresh(days_ago=8)) is True


def test_needs_refresh_no_timestamp():
    assert needs_refresh({"tokens": {}}) is True


def test_needs_refresh_bad_timestamp():
    assert needs_refresh({"last_refresh": "not-a-date"}) is True


@pytest.mark.asyncio
@respx.mock
async def test_refresh_tokens_success():
    respx.post(REFRESH_URL).mock(return_value=httpx.Response(200, json={
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "id_token": "new-id",
    }))

    profile = _profile_with_refresh(days_ago=10)
    result = await refresh_tokens(profile)

    assert result["tokens"]["access_token"] == "new-access"
    assert result["tokens"]["refresh_token"] == "new-refresh"
    assert "last_refresh" in result


@pytest.mark.asyncio
@respx.mock
async def test_refresh_tokens_failure_returns_original():
    respx.post(REFRESH_URL).mock(return_value=httpx.Response(401))

    profile = _profile_with_refresh(days_ago=10)
    result = await refresh_tokens(profile)

    assert result["tokens"]["access_token"] == "old-access"


@pytest.mark.asyncio
async def test_refresh_tokens_no_refresh_token():
    profile = {"auth_mode": "chatgpt", "tokens": {}}
    result = await refresh_tokens(profile)
    assert result == profile
