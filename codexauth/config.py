"""Configuration helpers for repo-local settings."""

import sys
from pathlib import Path

SYNC_DIR_ENV = "CODEXAUTH_SYNC_DIR"


def _default_dotenv_paths() -> list[Path]:
    """Return likely repo-local .env locations for the current invocation."""
    paths: list[Path] = []

    main_module = sys.modules.get("__main__")
    main_file = getattr(main_module, "__file__", None)
    if main_file:
        main_path = Path(main_file).resolve()
        paths.append(main_path.parent / ".env")
        if len(main_path.parents) > 1:
            paths.append(main_path.parents[1] / ".env")

    paths.append(Path.cwd() / ".env")

    unique_paths: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path not in seen:
            unique_paths.append(path)
            seen.add(path)
    return unique_paths


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Load simple KEY=VALUE pairs from a .env file."""
    env_paths = [path] if path is not None else _default_dotenv_paths()

    env_path = next((candidate for candidate in env_paths if candidate.exists()), None)
    if env_path is None:
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
