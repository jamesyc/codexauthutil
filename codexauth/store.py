"""Profile storage: read/write token files and active marker."""

import json
import os
import shutil
from pathlib import Path, PureWindowsPath

STORE_DIR = Path.home() / ".codexauth"
TOKENS_DIR = STORE_DIR / "tokens"
ACTIVE_FILE = STORE_DIR / "active"
HIDDEN_FILE = STORE_DIR / "hidden"
LOCAL_ONLY_FILE = STORE_DIR / "local-only"
CODEX_AUTH = Path.home() / ".codex" / "auth.json"
CODEX_AUTH_BACKUP = STORE_DIR / "auth.json.bak"


class ProfileNotFoundError(Exception):
    pass


def _ensure_store():
    STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    TOKENS_DIR.mkdir(mode=0o700, exist_ok=True)


def validate_profile_name(name: str) -> None:
    if (
        not name
        or name != name.strip()
        or name in {".", ".."}
        or Path(name).name != name
        or PureWindowsPath(name).drive
        or "\\" in name
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise ValueError("Profile name must be a single, non-empty filename component.")


def profile_path(name: str) -> Path:
    validate_profile_name(name)
    return TOKENS_DIR / f"{name}.json"


def _write_json_in_place(path: Path, data: dict):
    """Overwrite JSON content without replacing the destination inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    path.chmod(0o600)


def _copy_file_in_place(src: Path, dest: Path, *, preserve_mtime: bool):
    """Copy file contents without replacing the destination inode when it exists."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and src.samefile(dest):
        dest.chmod(0o600)
        return
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


def list_local_only_profiles() -> set[str]:
    _ensure_store()
    if not LOCAL_ONLY_FILE.exists():
        return set()
    return {
        line.strip()
        for line in LOCAL_ONLY_FILE.read_text().splitlines()
        if line.strip()
    }


def _save_local_only_profiles(names: set[str]) -> None:
    _ensure_store()
    existing = set(list_profiles())
    local_names = sorted(name for name in names if name in existing)
    if local_names:
        LOCAL_ONLY_FILE.write_text("".join(f"{name}\n" for name in local_names))
        LOCAL_ONLY_FILE.chmod(0o600)
    else:
        LOCAL_ONLY_FILE.unlink(missing_ok=True)


def mark_profile_local_only(name: str) -> None:
    if name not in list_profiles():
        raise ProfileNotFoundError(f"Profile '{name}' not found.")
    names = list_local_only_profiles()
    names.add(name)
    _save_local_only_profiles(names)


def _save_hidden_profiles(names: set[str]) -> None:
    _ensure_store()
    existing = set(list_profiles())
    visible_names = sorted(name for name in names if name in existing)
    if visible_names:
        HIDDEN_FILE.write_text("".join(f"{name}\n" for name in visible_names))
        HIDDEN_FILE.chmod(0o600)
    else:
        HIDDEN_FILE.unlink(missing_ok=True)


def save_hidden_profiles(names: set[str]) -> None:
    _save_hidden_profiles(names)


def list_visible_profiles() -> list[str]:
    hidden = list_hidden_profiles()
    return [name for name in list_profiles() if name not in hidden]


def hide_profile(name: str) -> None:
    path = profile_path(name)
    if not path.exists():
        raise ProfileNotFoundError(f"Profile '{name}' not found.")
    hidden = list_hidden_profiles()
    hidden.add(name)
    _save_hidden_profiles(hidden)


def unhide_profile(name: str) -> None:
    path = profile_path(name)
    if not path.exists():
        raise ProfileNotFoundError(f"Profile '{name}' not found.")
    hidden = list_hidden_profiles()
    hidden.discard(name)
    _save_hidden_profiles(hidden)


def load_profile(name: str) -> dict:
    path = profile_path(name)
    if not path.exists():
        raise ProfileNotFoundError(f"Profile '{name}' not found.")
    return json.loads(path.read_text())


def save_profile(name: str, data: dict):
    _ensure_store()
    path = profile_path(name)
    _write_json_in_place(path, data)


def save_profile_from_file(name: str, source_path: Path, preserve_mtime: bool = True):
    _ensure_store()
    dest_path = profile_path(name)
    _copy_file_in_place(source_path, dest_path, preserve_mtime=preserve_mtime)


def delete_profile(name: str):
    path = profile_path(name)
    if not path.exists():
        raise ProfileNotFoundError(f"Profile '{name}' not found.")
    path.unlink()
    hidden = list_hidden_profiles()
    if name in hidden:
        hidden.remove(name)
        _save_hidden_profiles(hidden)
    local_only = list_local_only_profiles()
    if name in local_only:
        local_only.remove(name)
        _save_local_only_profiles(local_only)


def get_active() -> str | None:
    if ACTIVE_FILE.exists():
        val = ACTIVE_FILE.read_text().strip()
        return val if val else None
    return None


def set_active(name: str):
    validate_profile_name(name)
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
    src = profile_path(name)
    if not src.exists():
        raise ProfileNotFoundError(f"Profile '{name}' not found.")
    if CODEX_AUTH.exists():
        shutil.copy2(CODEX_AUTH, CODEX_AUTH_BACKUP)
    _copy_file_in_place(src, CODEX_AUTH, preserve_mtime=True)
    set_active(name)
