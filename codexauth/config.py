"""Configuration helpers for repo-local settings."""

from pathlib import Path

SYNC_DIR_ENV = "CODEXAUTH_SYNC_DIR"


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Load simple KEY=VALUE pairs from a .env file."""
    env_path = path or (Path.cwd() / ".env")
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def get_sync_dir(path: Path | None = None) -> Path | None:
    """Return the configured sync directory, expanding user-relative paths."""
    raw_value = load_dotenv(path).get(SYNC_DIR_ENV)
    if not raw_value:
        return None
    return Path(raw_value).expanduser()
