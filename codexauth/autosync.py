"""Noninteractive bidirectional sync with credential-aware Git integration."""

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from time import sleep
from typing import Callable

import click

from codexauth import store
from codexauth.credentials import compare_credentials
from codexauth.git_sync import sync_local_profile_excludes
from codexauth.locking import sync_lock
from codexauth.reconcile import reconcile_active_to_store, reconcile_imported_active_profile
from codexauth.sync import HIDDEN_SYNC_FILE, list_blacklisted_profiles, parse_blacklisted_profiles

GIT_TIMEOUT_SECONDS = 30
DEFAULT_SYNC_ATTEMPTS = 3


class SyncError(Exception):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class SyncReport:
    imported: set[str] = field(default_factory=set)
    exported: set[str] = field(default_factory=set)
    removed: set[str] = field(default_factory=set)
    skipped: dict[str, str] = field(default_factory=dict)
    published: bool = False
    attempts: int = 0


def _profile(raw: bytes) -> dict:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Profile must be a JSON object.")
    if data.get("auth_mode") == "chatgpt":
        tokens = data.get("tokens")
        key = tokens.get("access_token") if isinstance(tokens, dict) else None
    elif data.get("auth_mode") == "api_key":
        key = data.get("OPENAI_API_KEY")
    else:
        raise ValueError("Unsupported profile type.")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Profile has no usable credential value.")
    return data


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write(path: Path, raw: bytes, expected: bytes | None) -> None:
    if _read(path) != expected:
        raise SyncError(f"{path.name} changed during sync; checking again.", retryable=True)
    if raw == expected:
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Preserve existing inodes, including hard links to the active auth file.
    path.write_bytes(raw)
    path.chmod(0o600)


def _names(raw: bytes | None) -> set[str]:
    return {line.strip() for line in (raw or b"").decode().splitlines() if line.strip()}


def _merge_names(local: set[str], external: set[str], base: set[str]) -> set[str]:
    return (local - base) | (external - base) | (local & external)


