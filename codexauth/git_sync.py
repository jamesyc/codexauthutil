"""Git helpers for the configured sync directory."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitCommandError(Exception):
    """Raised when a git subprocess fails."""

    def __init__(self, args: list[str], stderr: str = "", stdout: str = ""):
        self.args = args
        self.stderr = stderr.strip()
        self.stdout = stdout.strip()
        super().__init__(self.message)

    @property
    def message(self) -> str:
        details = self.stderr or self.stdout or "git command failed"
        return f"{' '.join(self.args)} failed: {details}"


def _run_git(sync_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    cmd = ["git", *args]
    result = subprocess.run(
        cmd,
        cwd=sync_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitCommandError(cmd, stderr=result.stderr, stdout=result.stdout)
    return result


def ensure_git_repo(sync_dir: Path) -> None:
    if not sync_dir.exists():
        raise FileNotFoundError(f"Sync directory does not exist: {sync_dir}")
    _run_git(sync_dir, "rev-parse", "--is-inside-work-tree")


def pull_sync_repo(sync_dir: Path) -> str:
    ensure_git_repo(sync_dir)
    result = _run_git(sync_dir, "pull")
    return (result.stdout or result.stderr).strip() or "Already up to date."


def push_sync_repo(sync_dir: Path, message: str = "Update exported codexauth profiles") -> str:
    ensure_git_repo(sync_dir)
    _run_git(sync_dir, "add", ".")
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=sync_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if diff.returncode == 0:
        return "No changes to commit."
    if diff.returncode != 1:
        raise GitCommandError(
            ["git", "diff", "--cached", "--quiet"],
            stderr=diff.stderr,
            stdout=diff.stdout,
        )

    _run_git(sync_dir, "commit", "-m", message)
    result = _run_git(sync_dir, "push")
    return (result.stdout or result.stderr).strip() or "Pushed changes."
