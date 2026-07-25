from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from agent_adapters.claude_cli import ClaudePrintResult

import runner_core.runner as runner_mod
from runner_core import RunnerConfig, RunRequest, run_once


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_run_once_claude_quota_exhaustion_uses_structured_quota_error(
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

    provider_message = (
        "You are out of extra usage.\n"
        "Your plan resets Feb 24, 8pm (America/New_York).\n"
    )

    def _fake_run_claude_print(**kwargs: object) -> ClaudePrintResult:
        raw_events_path = kwargs["raw_events_path"]
        last_message_path = kwargs["last_message_path"]
        stderr_path = kwargs["stderr_path"]
        assert isinstance(raw_events_path, Path)
        assert isinstance(last_message_path, Path)
        assert isinstance(stderr_path, Path)
        raw_events_path.write_text(
            "{\"type\":\"diagnostic\",\"message\":\"usage exhausted\"}\n",
            encoding="utf-8",
        )
        last_message_path.write_text(provider_message, encoding="utf-8")
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
        agents={"claude": {"binary": sys.executable, "output_format": "stream-json"}},
        policies={"safe": {"claude": {"allow_edits": False, "allowed_tools": ["Read"]}}},
    )

    result = run_once(cfg, RunRequest(repo=str(target), agent="claude", policy="safe"))
    assert result.exit_code == 1

    stderr_text = (result.run_dir / "agent_stderr.txt").read_text(encoding="utf-8")
    assert "[agent_quota_exceeded]" in stderr_text
    assert "out of extra usage" in stderr_text.lower()
    assert "reset_time=" in stderr_text
    assert "[synthetic_stderr]" not in stderr_text

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "AgentQuotaExceeded"
    assert error_obj.get("code") == "claude_out_of_extra_usage"
    assert error_obj.get("subtype") == "provider_quota_exceeded"
    assert error_obj.get("stderr_synthesized") is True
    assert error_obj.get("provider_message") == provider_message.strip()
    reset_time = error_obj.get("reset_time")
    assert isinstance(reset_time, dict)
    assert "Feb 24" in str(reset_time.get("raw"))

