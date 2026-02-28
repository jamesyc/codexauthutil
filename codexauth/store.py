"""Profile storage: read/write token files and active marker."""

import json
import shutil
from pathlib import Path

STORE_DIR = Path.home() / ".codexauth"
TOKENS_DIR = STORE_DIR / "tokens"
ACTIVE_FILE = STORE_DIR / "active"
CODEX_AUTH = Path.home() / ".codex" / "auth.json"
CODEX_AUTH_BACKUP = STORE_DIR / "auth.json.bak"


class ProfileNotFoundError(Exception):
    pass


def _ensure_store():
    STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    TOKENS_DIR.mkdir(mode=0o700, exist_ok=True)


def list_profiles() -> list[str]:
    _ensure_store()
    return sorted(p.stem for p in TOKENS_DIR.glob("*.json"))


def load_profile(name: str) -> dict:
    path = TOKENS_DIR / f"{name}.json"
    if not path.exists():
        raise ProfileNotFoundError(f"Profile '{name}' not found.")
    return json.loads(path.read_text())


def save_profile(name: str, data: dict):
    _ensure_store()
    path = TOKENS_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)


def delete_profile(name: str):
    path = TOKENS_DIR / f"{name}.json"
    if not path.exists():
        raise ProfileNotFoundError(f"Profile '{name}' not found.")
    path.unlink()


def get_active() -> str | None:
    if ACTIVE_FILE.exists():
        val = ACTIVE_FILE.read_text().strip()
        return val if val else None
    return None


def set_active(name: str):
    _ensure_store()
    ACTIVE_FILE.write_text(name + "\n")
    ACTIVE_FILE.chmod(0o600)


def activate(name: str):
    """Copy a profile to ~/.codex/auth.json, backing up the existing file."""
    src = TOKENS_DIR / f"{name}.json"
    if not src.exists():
        raise ProfileNotFoundError(f"Profile '{name}' not found.")
    if CODEX_AUTH.exists():
        shutil.copy2(CODEX_AUTH, CODEX_AUTH_BACKUP)
    CODEX_AUTH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, CODEX_AUTH)
    CODEX_AUTH.chmod(0o600)
    set_active(name)
