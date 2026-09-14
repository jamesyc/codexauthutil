"""Credential ordering uses identity and embedded dates, never copy timestamps."""

import base64
import copy
import json

import pytest

from codexauth.credentials import compare_credentials, credential_summary


def _jwt(**claims):
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{encoded}.signature"


def _profile(issued, refreshed):
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "account_id": "acct-1",
            "access_token": _jwt(iat=issued),
            "id_token": _jwt(iat=issued, iss="issuer", sub="user-1"),
            "refresh_token": f"refresh-secret-{issued}",
        },
        "last_refresh": refreshed,
    }


@pytest.fixture
def pair():
    return (
        _profile(1_735_689_600, "2025-01-01T00:00:00Z"),
        _profile(1_735_776_000, "2025-01-02T00:00:00Z"),
    )


@pytest.mark.parametrize("evidence", ["all", "access_token", "id_token", "last_refresh"])
def test_comparison_uses_available_credential_dates_in_both_directions(pair, evidence):
    older, newer = pair
    if evidence != "all":
        for profile in pair:
            if evidence != "last_refresh":
                profile.pop("last_refresh")
            for key in ("access_token", "id_token"):
                if key != evidence:
                    profile["tokens"][key] = f"opaque-{key}"

    assert compare_credentials(newer, older).winner == "source"
    assert compare_credentials(older, newer).winner == "destination"


def test_identical_profiles_need_no_identity_or_dates():
    profile = {"auth_mode": "api_key", "OPENAI_API_KEY": "key-secret"}
    assert compare_credentials(profile, dict(profile)).winner == "identical"


@pytest.mark.parametrize("change", ["account", "subject", "embedded_account", "auth_mode", "missing_identity"])
def test_different_or_unknown_identity_never_merges_automatically(pair, change):
    older, newer = pair
    if change == "account":
        newer["tokens"]["account_id"] = "different-account"
    elif change == "subject":
        newer["tokens"]["id_token"] = _jwt(iat=1_735_776_000, iss="issuer", sub="other-user")
    elif change == "embedded_account":
        newer["tokens"]["access_token"] = _jwt(**{
            "iat": 1_735_776_000,
            "https://api.openai.com/auth": {"chatgpt_account_id": "other-account"},
        })
    elif change == "auth_mode":
        newer["auth_mode"] = "api_key"
    else:
        newer["tokens"].pop("account_id")
        newer["tokens"]["id_token"] = "opaque"
    assert compare_credentials(newer, older).winner == "ambiguous"
    assert compare_credentials(older, newer).winner == "ambiguous"


def test_identity_can_be_confirmed_from_id_token_subject(pair):
    older, newer = pair
    for profile in pair:
        profile["tokens"].pop("account_id")
    assert compare_credentials(newer, older).winner == "source"


def test_identity_can_be_confirmed_from_embedded_account_claim(pair):
    older, newer = pair
    for profile, issued in zip(pair, [1_735_689_600, 1_735_776_000]):
        profile["tokens"].pop("account_id")
        profile["tokens"].pop("id_token")
        profile["tokens"]["access_token"] = _jwt(**{
            "iat": issued,
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1"},
        })
    assert compare_credentials(newer, older).winner == "source"


@pytest.mark.parametrize("conflicting_field", ["last_refresh", "id_token"])
def test_disagreeing_credential_timestamps_require_a_choice(pair, conflicting_field):
    older, newer = pair
    if conflicting_field == "last_refresh":
        newer["last_refresh"] = "2024-12-31T00:00:00Z"
    else:
        newer["tokens"]["id_token"] = _jwt(iat=1_735_603_200, iss="issuer", sub="user-1")
    comparison = compare_credentials(newer, older)
    assert comparison.winner == "ambiguous"
    assert "conflicting freshness" in comparison.reason


def test_missing_dates_on_one_side_do_not_make_it_older(pair):
    older, newer = pair
    older.pop("last_refresh")
    older["tokens"]["access_token"] = "opaque-access"
    older["tokens"]["id_token"] = "opaque-id"
    assert compare_credentials(newer, older).winner == "ambiguous"


def test_equal_dates_with_different_refresh_tokens_are_ambiguous(pair):
    older, _ = pair
    other = copy.deepcopy(older)
    other["tokens"]["refresh_token"] = "other-secret"
    assert compare_credentials(other, older).winner == "ambiguous"


@pytest.mark.parametrize("invalid", [None, "invalid", "2025-01-01T00:00:00", 123, True])
def test_invalid_refresh_dates_can_fall_back_to_token_issue_times(pair, invalid):
    older, newer = pair
    older["last_refresh"] = invalid
    assert compare_credentials(newer, older).winner == "source"


@pytest.mark.parametrize("invalid", [None, True, "1735776000", float("nan"), float("inf"), 10**100, -1])
def test_invalid_issue_times_do_not_crash_or_order_credentials(pair, invalid):
    older, newer = pair
    newer.pop("last_refresh")
    newer["tokens"]["access_token"] = _jwt(iat=invalid)
    newer["tokens"]["id_token"] = "opaque-id"
    assert compare_credentials(newer, older).winner == "ambiguous"


def test_refresh_timezones_are_compared_as_instants(pair):
    older, newer = pair
    for profile in pair:
        profile["tokens"]["access_token"] = "opaque"
        profile["tokens"]["id_token"] = "opaque"
    older["last_refresh"] = "2025-01-01T23:30:00-08:00"
    newer["last_refresh"] = "2025-01-02T08:00:00+00:00"
    assert compare_credentials(newer, older).winner == "source"


def test_comparison_and_summary_do_not_expose_credentials(pair):
    older, newer = pair
    output = repr(compare_credentials(newer, older)) + credential_summary(newer)
    assert "2025-01-02" in output
    assert "UTC" in output
    for profile in pair:
        for key in ("access_token", "id_token", "refresh_token"):
            assert profile["tokens"][key] not in output