class SyncRepo:
    def __init__(self, sync_dir: Path):
        self.directory = sync_dir.resolve()
        if not self.directory.is_dir():
            raise SyncError(f"Sync directory does not exist: {sync_dir}")
        self.root = self.directory
        self.root = Path(self.git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
        self.prefix = self.directory.relative_to(self.root)
        self.branch = self.git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
        self.remote = self.git("config", "--get", f"branch.{self.branch}.remote").stdout.strip()
        self.remote_branch = self.git("config", "--get", f"branch.{self.branch}.merge").stdout.strip()
        if not self.remote_branch.startswith("refs/heads/"):
            raise SyncError("Sync requires a branch with a configured upstream branch.")
        self._file_cache: dict[str, set[str]] = {}

    def git(self, *args: str, check: bool = True):
        env = os.environ.copy()
        env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_MERGE_AUTOEDIT="no")
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=10")
        try:
            result = subprocess.run(
                ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "commit.gpgsign=false",
                 "-c", "merge.autoStash=false", *args],
                cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SyncError(f"git {args[0]} timed out.", retryable=True) from exc
        if check and result.returncode:
            detail = " The remote changed; fetching again." if "[rejected]" in result.stdout else ""
            # Never include blob output or remote URLs that may contain credentials.
            raise SyncError(
                f"git {args[0]} failed (exit {result.returncode}).{detail}",
                retryable=args[0] in {"fetch", "push"},
            )
        return result

    def path(self, name: str) -> str:
        return (self.prefix / name).as_posix()

    def managed(self, path: str) -> bool:
        candidate = Path(path)
        return candidate.parent == self.prefix and (
            candidate.suffix == ".json" or candidate.name in {HIDDEN_SYNC_FILE, ".gitignore"}
        )

    def check_ready(self) -> None:
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
            location = Path(self.git("rev-parse", "--git-path", marker).stdout.strip())
            if not location.is_absolute():
                location = self.root / location
            if location.exists():
                raise SyncError("The sync repository has an unfinished Git operation; leaving it untouched.")
        staged = self.git("diff", "--cached", "--name-only", "-z").stdout.split("\0")
        if any(path and not self.managed(path) for path in staged):
            raise SyncError("Unrelated staged changes in the sync repository need attention first.")

    def commit(self, report: SyncReport, *, merging: bool = False) -> None:
        changed = set()
        for args in (("diff", "--name-only", "-z"), ("diff", "--cached", "--name-only", "-z"),
                     ("ls-files", "--others", "--exclude-standard", "-z")):
            changed.update(filter(None, self.git(*args).stdout.split("\0")))
        paths = []
        banned = set(list_blacklisted_profiles(self.directory))
        local_only = store.list_local_only_profiles()
        for path in sorted(changed):
            if not self.managed(path):
                continue
            filename = self.root / path
            if filename.suffix == ".json":
                if filename.stem in banned or filename.stem in local_only:
                    continue
                raw = _read(filename)
                try:
                    if raw is None:
                        raise ValueError("Deleted profile")
                    _profile(raw)
                except (ValueError, UnicodeError):
                    report.skipped[filename.stem] = "Invalid or deleted sync profile; left untouched."
                    continue
            paths.append(path)
        if paths:
            self.git("add", "--", *(f":(literal){path}" for path in paths))
        staged = set(filter(None, self.git("diff", "--cached", "--name-only", "-z").stdout.split("\0")))
        if not merging and staged - set(paths):
            raise SyncError("Staged files that cannot be synced automatically need attention first.")
        if merging or staged:
            self.git("commit", "-m", "Sync Codex credentials")

    def blob(self, ref: str, path: str) -> bytes | None:
        if path not in self.files(ref):
            return None
        return self.git("show", f"{ref}:{path}").stdout.encode()

    def files(self, ref: str) -> set[str]:
        if ref not in self._file_cache:
            paths = self.git("ls-tree", "-r", "--name-only", "-z", ref).stdout.split("\0")
            self._file_cache[ref] = {path for path in paths if path and self.managed(path)}
        return self._file_cache[ref]

    def integrate(self, report: SyncReport) -> str:
        self.git("fetch", "--no-tags", self.remote, self.remote_branch)
        remote_head = self.git("rev-parse", "FETCH_HEAD").stdout.strip()
        local_head = self.git("rev-parse", "HEAD").stdout.strip()
        if remote_head == local_head:
            return remote_head
        base = self.git("merge-base", local_head, remote_head).stdout.strip()
        # A tracked historical file can still be explicitly banned. Honor bans
        # from either branch before interpreting any credential-bearing blobs.
        banned = set()
        for ref in (local_head, remote_head):
            ignore = self.blob(ref, self.path(".gitignore"))
            if ignore is not None:
                banned.update(parse_blacklisted_profiles(ignore.decode()))
        selected: dict[str, bytes] = {}
        remote_files = self.files(remote_head)
        for path in sorted(self.files(local_head) | remote_files):
            local, external = self.blob(local_head, path), self.blob(remote_head, path)
            if path.endswith(".json") and (Path(path).stem in banned or local == external):
                # No credential merge is needed for banned or unchanged files.
                # Leave banned tracked files to normal Git integration, which
                # still refuses unresolved conflicts or overlapping local edits.
                continue
            if self.managed(path) and _read(self.root / path) != local:
                raise SyncError(f"Uncommitted changes in {Path(path).name} need attention first.")
            if path.endswith(".json"):
                if local is None or external is None:
                    selected[path] = local if external is None else external
                    continue
                try:
                    comparison = compare_credentials(_profile(local), _profile(external))
                except (ValueError, UnicodeError) as exc:
                    raise SyncError(f"Cannot merge invalid Git profile {Path(path).name}.") from exc
                if comparison.winner == "ambiguous":
                    raise SyncError(f"Git profile {Path(path).name} needs attention: {comparison.reason}")
                selected[path] = local if comparison.winner == "source" else external
            elif Path(path).name == HIDDEN_SYNC_FILE:
                names = _merge_names(_names(local), _names(external), _names(self.blob(base, path)))
                selected[path] = "".join(f"{name}\n" for name in sorted(names)).encode()

        # A fast-forward is enough if no credential needs to override the remote tree.
        can_fast_forward = self.git("merge-base", "--is-ancestor", local_head, remote_head, check=False).returncode == 0
        if can_fast_forward and all(self.blob(remote_head, path) == data for path, data in selected.items()):
            self.git("merge", "--ff-only", remote_head)
            return remote_head

        try:
            merge_result = self.git("merge", "--no-commit", "--no-ff", remote_head, check=False)
            merge_head = self.git("rev-parse", "--verify", "--quiet", "MERGE_HEAD", check=False)
            merging = merge_head.returncode == 0
            if merge_result.returncode and not merging:
                raise SyncError("Git could not integrate the remote changes; the working files were left untouched.")
            conflicts = set(filter(None, self.git("diff", "--name-only", "--diff-filter=U", "-z").stdout.split("\0")))
            if conflicts - selected.keys():
                raise SyncError("Git has a conflict outside the credential files; leaving it for manual resolution.")
            for path, raw in selected.items():
                destination = self.root / path
                _write(destination, raw, _read(destination))
            if conflicts:
                self.git("add", "--", *(f":(literal){path}" for path in sorted(conflicts)))
            self.commit(report, merging=merging)
        except BaseException:
            if self.git("rev-parse", "--verify", "--quiet", "MERGE_HEAD", check=False).returncode == 0:
                self.git("merge", "--abort")
            raise
        return remote_head


