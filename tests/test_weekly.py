"""Tests for starting unset weekly usage windows."""

import json
import os
import stat
import subprocess
from datetime import datetime, timezone

import codexauth.weekly as weekly_module
from codexauth.usage import UsageResult, UsageWindow


def test_weekly_window_needs_start_for_missing_or_zero_use_window():
    assert weekly_module.weekly_window_needs_start(UsageResult()) is True
    assert weekly_module.weekly_window_needs_start(
        UsageResult(windows={"secondary_window": UsageWindow("secondary_window")})
    ) is True
    assert weekly_module.weekly_window_needs_start(
        UsageResult(
            windows={
                "secondary_window": UsageWindow(
                    "secondary_window",
                    used_pct=0,
                    reset_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
                    limit_window_seconds=604800,
                    reset_after_seconds=604800,
                )
            }
        )
    ) is True
    assert weekly_module.weekly_window_needs_start(
        UsageResult(
            windows={
                "secondary_window": UsageWindow(
                    "secondary_window",
                    used_pct=25,
                    reset_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
                    limit_window_seconds=604800,
                    reset_after_seconds=604800,
                )
            }
        )
    ) is True
    assert weekly_module.weekly_window_needs_start(
        UsageResult(
            windows={
                "secondary_window": UsageWindow(
                    "secondary_window",
                    used_pct=0,
                    reset_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
                    limit_window_seconds=604800,
                    reset_after_seconds=500000,
                )
            }
        )
    ) is False
    assert weekly_module.weekly_window_needs_start(
        UsageResult(
            secondary_pct=1,
            secondary_reset_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        )
    ) is False
    assert weekly_module.weekly_window_needs_start(UsageResult(error="n/a")) is False


def test_start_weekly_timer_uses_isolated_auth_and_preserves_refresh(
    monkeypatch, sample_profile
):
    monkeypatch.setattr(weekly_module.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-leak")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "must-not-leak")
    observed = {}

    def fake_run(command, **kwargs):
        codex_home = weekly_module.Path(kwargs["env"]["CODEX_HOME"])
        auth_path = codex_home / "auth.json"
        workspace = codex_home / "workspace"
        observed["command"] = command
        observed["env"] = kwargs["env"]
        observed["auth"] = json.loads(auth_path.read_text())
        observed["auth_mode"] = stat.S_IMODE(auth_path.stat().st_mode)
        observed["home_mode"] = stat.S_IMODE(codex_home.stat().st_mode)
        observed["workspace"] = workspace
        refreshed = json.loads(auth_path.read_text())
        refreshed["tokens"]["access_token"] = "refreshed-access"
        auth_path.write_text(json.dumps(refreshed))
        return subprocess.CompletedProcess(command, 0, stdout="hi\n", stderr="")

    monkeypatch.setattr(weekly_module.subprocess, "run", fake_run)

    result = weekly_module.start_weekly_timer(sample_profile, model="gpt-test")

    assert result.succeeded is True
    assert result.updated_profile["tokens"]["access_token"] == "refreshed-access"
    assert observed["auth"] == sample_profile
    assert observed["auth_mode"] == 0o600
    assert observed["home_mode"] == 0o700
    assert observed["workspace"].exists() is False
    assert observed["command"][0:2] == ["/usr/bin/codex", "exec"]
    assert "--ephemeral" in observed["command"]
    assert "--ignore-user-config" in observed["command"]
    assert "--ignore-rules" in observed["command"]
    assert "read-only" in observed["command"]
    assert observed["command"][-1] == weekly_module.WEEKLY_START_PROMPT
    assert observed["command"][observed["command"].index("-m") + 1] == "gpt-test"
    assert observed["env"]["CODEX_HOME"]
    assert "OPENAI_API_KEY" not in observed["env"]
    assert "CODEX_API_KEY" not in observed["env"]
    assert "CODEX_ACCESS_TOKEN" not in observed["env"]


def test_start_weekly_timer_reports_missing_codex(monkeypatch, sample_profile):
    monkeypatch.setattr(weekly_module.shutil, "which", lambda name: None)

    result = weekly_module.start_weekly_timer(sample_profile)

    assert result.succeeded is False
    assert "not found on PATH" in result.detail


def test_start_weekly_timer_preserves_refresh_when_request_times_out(
    monkeypatch, sample_profile
):
    monkeypatch.setattr(weekly_module.shutil, "which", lambda name: "/usr/bin/codex")

    def fake_run(command, **kwargs):
        auth_path = weekly_module.Path(kwargs["env"]["CODEX_HOME"]) / "auth.json"
        refreshed = json.loads(auth_path.read_text())
        refreshed["tokens"]["access_token"] = "refreshed-before-timeout"
        auth_path.write_text(json.dumps(refreshed))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(weekly_module.subprocess, "run", fake_run)

    result = weekly_module.start_weekly_timer(sample_profile, model="gpt-test", timeout=3)

    assert result.succeeded is False
    assert "timed out after 3 seconds" in result.detail
    assert result.updated_profile["tokens"]["access_token"] == "refreshed-before-timeout"


def test_auto_tries_smallest_until_weekly_timer_changes(monkeypatch, sample_profile):
    monkeypatch.setattr(weekly_module.shutil, "which", lambda name: "/usr/bin/codex")

    async def models(*args):
        return [{"model": "gpt-large", "isDefault": True},
                {"model": "gpt-mini"}, {"model": "gpt-luna"}]

    monkeypatch.setattr(weekly_module, "_list_models", models)
    calls = []

    def run(command, **kwargs):
        model = command[command.index("-m") + 1]
        calls.append(model)
        return subprocess.CompletedProcess(command, 0, stdout="hi", stderr="")

    monkeypatch.setattr(weekly_module.subprocess, "run", run)
    verified = iter([False, True])
    monkeypatch.setattr(weekly_module, "_verify_weekly", lambda profile: next(verified))
    result = weekly_module.start_weekly_timer(sample_profile)
    assert result.succeeded
    assert result.model == "gpt-luna"
    assert calls == ["gpt-mini", "gpt-luna"]


def test_auto_skips_rejected_model_and_preserves_refresh(monkeypatch, sample_profile):
    monkeypatch.setattr(weekly_module.shutil, "which", lambda name: "/usr/bin/codex")

    async def models(*args):
        return [{"model": "gpt-mini"}, {"model": "gpt-terra"}]

    monkeypatch.setattr(weekly_module, "_list_models", models)
    calls = []

    def run(command, **kwargs):
        model = command[command.index("-m") + 1]
        calls.append(model)
        auth_path = weekly_module.Path(kwargs["env"]["CODEX_HOME"]) / "auth.json"
        profile = json.loads(auth_path.read_text())
        if len(calls) == 1:
            profile["tokens"]["access_token"] = "new-access"
            auth_path.write_text(json.dumps(profile))
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="model is not supported")
        assert profile["tokens"]["access_token"] == "new-access"
        return subprocess.CompletedProcess(command, 0, stdout="hi", stderr="")

    monkeypatch.setattr(weekly_module.subprocess, "run", run)
    monkeypatch.setattr(weekly_module, "_verify_weekly", lambda profile: True)
    result = weekly_module.start_weekly_timer(sample_profile)
    assert result.succeeded
    assert result.updated_profile["tokens"]["access_token"] == "new-access"
    assert calls == ["gpt-mini", "gpt-terra"]


