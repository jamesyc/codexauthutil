"""Start an unset Codex weekly usage window with a minimal isolated request."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from codexauth.usage import UsageResult, WEEKLY_WINDOW_SECONDS, USAGE_URL, _parse_usage_windows

DEFAULT_WEEKLY_START_MODEL = "auto"
WEEKLY_START_TIMEOUT_SECONDS = 120
WEEKLY_START_PROMPT = "Reply with exactly: hi. Do not use tools."


@dataclass
class WeeklyStartResult:
    succeeded: bool
    detail: str | None = None
    updated_profile: dict | None = None
    model: str | None = None


def weekly_window_needs_start(usage: UsageResult) -> bool:
    """Return whether the weekly reset is null or its full seven-day duration remains."""
    if usage.error is not None:
        return False
    weekly_window = usage.windows.get("secondary_window")
    if weekly_window is None or weekly_window.reset_at is None:
        return True
    return weekly_window.reset_after_seconds == WEEKLY_WINDOW_SECONDS


def _last_output_line(completed: subprocess.CompletedProcess[str]) -> str | None:
    output = "\n".join(part for part in (completed.stderr, completed.stdout) if part)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    return lines[-1][:300]


async def _list_models(binary: str, env: dict, workspace: Path) -> list[dict]:
    """Discover the profile's catalog over the supported app-server protocol."""
    process = await asyncio.create_subprocess_exec(
        binary, "app-server", "--listen", "stdio://", "-c", 'cli_auth_credentials_store="file"',
        env=env, cwd=workspace, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=(os.name == "posix"),
    )
    async def rpc(request_id, method, params):
        process.stdin.write((json.dumps({"id": request_id, "method": method,
                                         "params": params}) + "\n").encode())
        await process.stdin.drain()
        while True:
            line = await process.stdout.readline()
            if not line:
                raise ValueError("Codex model discovery exited unexpectedly")
            message = json.loads(line)
            if message.get("id") == request_id:
                if "error" in message:
                    raise ValueError("Codex model discovery failed")
                return message["result"]
    try:
        await rpc(1, "initialize", {"clientInfo": {"name": "codexauth", "version": "0.1.0"}})
        process.stdin.write(b'{"method":"initialized"}\n')
        await process.stdin.drain()
        models, cursor, seen = [], None, set()
        while True:
            result = await rpc(2, "model/list", {"limit": 100, "cursor": cursor})
            models.extend(result["data"])
            cursor = result.get("nextCursor")
            if not cursor:
                return models
            if cursor in seen:
                raise ValueError("Codex model discovery repeated a page")
            seen.add(cursor)
    finally:
        if process.returncode is None:
            try:
                if os.name == "posix":
                    # npm launchers have a native child that also holds stdout open.
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
        await process.wait()


def _rank_models(models: list[dict]) -> list[dict]:
    # Catalogs do not expose parameter counts/prices. Prefer small model families,
    # then the account default, preserving catalog order for unknown models.
    def rank(item):
        slug = item["model"].lower()
        for index, token in enumerate(("nano", "mini", "luna", "terra")):
            if token in slug:
                return index
        return 4 if item.get("isDefault") else 5
    unique = {}
    for item in models:
        if (isinstance(item, dict) and isinstance(item.get("model"), str)
                and item["model"] and not item.get("hidden")
                and "text" in item.get("inputModalities", ["text"])):
            unique.setdefault(item["model"], item)
    return sorted(unique.values(), key=rank)


def _verify_weekly(profile: dict) -> bool:
    """Allow usage accounting to settle; never infer a timer from request success."""
    tokens = profile.get("tokens", {})
    headers = {"Authorization": f"Bearer {tokens.get('access_token', '')}"}
    if tokens.get("account_id"):
        headers["ChatGPT-Account-Id"] = tokens["account_id"]
    with httpx.Client(timeout=15) as client:
        for attempt in range(3):
            if attempt:
                time.sleep(2)
            response = client.get(USAGE_URL, headers=headers)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data.get("rate_limit"), dict):
                raise ValueError("Usage response has no rate limit")
            usage = UsageResult(windows=_parse_usage_windows(data["rate_limit"]))
            if not weekly_window_needs_start(usage):
                return True
    return False


