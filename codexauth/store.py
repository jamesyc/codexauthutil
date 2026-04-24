"""Profile storage: read/write token files and active marker."""

import json
import os
import shutil
from pathlib import Path

STORE_DIR = Path.home() / ".codexauth"
TOKENS_DIR = STORE_DIR / "tokens"
ACTIVE_FILE = STORE_DIR / "active"
HIDDEN_FILE = STORE_DIR / "hidden"
CODEX_AUTH = Path.home() / ".codex" / "auth.json"
CODEX_AUTH_BACKUP = STORE_DIR / "auth.json.bak"


class ProfileNotFoundError(Exception):
    pass


def _ensure_store():
    STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    TOKENS_DIR.mkdir(mode=0o700, exist_ok=True)


def _write_json_in_place(path: Path, data: dict):
    """Overwrite JSON content without replacing the destination inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    path.chmod(0o600)


def _copy_file_in_place(src: Path, dest: Path, *, preserve_mtime: bool):
    """Copy file contents without replacing the destination inode when it exists."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as source_handle, dest.open("wb") as dest_handle:
        shutil.copyfileobj(source_handle, dest_handle)

    if preserve_mtime:
        stat = src.stat()
        os.utime(dest, (stat.st_atime, stat.st_mtime))

    dest.chmod(0o600)


def list_profiles() -> list[str]:
    _ensure_store()
    return sorted(p.stem for p in TOKENS_DIR.glob("*.json"))


def list_hidden_profiles() -> set[str]:
    _ensure_store()
    if not HIDDEN_FILE.exists():
        return set()
    return {
        line.strip()
        for line in HIDDEN_FILE.read_text().splitlines()
        if line.strip()
    }


def _save_hidden_profiles(names: set[str]) -> None:
    _ensure_store()
    existing = set(list_profiles())
    visible_names = sorted(name for name in names if name in existing)
    if visible_names:
        HIDDEN_FILE.write_text("".join(f"{name}\n" for name in visible_names))
        HIDDEN_FILE.chmod(0o600)
    else:
        HIDDEN_FILE.unlink(missing_ok=True)


def list_visible_profiles() -> list[str]:
    hidden = list_hidden_profiles()
    return [name for name in list_profiles() if name not in hidden]


def hide_profile(name: str) -> None:
    path = TOKENS_DIR / f"{name}.json"
    if not path.exists():
        raise ProfileNotFoundError(f"Profile '{name}' not found.")
    hidden = list_hidden_profiles()
    hidden.add(name)
    _save_hidden_profiles(hidden)


def unhide_profile(name: str) -> None:
    path = TOKENS_DIR / f"{name}.json"
    if not path.exists():
        raise ProfileNotFoundError(f"Profile '{name}' not found.")
    hidden = list_hidden_profiles()
    hidden.discard(name)
    _save_hidden_profiles(hidden)


def load_profile(name: str) -> dict:
    path = TOKENS_DIR / f"{name}.json"
    if not path.exists():
        raise ProfileNotFoundError(f"Profile '{name}' not found.")
    return json.loads(path.read_text())


def save_profile(name: str, data: dict):
    _ensure_store()
    path = TOKENS_DIR / f"{name}.json"
    _write_json_in_place(path, data)


def save_profile_from_file(name: str, source_path: Path, preserve_mtime: bool = True):
    _ensure_store()
    dest_path = TOKENS_DIR / f"{name}.json"
    _copy_file_in_place(source_path, dest_path, preserve_mtime=preserve_mtime)


def delete_profile(name: str):
    path = TOKENS_DIR / f"{name}.json"
    if not path.exists():
        raise ProfileNotFoundError(f"Profile '{name}' not found.")
    path.unlink()
    hidden = list_hidden_profiles()
    if name in hidden:
        hidden.remove(name)
        _save_hidden_profiles(hidden)


def get_active() -> str | None:
    if ACTIVE_FILE.exists():
        val = ACTIVE_FILE.read_text().strip()
        return val if val else None
    return None


def set_active(name: str):
    _ensure_store()
    ACTIVE_FILE.write_text(name + "\n")
    ACTIVE_FILE.chmod(0o600)


def save_codex_auth(data: dict):
    """Write ~/.codex/auth.json, backing up the existing file when present."""
    if CODEX_AUTH.exists():
        _ensure_store()
        shutil.copy2(CODEX_AUTH, CODEX_AUTH_BACKUP)
    _write_json_in_place(CODEX_AUTH, data)


def activate(name: str):
    """Copy a profile to ~/.codex/auth.json, backing up the existing file."""
    src = TOKENS_DIR / f"{name}.json"
    if not src.exists():
        raise ProfileNotFoundError(f"Profile '{name}' not found.")
    if CODEX_AUTH.exists():
        shutil.copy2(CODEX_AUTH, CODEX_AUTH_BACKUP)
    _copy_file_in_place(src, CODEX_AUTH, preserve_mtime=True)
    set_active(name)
