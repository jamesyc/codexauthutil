"""Git helpers for the configured sync directory."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitCommandError(Exception):
    """Raised when a git subprocess fails."""

    def __init__(self, args: list[str], stderr: str = "", stdout: str = ""):
        self.command = tuple(args)
        self.stderr = stderr.strip()
        self.stdout = stdout.strip()
        super().__init__(self.message)

    @property
    def message(self) -> str:
        details = _combined_output(self.stdout, self.stderr) or "git command failed"
        return f"{' '.join(self.command)} failed: {details}"


def _combined_output(stdout: str = "", stderr: str = "") -> str:
    """Return all useful Git output without repeating identical streams."""
    parts: list[str] = []
    for value in (stderr.strip(), stdout.strip()):
        if value and value not in parts:
            parts.append(value)
    return "\n".join(parts)


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
    result = _run_git(sync_dir, "pull", "--no-rebase", "--no-edit")
    return _combined_output(result.stdout, result.stderr) or "Already up to date."


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
    committed = diff.returncode == 1
    if diff.returncode not in {0, 1}:
        raise GitCommandError(
            ["git", "diff", "--cached", "--quiet"],
            stderr=diff.stderr,
            stdout=diff.stdout,
        )

    if committed:
        _run_git(sync_dir, "commit", "-m", message)
        # Close the small race between the pre-export pull and publication.
        _run_git(sync_dir, "pull", "--no-rebase", "--no-edit")

    result = _run_git(sync_dir, "push")
    output = _combined_output(result.stdout, result.stderr) or "Pushed changes."
    if not committed:
        return f"No changes to commit.\n{output}"
    return output
