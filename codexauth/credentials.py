"""Compare complete credential sets without relying on filesystem timestamps."""

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


@dataclass(frozen=True)
class CredentialComparison:
    winner: Literal["source", "destination", "identical", "ambiguous"]
    reason: str


def _claims(token: object) -> dict:
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    try:
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, UnicodeDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _tokens(profile: dict) -> dict:
    tokens = profile.get("tokens")
    return tokens if isinstance(tokens, dict) else {}


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _identity(profile: dict) -> tuple[set[str], tuple[str, str] | None]:
    tokens = _tokens(profile)
    account_ids = set()
    account_id = _text(tokens.get("account_id"))
    if account_id:
        account_ids.add(account_id)
    for key in ("access_token", "id_token"):
        auth = _claims(tokens.get(key)).get("https://api.openai.com/auth")
        if isinstance(auth, dict):
            account_id = _text(auth.get("chatgpt_account_id"))
            if account_id:
                account_ids.add(account_id)

    claims = _claims(tokens.get("id_token"))
    issuer, subject = _text(claims.get("iss")), _text(claims.get("sub"))
    return account_ids, (issuer, subject) if issuer and subject else None


def _identity_problem(source: dict, destination: dict) -> str | None:
    source_accounts, source_subject = _identity(source)
    dest_accounts, dest_subject = _identity(destination)
    if len(source_accounts) > 1 or len(dest_accounts) > 1:
        return "Account identifiers within a credential set disagree."
    if source_accounts and dest_accounts and source_accounts != dest_accounts:
        return "The credentials belong to different accounts."
    if source_subject and dest_subject and source_subject != dest_subject:
        return "The credentials belong to different users."
    if (source_accounts and dest_accounts) or (source_subject and dest_subject):
        return None
    return "The account identity could not be confirmed."


def _parse_refresh(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _issued_at(token: object) -> datetime | None:
    value = _claims(token).get("iat")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def credential_times(profile: dict) -> dict[str, datetime | None]:
    tokens = _tokens(profile)
    return {
        "access token issued": _issued_at(tokens.get("access_token")),
        "ID token issued": _issued_at(tokens.get("id_token")),
        "last refresh": _parse_refresh(profile.get("last_refresh")),
    }


def credential_summary(profile: dict) -> str:
    """Describe freshness using only timestamps, never credential values."""
    timestamps = [
        f"{label} {value.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        for label, value in credential_times(profile).items()
        if value is not None
    ]
    return "; ".join(timestamps) or "credential timestamps unavailable"


def compare_credentials(source: dict, destination: dict) -> CredentialComparison:
    """Choose a whole profile only when identity and comparable timestamps agree.

    JWT claims here are local comparison hints, not signature or validity checks.
    Missing timestamps never make the other copy automatically newer.
    """
    if not isinstance(source, dict) or not isinstance(destination, dict):
        return CredentialComparison("ambiguous", "One credential set is not a valid profile object.")
    if source == destination:
        return CredentialComparison("identical", "Profile contents are identical.")
    if source.get("auth_mode") != "chatgpt" or destination.get("auth_mode") != "chatgpt":
        return CredentialComparison("ambiguous", "Only ChatGPT credentials have comparable refresh metadata.")
    if not _text(_tokens(source).get("access_token")) or not _text(_tokens(destination).get("access_token")):
        return CredentialComparison("ambiguous", "One credential set is missing an access token.")
    identity_problem = _identity_problem(source, destination)
    if identity_problem:
        return CredentialComparison("ambiguous", identity_problem)

    source_times, dest_times = credential_times(source), credential_times(destination)
    directions: set[Literal["source", "destination"]] = {
        "source" if value > dest_times[label] else "destination"
        for label, value in source_times.items()
        if value is not None and dest_times[label] is not None and value != dest_times[label]
    }
    if len(directions) > 1:
        return CredentialComparison("ambiguous", "Token issue times and last_refresh give conflicting freshness signals.")
    if not directions:
        return CredentialComparison("ambiguous", "Credential timestamps are equal or unavailable.")
    winner = directions.pop()
    return CredentialComparison(winner, f"The {winner} has newer credential timestamps.")
