from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from backlog_miner import run_backlog_prompt
from runner_core import RunnerConfig


def test_run_backlog_prompt_codex_prefers_host_login_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, str | None] = {}

    def _fake_run_codex_exec(**kwargs: object) -> object:
        observed["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY")
        observed["OPENAI_BASE_URL"] = os.environ.get("OPENAI_BASE_URL")
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
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr("backlog_miner.ensemble.run_codex_exec", _fake_run_codex_exec)
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key")
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

    assert observed["OPENAI_API_KEY"] is None
    assert observed["OPENAI_BASE_URL"] is None
    assert observed["ignore_rules"] == "True"
    assert os.environ.get("OPENAI_API_KEY") == "dummy-key"
    assert os.environ.get("OPENAI_BASE_URL") == "https://example.invalid/v1"
    assert output == "[]"


def test_run_backlog_prompt_codex_failure_does_not_return_stale_last_message(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def _fake_run_codex_exec(**kwargs: object) -> object:
        raw_events_path = kwargs.get("raw_events_path")
        stderr_path = kwargs.get("stderr_path")
        assert isinstance(raw_events_path, Path)
        assert isinstance(stderr_path, Path)
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("agent failed\n", encoding="utf-8")
        return SimpleNamespace(exit_code=1)

    monkeypatch.setattr("backlog_miner.ensemble.run_codex_exec", _fake_run_codex_exec)
    out_dir = tmp_path / "backlog_artifacts"
    out_dir.mkdir()
    stale_last_message = out_dir / "miner_001.last_message.txt"
    stale_response = out_dir / "miner_001.response.txt"
    stale_last_message.write_text("[{\"problem_id\":\"stale\"}]", encoding="utf-8")
    stale_response.write_text("[{\"problem_id\":\"stale-response\"}]", encoding="utf-8")

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
