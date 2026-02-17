from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_adapters.claude_cli import ClaudePrintResult

import runner_core.runner as runner_mod
from runner_core import RunnerConfig, RunRequest, run_once


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_run_once_claude_empty_stderr_writes_structured_synthetic_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = tmp_path / "runner_root"
    _write(
        runner_root / "configs" / "catalog.yaml",
        "\n".join(
            [
                "version: 1",
                "personas_dirs:",
                "  - configs/personas",
                "missions_dirs:",
                "  - configs/missions",
                "prompt_templates_dir: configs/prompt_templates",
                "report_schemas_dir: configs/report_schemas",
                "defaults:",
                "  persona_id: p",
                "  mission_id: m",
                "",
            ]
        ),
    )
    _write(
        runner_root / "configs" / "personas" / "p.persona.md",
        "\n".join(["---", "id: p", "name: P", "extends: null", "---", "P", ""]),
    )
    _write(
        runner_root / "configs" / "missions" / "m.mission.md",
        "\n".join(
            [
                "---",
                "id: m",
                "name: M",
                "extends: null",
                "execution_mode: single_pass_inline_report",
                "prompt_template: t.prompt.md",
                "report_schema: s.schema.json",
                "---",
                "Mission",
                "",
            ]
        ),
    )
    _write(runner_root / "configs" / "prompt_templates" / "t.prompt.md", "prompt\n")
    _write(runner_root / "configs" / "report_schemas" / "s.schema.json", "{\"type\":\"object\"}\n")

    target = tmp_path / "target"
    target.mkdir()
    _write(target / "README.md", "# hi\n")
    _write(target / "USERS.md", "# Users\n")

    def _fake_run_claude_print(**kwargs: object) -> ClaudePrintResult:
        raw_events_path = kwargs["raw_events_path"]
        last_message_path = kwargs["last_message_path"]
        stderr_path = kwargs["stderr_path"]
        assert isinstance(raw_events_path, Path)
        assert isinstance(last_message_path, Path)
        assert isinstance(stderr_path, Path)
        raw_events_path.write_text(
            "{\"type\":\"diagnostic\",\"message\":\"failed\"}\n", encoding="utf-8"
        )
        last_message_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return ClaudePrintResult(
            argv=["claude", "-p"],
            exit_code=1,
            raw_events_path=raw_events_path,
            last_message_path=last_message_path,
            stderr_path=stderr_path,
        )

    monkeypatch.setattr(runner_mod, "run_claude_print", _fake_run_claude_print)

    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"claude": {"binary": "python", "output_format": "stream-json"}},
        policies={"safe": {"claude": {"allow_edits": False, "allowed_tools": ["Read"]}}},
    )

    result = run_once(cfg, RunRequest(repo=str(target), agent="claude", policy="safe"))
    assert result.exit_code == 1

    stderr_text = (result.run_dir / "agent_stderr.txt").read_text(encoding="utf-8")
    assert "[synthetic_stderr]" in stderr_text
    assert "agent=claude" in stderr_text
    assert "raw_events_size_bytes=" in stderr_text
    assert "hint=Claude produced no stderr" in stderr_text

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "AgentExecFailed"
    assert error_obj.get("stderr_synthesized") is True
    artifacts = error_obj.get("artifacts")
    assert isinstance(artifacts, dict)
    assert artifacts.get("raw_events") == "raw_events.jsonl"
    assert artifacts.get("last_message") == "agent_last_message.txt"
    assert artifacts.get("stderr") == "agent_stderr.txt"
