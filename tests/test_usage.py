"""Tests for codexauth.usage."""

import asyncio
import json
import os
from datetime import datetime, timezone

import pytest
import respx
import httpx

import codexauth.usage as usage_module
import codexauth.store as store_module
from codexauth.usage import fetch_usage, fetch_all_usage, UsageFetchSummary, UsageResult, USAGE_URL
from codexauth.usage import _parse_usage_windows, _parse_additional_rate_limits

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

    name, result, refreshed = await fetch_usage("work", FRESH_PROFILE)

    assert name == "work"
    assert result.primary_pct == 45
    assert result.secondary_pct == 74
    assert result.primary_reset_at == datetime.fromtimestamp(9999999999, tz=timezone.utc)
    assert result.secondary_reset_at == datetime.fromtimestamp(9999999999, tz=timezone.utc)
    assert result.error is None
    assert refreshed is False


@pytest.mark.asyncio
@respx.mock
async def test_fetch_usage_parses_spark_window():
    respx.get(USAGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {"used_percent": 45, "reset_at": 9999999999},
                    "secondary_window": {"used_percent": 74, "reset_at": 9999999999},
                },
                "additional_rate_limits": [
                    {
                        "limit_name": "GPT-5.3-Codex-Spark",
                        "rate_limit": {
                            "primary_window": {"used_percent": 12, "reset_at": 9999999000},
                            "secondary_window": {"used_percent": 9, "reset_at": 9999998000},
                        },
                    }
                ],
            },
        )
    )

    _, result, refreshed = await fetch_usage("work", FRESH_PROFILE)

    assert result.windows["additional_gpt_5_3_codex_spark_primary_window"].label == "GPT-5.3-Codex-Spark"
    assert result.windows["additional_gpt_5_3_codex_spark_primary_window"].used_pct == 12
    assert result.windows["additional_gpt_5_3_codex_spark_primary_window"].reset_at == datetime.fromtimestamp(9999999000, tz=timezone.utc)
    assert result.windows["additional_gpt_5_3_codex_spark_secondary_window"].label == "GPT-5.3-Codex-Spark Weekly"
    assert result.windows["additional_gpt_5_3_codex_spark_secondary_window"].used_pct == 9
    assert refreshed is False


def test_parse_usage_windows_uses_duration_to_classify_standard_windows():
    result = _parse_usage_windows(
        {
            "primary_window": {"used_percent": 11, "reset_at": 9999999000, "limit_window_seconds": 18000},
            "secondary_window": {"used_percent": 22, "reset_at": 9999998000, "limit_window_seconds": 604800},
        }
    )

    assert set(result) == {"primary_window", "secondary_window"}
    assert result["primary_window"].used_pct == 11
    assert result["primary_window"].limit_window_seconds == 18000
    assert result["secondary_window"].used_pct == 22
    assert result["secondary_window"].limit_window_seconds == 604800


def test_parse_usage_windows_moves_weekly_primary_window_into_weekly_bucket():
    result = _parse_usage_windows(
        {
            "primary_window": {"used_percent": 100, "reset_at": 1774679953, "limit_window_seconds": 604800},
            "secondary_window": None,
        }
    )

    assert "primary_window" not in result
    assert result["secondary_window"].used_pct == 100
    assert result["secondary_window"].limit_window_seconds == 604800


def test_parse_usage_windows_keeps_legacy_primary_secondary_mapping_without_duration():
    result = _parse_usage_windows(
        {
            "primary_window": {"used_percent": 33, "reset_at": 9999999000},
            "secondary_window": {"used_percent": 44, "reset_at": 9999998000},
        }
    )

    assert set(result) == {"primary_window", "secondary_window"}
    assert result["primary_window"].used_pct == 33
    assert result["secondary_window"].used_pct == 44


def test_parse_usage_windows_preserves_unknown_duration_as_extra_column():
    result = _parse_usage_windows(
        {
            "primary_window": {"used_percent": 55, "reset_at": 9999999000, "limit_window_seconds": 86400},
            "secondary_window": None,
        }
    )

    assert "primary_window" not in result
    assert result["extra_primary_window"].used_pct == 55
    assert result["extra_primary_window"].limit_window_seconds == 86400


