from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from runner_core import RunnerConfig, RunRequest, find_repo_root, run_once


def _install_no_requirements_mission(target_repo: Path) -> None:
    usertest_dir = target_repo / ".usertest"
    missions_dir = usertest_dir / "missions"
    missions_dir.mkdir(parents=True, exist_ok=True)

    (usertest_dir / "catalog.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "missions_dirs:",
                "  - .usertest/missions",
                "defaults:",
                "  mission_id: test_no_requirements_smoke",
                "",
            ]
        ),
        encoding="utf-8",
    )

    (missions_dir / "test_no_requirements_smoke.mission.md").write_text(
        "\n".join(
            [
                "---",
                "id: test_no_requirements_smoke",
                "name: Test No-Requirements Smoke",
                "extends: null",
                "execution_mode: single_pass_inline_report",
                "prompt_template: default_inline_report.prompt.md",
                "report_schema: default_report.schema.json",
                "requires_shell: false",
                "requires_edits: false",
                "---",
                "Mission used by tests that exercise read-only preflight flows.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _make_dummy_codex_with_personality_warning(tmp_path: Path) -> str:
    script = tmp_path / "dummy_codex_personality_warning.py"
    script.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import json",
                "import sys",
                "from pathlib import Path",
                "",
                "",
                "def main() -> int:",
                "    argv = sys.argv[1:]",
                "    out_path: str | None = None",
                "    if '--output-last-message' in argv:",
                "        idx = argv.index('--output-last-message')",
                "        if idx + 1 < len(argv):",
                "            out_path = argv[idx + 1]",
                "",
                "    report = {",
                "        'schema_version': 1,",
                "        'persona': {'name': 'Evaluator', 'description': 'Dummy'},",
                "        'mission': 'Test mission',",
                "        'minimal_mental_model': {'summary': 'ok', 'entry_points': ['README.md']},",
                "        'confidence_signals': {'found': ['x'], 'missing': []},",
                "        'confusion_points': [],",
                "        'adoption_decision': {'recommendation': 'investigate', 'rationale': 'x'},",
                "        'suggested_changes': [],",
                "    }",
                "    if out_path is not None:",
                "        Path(out_path).write_text(json.dumps(report) + '\\n', encoding='utf-8')",
                "",
                "    sys.stderr.write(",
                "        '2026-02-11T07:26:19.697569Z WARN codex_protocol::openai_models: '",
                (
                    "        'Model personality requested but model_messages is missing, "
                    "falling back '"
                ),
                "        'to base instructions. model=gpt-5.2 personality=pragmatic\\n'",
                "    )",
                "    sys.stderr.flush()",
                (
                    "    print(json.dumps({'id': '1', 'msg': {'type': 'agent_message', "
                    "'message': 'hi'}}))"
                ),
                "    return 0",
                "",
                "",
                "if __name__ == '__main__':",
                "    raise SystemExit(main())",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    if os.name == "nt":
        wrapper = tmp_path / "dummy_codex_personality_warning.cmd"
        wrapper.write_text(
            "\n".join(
                [
                    "@echo off",
                    f"\"{sys.executable}\" \"{script}\" %*",
                    "exit /b %ERRORLEVEL%",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return str(wrapper)

    wrapper = tmp_path / "dummy_codex_personality_warning.sh"
    wrapper.write_text(
        f"#!/bin/sh\nexec \"{sys.executable}\" \"{script}\" \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def test_run_once_succeeds_when_codex_reports_personality_warning(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    dummy_binary = _make_dummy_codex_with_personality_warning(tmp_path)
    cfg = RunnerConfig(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": dummy_binary}},
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="safe",
        ),
    )

    assert result.exit_code == 0
    assert result.report_validation_errors == []
    assert not (result.run_dir / "error.json").exists()

    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    warnings = attempts.get("attempts", [{}])[0].get("warnings", [])
    assert any("code=codex_model_messages_missing" in str(line) for line in warnings)
    stderr_text = (result.run_dir / "agent_stderr.txt").read_text(encoding="utf-8")
    assert "Model personality requested but model_messages is missing" in stderr_text