def _merge_store(sync_dir: Path, report: SyncReport) -> None:
    active = store.get_active()
    try:
        preflight = reconcile_active_to_store(prompt_on_unsafe=False)
    except (ValueError, click.ClickException):
        if active:
            report.skipped[active] = "Invalid active credentials; left untouched."
    else:
        if preflight.status in {"unsafe", "warning"} and active:
            report.skipped[active] = preflight.message
    local_only = store.list_local_only_profiles()
    banned = set(list_blacklisted_profiles(sync_dir)) - local_only
    for name in sorted(banned & set(store.list_profiles())):
        store.delete_profile(name)
        if store.get_active() == name:
            store.ACTIVE_FILE.unlink(missing_ok=True)
        report.removed.add(name)

    external_names = {path.stem for path in sync_dir.glob("*.json")}
    for name in sorted((set(store.list_profiles()) | external_names) - banned - local_only):
        if name in report.skipped:
            continue
        local_path, external_path = store.TOKENS_DIR / f"{name}.json", sync_dir / f"{name}.json"
        local, external = _read(local_path), _read(external_path)
        if local is None and external is None:
            continue
        try:
            local_data = _profile(local) if local is not None else None
            external_data = _profile(external) if external is not None else None
        except (ValueError, UnicodeError):
            report.skipped[name] = "Invalid profile data; left untouched."
            continue
        if local_data is None:
            winner = "destination"
        elif external_data is None:
            winner = "source"
        else:
            comparison = compare_credentials(local_data, external_data)
            winner = comparison.winner
            if winner == "ambiguous":
                report.skipped[name] = comparison.reason
        if winner == "source":
            _write(external_path, local, external)
            report.exported.add(name)
        elif winner == "destination":
            _write(local_path, external, local)
            report.imported.add(name)

    try:
        result = reconcile_imported_active_profile(report.imported, prompt_on_unsafe=False)
    except (ValueError, click.ClickException):
        if store.get_active():
            report.skipped[store.get_active()] = "Invalid active credentials; left untouched."
        return
    if result.status in {"unsafe", "warning"} and store.get_active():
        report.skipped[store.get_active()] = result.message
    if result.store_updated_from_auth:
        raise SyncError("Active credentials changed during sync; checking again.", retryable=True)


def _merge_hidden(sync_dir: Path) -> tuple[Path, bytes]:
    key = hashlib.sha256(str(sync_dir.resolve()).encode()).hexdigest()[:16]
    state_path = store.STORE_DIR / "sync-state" / f"{key}.json"
    previous = _read(state_path)
    base = set(json.loads(previous)["hidden"]) if previous else set()
    external_path = sync_dir / HIDDEN_SYNC_FILE
    external_raw = _read(external_path)
    local_only = store.list_local_only_profiles()
    local_hidden = store.list_hidden_profiles()
    merged = _merge_names(local_hidden - local_only, _names(external_raw), base)
    merged &= set(store.list_profiles()) - local_only
    store.save_hidden_profiles(merged | (local_hidden & local_only))
    if merged or external_raw is not None:
        _write(external_path, "".join(f"{name}\n" for name in sorted(merged)).encode(), external_raw)
    return state_path, json.dumps({"hidden": sorted(merged)}).encode()


def _local_snapshot() -> dict[Path, bytes | None]:
    return {
        path: _read(path) for path in [
            *store.TOKENS_DIR.glob("*.json"), store.CODEX_AUTH, store.ACTIVE_FILE, store.HIDDEN_FILE,
            store.LOCAL_ONLY_FILE,
        ]
    }


def _save_state(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sync_once(
    sync_dir: Path,
    *,
    max_attempts: int = DEFAULT_SYNC_ATTEMPTS,
    notify: Callable[[str], None] = lambda message: None,
) -> SyncReport:
    max_attempts = max(1, max_attempts)
    report = SyncReport()
    with sync_lock():
        repo = SyncRepo(sync_dir)
        sync_local_profile_excludes(repo.directory, store.list_local_only_profiles())
        repo.check_ready()
        for attempt in range(1, max_attempts + 1):
            report.attempts = attempt
            report.skipped.clear()
            try:
                repo.commit(report)
                remote_head = repo.integrate(report)
                _merge_store(repo.directory, report)
                state_path, state = _merge_hidden(repo.directory)
                snapshot = _local_snapshot()
                repo.commit(report)
                if repo.git("rev-parse", "HEAD").stdout.strip() != remote_head:
                    repo.git("push", "--porcelain", repo.remote, f"HEAD:{repo.remote_branch}")
                    report.published = True
                if _local_snapshot() != snapshot:
                    raise SyncError("Local credentials changed while publishing; checking again.", retryable=True)
                _save_state(state_path, state)
                return report
            except SyncError as exc:
                if not exc.retryable or attempt == max_attempts:
                    raise
                notify(f"{exc} Retrying sync ({attempt + 1}/{max_attempts}).")
                sleep(min(2 ** (attempt - 1), 4))
    return report
