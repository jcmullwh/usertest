from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_adapters import CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS, CodexLoginStatusResult
from backlog_miner import run_backlog_prompt
from runner_core import RunnerConfig

_CODEX_THREAD_ID = "11111111-1111-4111-8111-111111111111"


def _chatgpt_status(codex_home: Path) -> CodexLoginStatusResult:
    return CodexLoginStatusResult(
        argv=["codex", "login", "status"],
        exit_code=0,
        stdout="Logged in using ChatGPT\n",
        stderr="",
        codex_home=str(codex_home),
        auth_env_vars_blank={name: True for name in CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS},
    )


def test_run_backlog_prompt_codex_prefers_host_login_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}
    codex_home = tmp_path / "host_codex_home"
    codex_home.mkdir()

    def _fake_run_codex_exec(**kwargs: object) -> object:
        observed["env_overrides"] = kwargs.get("env_overrides")
        observed["config_overrides"] = kwargs.get("config_overrides")
        observed["ignore_rules"] = str(kwargs.get("ignore_rules"))
        last_message_path = kwargs.get("last_message_path")
        raw_events_path = kwargs.get("raw_events_path")
        stderr_path = kwargs.get("stderr_path")
        assert isinstance(last_message_path, Path)
        assert isinstance(raw_events_path, Path)
        assert isinstance(stderr_path, Path)
        last_message_path.write_text("[]", encoding="utf-8")
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return SimpleNamespace(exit_code=0, thread_id=_CODEX_THREAD_ID)

    monkeypatch.setattr("backlog_miner.ensemble.run_codex_exec", _fake_run_codex_exec)
    monkeypatch.setattr(
        "backlog_miner.ensemble.probe_codex_login_status",
        lambda **_: _chatgpt_status(codex_home),
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key")
    monkeypatch.setenv("CODEX_API_KEY", "alternate-key")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "alternate-token")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")

    cfg = RunnerConfig(
        repo_root=tmp_path,
        runs_dir=tmp_path,
        agents={},
        policies={},
    )

    output = run_backlog_prompt(
        agent="codex",
        prompt="Return an empty list.",
        out_dir=tmp_path / "backlog_artifacts",
        tag="miner_001",
        model=None,
        cfg=cfg,
    )

    env_overrides = observed["env_overrides"]
    config_overrides = observed["config_overrides"]
    assert isinstance(env_overrides, dict)
    assert isinstance(config_overrides, list)
    assert env_overrides["OPENAI_API_KEY"] == ""
    assert env_overrides["CODEX_API_KEY"] == ""
    assert env_overrides["CODEX_ACCESS_TOKEN"] == ""
    assert env_overrides["OPENAI_BASE_URL"] == ""
    assert Path(env_overrides["CODEX_HOME"]).resolve() == codex_home.resolve()
    assert config_overrides[-2:] == [
        'forced_login_method="chatgpt"',
        'model_provider="openai"',
    ]
    assert observed["ignore_rules"] == "True"
    assert os.environ.get("OPENAI_API_KEY") == "dummy-key"
    assert os.environ.get("CODEX_API_KEY") == "alternate-key"
    assert os.environ.get("CODEX_ACCESS_TOKEN") == "alternate-token"
    assert os.environ.get("OPENAI_BASE_URL") == "https://example.invalid/v1"
    assert output == "[]"


def test_run_backlog_prompt_codex_failure_does_not_return_stale_last_message(
    monkeypatch,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "host_codex_home"
    codex_home.mkdir()

    def _fake_run_codex_exec(**kwargs: object) -> object:
        raw_events_path = kwargs.get("raw_events_path")
        stderr_path = kwargs.get("stderr_path")
        assert isinstance(raw_events_path, Path)
        assert isinstance(stderr_path, Path)
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("agent failed\n", encoding="utf-8")
        return SimpleNamespace(exit_code=1)

    monkeypatch.setattr("backlog_miner.ensemble.run_codex_exec", _fake_run_codex_exec)
    monkeypatch.setattr(
        "backlog_miner.ensemble.probe_codex_login_status",
        lambda **_: _chatgpt_status(codex_home),
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    out_dir = tmp_path / "backlog_artifacts"
    out_dir.mkdir()
    stale_last_message = out_dir / "miner_001.last_message.txt"
    stale_response = out_dir / "miner_001.response.txt"
    stale_last_message.write_text('[{"problem_id":"stale"}]', encoding="utf-8")
    stale_response.write_text('[{"problem_id":"stale-response"}]', encoding="utf-8")

    cfg = RunnerConfig(
        repo_root=tmp_path,
        runs_dir=tmp_path,
        agents={},
        policies={},
    )

    with pytest.raises(RuntimeError, match="Codex backlog prompt failed exit_code=1"):
        run_backlog_prompt(
            agent="codex",
            prompt="Return an empty list.",
            out_dir=out_dir,
            tag="miner_001",
            model=None,
            cfg=cfg,
        )

    assert not stale_last_message.exists()
    assert not stale_response.exists()
