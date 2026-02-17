from __future__ import annotations

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


def _make_dummy_codex_binary(tmp_path: Path) -> str:
    script = tmp_path / "dummy_codex.py"
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
                "        'persona': {",
                "            'name': 'Evaluator',",
                "            'description': 'Dummy codex for tests.',",
                "        },",
                "        'mission': 'Assess fit quickly and safely.',",
                "        'minimal_mental_model': {",
                "            'summary': 'A minimal report emitted by a dummy test binary.',",
                "            'entry_points': ['README.md'],",
                "        },",
                "        'confidence_signals': {",
                "            'found': ['Has files'],",
                "            'missing': ['No USERS.md provided'],",
                "        },",
                "        'confusion_points': [],",
                "        'adoption_decision': {",
                "            'recommendation': 'investigate',",
                "            'rationale': 'Test output.',",
                "        },",
                "        'suggested_changes': [],",
                "    }",
                "",
                "    if out_path is not None:",
                "        Path(out_path).write_text(json.dumps(report) + '\\n', encoding='utf-8')",
                "",
                "    # Emit a minimal raw event line so normalization has something to read.",
                "    msg = {'id': '1', 'msg': {'type': 'agent_message', 'message': 'hi'}}",
                "    print(json.dumps(msg))",
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
        wrapper = tmp_path / "dummy_codex.cmd"
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

    wrapper = tmp_path / "dummy_codex.sh"
    wrapper_text = f"#!/bin/sh\nexec \"{sys.executable}\" \"{script}\" \"$@\"\n"
    wrapper.write_text(wrapper_text, encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def test_run_once_allows_missing_users_md_without_opt_in(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    dummy_binary = _make_dummy_codex_binary(tmp_path)
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
    assert not result.report_validation_errors
    assert not (result.run_dir / "users.md").exists()
    assert (result.run_dir / "persona.source.md").exists()
    assert (result.run_dir / "persona.resolved.md").exists()
    assert (result.run_dir / "mission.source.md").exists()
    assert (result.run_dir / "mission.resolved.md").exists()
    assert (result.run_dir / "prompt.template.md").exists()
    assert (result.run_dir / "report.schema.json").exists()
    assert (result.run_dir / "effective_run_spec.json").exists()
    assert (result.run_dir / "normalized_events.jsonl").exists()
    assert (result.run_dir / "report.json").exists()
    assert (result.run_dir / "report.md").exists()


def test_run_once_missing_users_no_longer_blocks_preflight(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    dummy_binary = _make_dummy_codex_binary(tmp_path)
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