def _start_auto(binary, env, workspace, auth_path, timeout):
    models = asyncio.run(asyncio.wait_for(_list_models(binary, env, workspace), timeout=30))
    current = json.loads(auth_path.read_text(encoding="utf-8"))
    attempts = []
    for item in _rank_models(models):
        model = item["model"]
        efforts = [entry.get("reasoningEffort") for entry in item.get("supportedReasoningEfforts", [])]
        effort = next((value for value in ("none", "minimal", "low", "medium", "high")
                       if value in efforts), item.get("defaultReasoningEffort", "low"))
        result = start_weekly_timer(current, model=model, codex_binary=binary,
                                    timeout=timeout, reasoning_effort=effort)
        current = result.updated_profile or current
        auth_path.write_text(json.dumps(current), encoding="utf-8")
        if not result.succeeded:
            detail = result.detail or "unknown error"
            # Only an explicit model rejection justifies another inference attempt.
            if "model" in detail.lower() and any(word in detail.lower() for word in
                    ("not supported", "does not exist", "not available", "unsupported")):
                attempts.append(f"{model}: unsupported")
                continue
            return WeeklyStartResult(False, f"{model}: {detail}", model=model)
        try:
            if _verify_weekly(current):
                return WeeklyStartResult(True, model=model)
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            return WeeklyStartResult(False, f"{model}: request succeeded but weekly verification failed ({type(exc).__name__})", model=model)
        attempts.append(f"{model}: weekly timer unchanged")
    return WeeklyStartResult(False, "; ".join(attempts) or "No available text models found")


def start_weekly_timer(
    profile: dict,
    *,
    model: str = DEFAULT_WEEKLY_START_MODEL,
    codex_binary: str = "codex",
    timeout: int = WEEKLY_START_TIMEOUT_SECONDS,
    reasoning_effort: str = "low",
) -> WeeklyStartResult:
    """Start a timer with discovered models, or send one explicit-model request."""
    binary = shutil.which(codex_binary)
    if binary is None:
        return WeeklyStartResult(False, f"{codex_binary!r} was not found on PATH")

    updated_profile = None
    with tempfile.TemporaryDirectory(prefix="codexauth-weekly-") as temp_dir:
        codex_home = Path(temp_dir)
        codex_home.chmod(0o700)
        workspace = codex_home / "workspace"
        workspace.mkdir(mode=0o700)
        auth_path = codex_home / "auth.json"
        auth_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        auth_path.chmod(0o600)

        env = os.environ.copy()
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
            env.pop(key, None)
        env["CODEX_HOME"] = str(codex_home)

        if model == "auto":
            try:
                result = _start_auto(binary, env, workspace, auth_path, timeout)
            except (OSError, ValueError, KeyError, TypeError, asyncio.TimeoutError):
                result = WeeklyStartResult(False, "Could not discover account models; retry or specify --model")
            finally:
                try:
                    current = json.loads(auth_path.read_text(encoding="utf-8"))
                    if isinstance(current, dict) and current != profile:
                        updated_profile = current
                except (OSError, ValueError):
                    pass
            result.updated_profile = updated_profile
            return result

        command = [
            binary,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "-C",
            str(workspace),
            "-m",
            model,
            "-c",
            f'model_reasoning_effort={json.dumps(reasoning_effort)}',
            "-c",
            'cli_auth_credentials_store="file"',
            WEEKLY_START_PROMPT,
        ]

        completed = None
        failure_detail = None
        try:
            completed = subprocess.run(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            failure_detail = f"Codex request timed out after {timeout} seconds"
        except OSError as exc:
            failure_detail = f"Could not run Codex: {exc}"
        finally:
            try:
                candidate = json.loads(auth_path.read_text(encoding="utf-8"))
                if isinstance(candidate, dict) and candidate != profile:
                    updated_profile = candidate
            except (OSError, json.JSONDecodeError):
                pass

        if failure_detail is not None:
            return WeeklyStartResult(False, failure_detail, updated_profile)
        assert completed is not None
        if completed.returncode != 0:
            detail = _last_output_line(completed) or f"Codex exited with status {completed.returncode}"
            return WeeklyStartResult(False, detail, updated_profile)
        return WeeklyStartResult(True, updated_profile=updated_profile, model=model)