def test_parse_usage_windows_preserves_duplicate_recognized_duration_as_extra_column():
    result = _parse_usage_windows(
        {
            "primary_window": {"used_percent": 10, "reset_at": 9999999000, "limit_window_seconds": 18000},
            "secondary_window": {"used_percent": 20, "reset_at": 9999998000, "limit_window_seconds": 18000},
        }
    )

    assert result["primary_window"].used_pct == 10
    assert result["extra_secondary_window"].used_pct == 20


def test_parse_usage_windows_ignores_invalid_shapes_and_values():
    result = _parse_usage_windows(
        {
            "primary_window": {"used_percent": 66, "reset_at": 9999999000, "limit_window_seconds": "invalid"},
            "secondary_window": "not-a-dict",
            "tertiary_window": {"used_percent": 77, "reset_at": 9999998000, "limit_window_seconds": 0},
        }
    )

    assert result["primary_window"].limit_window_seconds is None
    assert result["tertiary_window"].limit_window_seconds is None
    assert "secondary_window" not in result


def test_parse_additional_rate_limits_uses_duration_to_classify_named_windows():
    result = _parse_additional_rate_limits(
        [
            {
                "limit_name": "GPT-5.3-Codex-Spark",
                "rate_limit": {
                    "primary_window": {"used_percent": 12, "reset_at": 9999999000, "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 9, "reset_at": 9999998000, "limit_window_seconds": 604800},
                },
            }
        ]
    )

    assert result["additional_gpt_5_3_codex_spark_primary_window"].label == "GPT-5.3-Codex-Spark"
    assert result["additional_gpt_5_3_codex_spark_secondary_window"].label == "GPT-5.3-Codex-Spark Weekly"


def test_parse_additional_rate_limits_moves_weekly_primary_window_into_named_weekly_bucket():
    result = _parse_additional_rate_limits(
        [
            {
                "limit_name": "GPT-5.3-Codex-Spark",
                "rate_limit": {
                    "primary_window": {"used_percent": 9, "reset_at": 9999998000, "limit_window_seconds": 604800},
                    "secondary_window": None,
                },
            }
        ]
    )

    assert "additional_gpt_5_3_codex_spark_primary_window" not in result
    assert result["additional_gpt_5_3_codex_spark_secondary_window"].label == "GPT-5.3-Codex-Spark Weekly"
    assert result["additional_gpt_5_3_codex_spark_secondary_window"].used_pct == 9


def test_parse_additional_rate_limits_keeps_legacy_named_mapping_without_duration():
    result = _parse_additional_rate_limits(
        [
            {
                "limit_name": "GPT-5.3-Codex-Spark",
                "rate_limit": {
                    "primary_window": {"used_percent": 12, "reset_at": 9999999000},
                    "secondary_window": {"used_percent": 9, "reset_at": 9999998000},
                },
            }
        ]
    )

    assert "additional_gpt_5_3_codex_spark_primary_window" in result
    assert "additional_gpt_5_3_codex_spark_secondary_window" in result


def test_parse_additional_rate_limits_preserves_unknown_duration_as_extra_column():
    result = _parse_additional_rate_limits(
        [
            {
                "limit_name": "GPT-5.3-Codex-Spark",
                "rate_limit": {
                    "primary_window": {"used_percent": 12, "reset_at": 9999999000, "limit_window_seconds": 86400},
                },
            }
        ]
    )

    assert "additional_gpt_5_3_codex_spark_primary_window" not in result
    assert result["additional_gpt_5_3_codex_spark_extra_primary_window"].used_pct == 12


def test_parse_additional_rate_limits_ignores_invalid_items():
    result = _parse_additional_rate_limits(
        [
            None,
            {"limit_name": "", "rate_limit": {}},
            {"limit_name": "Spark"},
            {"limit_name": "Spark", "rate_limit": "not-a-dict"},
            {
                "limit_name": "Spark",
                "rate_limit": {
                    "primary_window": {"used_percent": 1, "reset_at": 9999999000},
                },
            },
        ]
    )

    assert set(result) == {"additional_spark_primary_window"}


@pytest.mark.asyncio
@respx.mock
async def test_fetch_usage_handles_null_additional_rate_limits():
    respx.get(USAGE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {"used_percent": 11, "reset_at": 9999999999},
                    "secondary_window": {"used_percent": 9, "reset_at": 9999999000},
                },
                "additional_rate_limits": None,
            },
        )
    )

    _, result, refreshed = await fetch_usage("work", FRESH_PROFILE)

    assert result.primary_pct == 11
    assert result.secondary_pct == 9
    assert result.windows.keys() == {"primary_window", "secondary_window"}
    assert refreshed is False


