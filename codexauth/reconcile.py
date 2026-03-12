"""Helpers for reconciling the active auth file with stored profiles."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import click

from codexauth import store
from codexauth.display import console
from codexauth.store import ProfileNotFoundError


@dataclass
class ReconcileResult:
    status: str
    message: str
    store_updated_from_auth: bool = False


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Failed to parse {path}: {exc.msg}") from exc


def _decode_id_claims(profile: dict) -> dict | None:
    tokens = profile.get("tokens", {})
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str) or id_token.count(".") < 2:
        return None

    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def _identity_tuple(profile: dict) -> tuple[str, str] | None:
    claims = _decode_id_claims(profile)
    if not claims:
        return None
    iss = claims.get("iss")
    sub = claims.get("sub")
    if isinstance(iss, str) and iss and isinstance(sub, str) and sub:
        return iss, sub
    return None


def _identity_status(left: dict, right: dict) -> str:
    left_tokens = left.get("tokens", {})
    right_tokens = right.get("tokens", {})
    left_account = left_tokens.get("account_id")
    right_account = right_tokens.get("account_id")

    if left_account and right_account:
        return "match" if left_account == right_account else "conflict"

    left_identity = _identity_tuple(left)
    right_identity = _identity_tuple(right)
    if left_identity and right_identity:
        return "match" if left_identity == right_identity else "conflict"

    return "unknown"


def _parse_last_refresh(profile: dict) -> datetime | None:
    value = profile.get("last_refresh")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _choose_copy(name: str, reason: str) -> str:
    console.print(f"[yellow]{reason}[/yellow]")
    return click.prompt(
        f"Choose which copy should win for '{name}'",
        type=click.Choice(["store", "auth", "skip"], case_sensitive=False),
        default="skip",
        show_choices=True,
    ).lower()


def _apply_choice(
    name: str,
    choice: str,
    stored_profile: dict,
    auth_profile: dict,
    store_source_label: str,
    auth_source_label: str,
) -> ReconcileResult:
    if choice == "store":
        store.save_codex_auth(stored_profile)
        return ReconcileResult(
            "updated",
            f"Updated ~/.codex/auth.json from {store_source_label} profile '{name}'.",
        )
    if choice == "auth":
        store.save_profile(name, auth_profile)
        return ReconcileResult(
            "updated",
            f"Updated stored profile '{name}' from {auth_source_label}.",
            store_updated_from_auth=True,
        )
    return ReconcileResult("warning", f"Skipped reconciliation for active profile '{name}'.")


def _winner_from_recency(stored_profile: dict, auth_profile: dict, store_path: Path, auth_path: Path) -> str | None:
    stored_refresh = _parse_last_refresh(stored_profile)
    auth_refresh = _parse_last_refresh(auth_profile)
    store_mtime = store_path.stat().st_mtime
    auth_mtime = auth_path.stat().st_mtime

    refresh_winner = None
    mtime_winner = None

    if stored_refresh and auth_refresh and stored_refresh != auth_refresh:
        refresh_winner = "store" if stored_refresh > auth_refresh else "auth"
    if store_mtime != auth_mtime:
        mtime_winner = "store" if store_mtime > auth_mtime else "auth"

    if refresh_winner and mtime_winner and refresh_winner != mtime_winner:
        return None
    if refresh_winner:
        return refresh_winner
    if mtime_winner:
        return mtime_winner
    return None


def _reconcile_pair(
    name: str,
    stored_profile: dict,
    auth_profile: dict,
    store_path: Path,
    auth_path: Path,
    prompt_on_unsafe: bool,
    imported: bool,
) -> ReconcileResult:
    if stored_profile == auth_profile:
        return ReconcileResult("noop", f"Active profile '{name}' is already in sync.")

    identity_status = _identity_status(auth_profile, stored_profile)
    if identity_status != "match":
        message = (
            f"Active profile '{name}' differs from ~/.codex/auth.json, but identity could not be safely confirmed."
        )
        if not prompt_on_unsafe:
            return ReconcileResult("unsafe", message)
        choice = _choose_copy(name, message)
        return _apply_choice(
            name,
            choice,
            stored_profile,
            auth_profile,
            "active",
            "~/.codex/auth.json",
        )

    winner = _winner_from_recency(stored_profile, auth_profile, store_path, auth_path)
    if winner == "store":
        store.save_codex_auth(stored_profile)
        prefix = "imported active " if imported else ""
        return ReconcileResult(
            "updated",
            f"Updated ~/.codex/auth.json from {prefix}profile '{name}'.",
        )
    if winner == "auth":
        store.save_profile(name, auth_profile)
        if imported:
            return ReconcileResult(
                "updated",
                f"Updated imported active profile '{name}' from ~/.codex/auth.json.",
                store_updated_from_auth=True,
            )
        return ReconcileResult(
            "updated",
            f"Reconciled active profile '{name}' from ~/.codex/auth.json into store.",
            store_updated_from_auth=True,
        )

    choice = _choose_copy(
        name,
        "Active profile matches identity, but recency is ambiguous between last_refresh and modified time.",
    )
    return _apply_choice(
        name,
        choice,
        stored_profile,
        auth_profile,
        "imported active" if imported else "active",
        "~/.codex/auth.json",
    )


def reconcile_active_to_store(prompt_on_unsafe: bool = False) -> ReconcileResult:
    """Update the active stored profile from ~/.codex/auth.json when safe."""
    active = store.get_active()
    if not active:
        return ReconcileResult("noop", "No active profile to reconcile.")
    if not store.CODEX_AUTH.exists():
        return ReconcileResult("noop", "No ~/.codex/auth.json present; nothing to reconcile.")

    try:
        stored_profile = store.load_profile(active)
    except ProfileNotFoundError:
        return ReconcileResult(
            "warning",
            f"Active profile '{active}' is missing from ~/.codexauth/tokens; skipping reconciliation.",
        )

    auth_profile = _load_json(store.CODEX_AUTH)
    return _reconcile_pair(
        active,
        stored_profile,
        auth_profile,
        store.TOKENS_DIR / f"{active}.json",
        store.CODEX_AUTH,
        prompt_on_unsafe=prompt_on_unsafe,
        imported=False,
    )


def reconcile_imported_active_profile(imported_names: set[str], prompt_on_unsafe: bool = True) -> ReconcileResult:
    """Reconcile an imported active profile against ~/.codex/auth.json when needed."""
    active = store.get_active()
    if not active or active not in imported_names:
        return ReconcileResult("noop", "No imported active profile needed reconciliation.")
    if not store.CODEX_AUTH.exists():
        return ReconcileResult("noop", "No ~/.codex/auth.json present; imported profile kept in store.")

    stored_profile = store.load_profile(active)
    auth_profile = _load_json(store.CODEX_AUTH)
    return _reconcile_pair(
        active,
        stored_profile,
        auth_profile,
        store.TOKENS_DIR / f"{active}.json",
        store.CODEX_AUTH,
        prompt_on_unsafe=prompt_on_unsafe,
        imported=True,
    )
