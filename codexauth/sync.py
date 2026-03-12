"""Import/export helpers for profile synchronization."""

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from codexauth import store
from codexauth.store import ProfileNotFoundError


@dataclass
class SyncCandidate:
    name: str
    source_path: Path
    dest_path: Path
    source_modified: datetime
    dest_modified: datetime | None

    @property
    def should_confirm_overwrite(self) -> bool:
        return (
            self.dest_modified is not None
            and self.source_modified < self.dest_modified
        )


def profile_path(name: str) -> Path:
    return store.TOKENS_DIR / f"{name}.json"


def list_sync_profiles(sync_dir: Path) -> list[str]:
    if not sync_dir.exists():
        return []
    return sorted(path.stem for path in sync_dir.glob("*.json"))


def build_import_candidates(sync_dir: Path) -> list[SyncCandidate]:
    candidates: list[SyncCandidate] = []
    for name in list_sync_profiles(sync_dir):
        source_path = sync_dir / f"{name}.json"
        dest_path = profile_path(name)
        dest_modified = (
            datetime.fromtimestamp(dest_path.stat().st_mtime) if dest_path.exists() else None
        )
        candidates.append(
            SyncCandidate(
                name=name,
                source_path=source_path,
                dest_path=dest_path,
                source_modified=datetime.fromtimestamp(source_path.stat().st_mtime),
                dest_modified=dest_modified,
            )
        )
    return candidates


def build_export_candidates(sync_dir: Path) -> list[SyncCandidate]:
    candidates: list[SyncCandidate] = []
    for name in store.list_profiles():
        source_path = profile_path(name)
        dest_path = sync_dir / f"{name}.json"
        dest_modified = (
            datetime.fromtimestamp(dest_path.stat().st_mtime) if dest_path.exists() else None
        )
        candidates.append(
            SyncCandidate(
                name=name,
                source_path=source_path,
                dest_path=dest_path,
                source_modified=datetime.fromtimestamp(source_path.stat().st_mtime),
                dest_modified=dest_modified,
            )
        )
    return candidates


def read_profile(path: Path) -> dict:
    return json.loads(path.read_text())


def import_profile(name: str, source_path: Path):
    read_profile(source_path)
    dest_path = profile_path(name)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, dest_path)
    dest_path.chmod(0o600)


def export_profile(name: str, dest_path: Path):
    try:
        source_path = profile_path(name)
        if not source_path.exists():
            raise ProfileNotFoundError(f"Profile '{name}' not found.")
        read_profile(source_path)
    except ProfileNotFoundError:
        raise
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, dest_path)
    dest_path.chmod(0o600)


def format_modified(value: datetime | None) -> str:
    if value is None:
        return "missing"
    return value.strftime("%Y-%m-%d %H:%M:%S")