@pytest.mark.asyncio
@respx.mock
async def test_fetch_usage_expired():
    respx.get(USAGE_URL).mock(return_value=httpx.Response(401))

    _, result, refreshed = await fetch_usage("work", FRESH_PROFILE)
    assert result.error == "expired"
    assert refreshed is False


@pytest.mark.asyncio
@respx.mock
async def test_fetch_usage_server_error():
    respx.get(USAGE_URL).mock(return_value=httpx.Response(500))

    _, result, refreshed = await fetch_usage("work", FRESH_PROFILE)
    assert result.error == "n/a"
    assert refreshed is False


@pytest.mark.asyncio
async def test_fetch_usage_api_key_mode():
    profile = {"auth_mode": "api_key", "OPENAI_API_KEY": "sk-test"}
    _, result, refreshed = await fetch_usage("apikey", profile)
    assert result.error == "n/a"
    assert refreshed is False


@pytest.mark.asyncio
@respx.mock
async def test_fetch_all_usage():
    respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json=USAGE_RESPONSE))

    profiles = {"work": FRESH_PROFILE, "personal": FRESH_PROFILE}
    results = await fetch_all_usage(profiles)

    assert isinstance(results, UsageFetchSummary)
    assert set(results.usage_map.keys()) == {"work", "personal"}
    assert results.usage_map["work"].primary_pct == 45
    assert results.refreshed_profiles == []


@pytest.mark.asyncio
async def test_fetch_usage_refresh_updates_stored_mtime(monkeypatch, sample_profile):
    store_module.save_profile("work", sample_profile)
    stored_path = store_module.TOKENS_DIR / "work.json"
    os.utime(stored_path, (1_600_000_000, 1_600_000_000))

    stale_profile = dict(sample_profile)
    stale_profile["tokens"] = dict(sample_profile["tokens"])
    stale_profile["last_refresh"] = "2000-01-01T00:00:00+00:00"

    async def fake_refresh(profile, client=None):
        refreshed = dict(profile)
        refreshed["tokens"] = dict(profile["tokens"])
        refreshed["tokens"]["access_token"] = "new-access-token"
        refreshed["last_refresh"] = "2026-03-12T00:00:00+00:00"
        return refreshed

    class DummyResponse:
        status_code = 200

        @staticmethod
        def json():
            return USAGE_RESPONSE

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers):
            return DummyResponse()

    monkeypatch.setattr(usage_module, "needs_refresh", lambda profile: True)
    monkeypatch.setattr(usage_module, "refresh_tokens", fake_refresh)
    monkeypatch.setattr(httpx, "AsyncClient", lambda timeout=15: DummyClient())

    _, result, refreshed = await fetch_usage("work", stale_profile)

    assert result.error is None
    assert refreshed is True
    assert stored_path.stat().st_mtime > 1_600_000_000
    saved = json.loads(stored_path.read_text())
    assert saved["tokens"]["access_token"] == "new-access-token"


@pytest.mark.asyncio
async def test_fetch_all_usage_reuses_clients_and_limits_concurrency(monkeypatch):
    profiles = {f"profile-{i}": FRESH_PROFILE for i in range(5)}
    client_instances = []
    usage_client_ids = set()
    refresh_client_ids = set()
    active = 0
    max_active = 0

    class DummyClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            client_instances.append(self)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    async def fake_fetch_usage(name, profile, *, usage_client=None, refresh_client=None):
        nonlocal active, max_active
        usage_client_ids.add(id(usage_client))
        refresh_client_ids.add(id(refresh_client))
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return name, UsageResult(primary_pct=45), False

    monkeypatch.setattr(usage_module.httpx, "AsyncClient", DummyClient)
    monkeypatch.setattr(usage_module, "fetch_usage", fake_fetch_usage)

    results = await fetch_all_usage(profiles, max_concurrency=2)

    assert isinstance(results, UsageFetchSummary)
    assert set(results.usage_map) == set(profiles)
    assert len(client_instances) == 2
    assert {client.timeout for client in client_instances} == {15, 30}
    assert len(usage_client_ids) == 1
    assert len(refresh_client_ids) == 1
    assert max_active == 2


@pytest.mark.asyncio
async def test_fetch_all_usage_empty_profiles():
    results = await fetch_all_usage({})

    assert results.usage_map == {}
    assert results.refreshed_profiles == []
