"""Helpers for reconciling the active auth file with stored profiles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import click

from codexauth import store
from codexauth.credentials import compare_credentials
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


def _reconcile_pair(
    name: str,
    stored_profile: dict,
    auth_profile: dict,
    prompt_on_unsafe: bool,
    imported: bool,
) -> ReconcileResult:
    comparison = compare_credentials(stored_profile, auth_profile)
    if comparison.winner == "identical":
        return ReconcileResult("noop", f"Active profile '{name}' is already in sync.")

    if comparison.winner == "ambiguous":
        message = (
            f"Cannot automatically reconcile active profile '{name}': {comparison.reason}"
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

    if comparison.winner == "source":
        store.save_codex_auth(stored_profile)
        prefix = "imported active " if imported else ""
        return ReconcileResult(
            "updated",
            f"Updated ~/.codex/auth.json from {prefix}profile '{name}'.",
        )
    if comparison.winner == "destination":
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
        prompt_on_unsafe=prompt_on_unsafe,
        imported=True,
    )