def test_auto_stops_on_auth_error(monkeypatch, sample_profile):
    monkeypatch.setattr(weekly_module.shutil, "which", lambda name: "/usr/bin/codex")

    async def models(*args):
        return [{"model": "gpt-mini"}, {"model": "gpt-large"}]

    monkeypatch.setattr(weekly_module, "_list_models", models)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="401 unauthorized")

    monkeypatch.setattr(weekly_module.subprocess, "run", run)
    result = weekly_module.start_weekly_timer(sample_profile)
    assert not result.succeeded
    assert len(calls) == 1


def test_verify_weekly_waits_for_real_timer(monkeypatch, sample_profile):
    import httpx
    import respx

    monkeypatch.setattr(weekly_module.time, "sleep", lambda seconds: None)
    with respx.mock as router:
        route = router.get(weekly_module.USAGE_URL).mock(side_effect=[
            httpx.Response(200, json={"rate_limit": {"secondary_window": {
                "reset_at": 2000000000, "reset_after_seconds": seconds,
                "limit_window_seconds": 604800}}})
            for seconds in (604800, 604800, 604799)
        ])
        assert weekly_module._verify_weekly(sample_profile)
        assert route.call_count == 3


def test_rank_models_filters_hidden_and_duplicates():
    assert [m["model"] for m in weekly_module._rank_models([
        {"model": "large"}, {"model": "hidden-mini", "hidden": True},
        {"model": "gpt-luna"}, {"model": "gpt-luna"},
        {"model": "audio-mini", "inputModalities": ["audio"]},
        {"model": "gpt-terra"},
    ])] == ["gpt-luna", "gpt-terra", "large"]


def test_auto_stops_when_usage_verification_fails(monkeypatch, sample_profile):
    monkeypatch.setattr(weekly_module.shutil, "which", lambda name: "/usr/bin/codex")

    async def models(*args):
        return [{"model": "gpt-mini"}, {"model": "gpt-large"}]

    monkeypatch.setattr(weekly_module, "_list_models", models)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="hi", stderr="")

    def verify(profile):
        raise ValueError("missing usage")

    monkeypatch.setattr(weekly_module.subprocess, "run", run)
    monkeypatch.setattr(weekly_module, "_verify_weekly", verify)
    result = weekly_module.start_weekly_timer(sample_profile)
    assert not result.succeeded
    assert "verification failed" in result.detail
    assert len(calls) == 1


def test_model_discovery_initializes_paginates_and_cleans_up(monkeypatch, tmp_path):
    import asyncio

    class Process:
        returncode = None

        def __init__(self):
            self.stdin = self
            self.stdout = self
            self.messages = []
            self.responses = iter([
                {"id": 1, "result": {}},
                {"method": "notification"},
                {"id": 2, "result": {"data": [{"model": "gpt-mini"}], "nextCursor": "p2"}},
                {"id": 2, "result": {"data": [{"model": "gpt-large"}], "nextCursor": None}},
            ])

        def write(self, value):
            self.messages.append(json.loads(value))

        async def drain(self):
            pass

        async def readline(self):
            return json.dumps(next(self.responses)).encode() + b"\n"

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    process = Process()
    process.pid = 12345
    monkeypatch.setattr(weekly_module.os, "killpg", lambda pid, sig: process.kill())

    async def create(*args, **kwargs):
        assert kwargs["env"] == {"CODEX_HOME": str(tmp_path)}
        return process

    monkeypatch.setattr(weekly_module.asyncio, "create_subprocess_exec", create)
    models = asyncio.run(weekly_module._list_models("codex", {"CODEX_HOME": str(tmp_path)}, tmp_path))
    assert [m["model"] for m in models] == ["gpt-mini", "gpt-large"]
    assert [m["method"] for m in process.messages] == ["initialize", "initialized", "model/list", "model/list"]
    assert process.messages[-1]["params"]["cursor"] == "p2"
    assert process.returncode == -9
