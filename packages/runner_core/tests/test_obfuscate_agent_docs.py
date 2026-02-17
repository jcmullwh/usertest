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


def _make_dummy_codex_binary(tmp_path: Path, *, sentinel: str) -> str:
    script = tmp_path / "dummy_codex_obfuscate.py"
    script.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import json",
                "import os",
                "import sys",
                "from pathlib import Path",
                "",
                "",
                f"SENTINEL = {sentinel!r}",
                "",
                "",
                "def main() -> int:",
                "    argv = sys.argv[1:]",
                "    cd: str | None = None",
                "    out_path: str | None = None",
                "",
                "    if '--cd' in argv:",
                "        idx = argv.index('--cd')",
                "        if idx + 1 < len(argv):",
                "            cd = argv[idx + 1]",
                "    if cd is not None:",
                "        os.chdir(cd)",
                "",
                "    if '--output-last-message' in argv:",
                "        idx = argv.index('--output-last-message')",
                "        if idx + 1 < len(argv):",
                "            out_path = argv[idx + 1]",
                "",
                "    agents_md = Path('agents.md')",
                "    if agents_md.exists():",
                "        text = agents_md.read_text(encoding='utf-8', errors='replace')",
                "        if SENTINEL in text:",
                "            event = {",
                "                'id': '1',",
                "                'msg': {'type': 'agent_message', 'message': 'saw agents.md'},",
                "            }",
                "            print(json.dumps(event))",
                "            return 3",
                "",
                "    report = {",
                "        'schema_version': 1,",
                "        'persona': {",
                "            'name': 'Evaluator',",
                "            'description': 'Dummy codex for tests.',",
                "        },",
                "        'mission': 'Assess fit quickly and safely.',",
                "        'minimal_mental_model': {",
                "            'summary': 'Dummy report.',",
                "            'entry_points': ['README.md'],",
                "        },",
                "        'confidence_signals': {",
                "            'found': ['ok'],",
                "            'missing': [],",
                "        },",
                "        'confusion_points': [],",
                "        'adoption_decision': {",
                "            'recommendation': 'investigate',",
                "            'rationale': 'test',",
                "        },",
                "        'suggested_changes': [],",
                "    }",
                "",
                "    if out_path is not None:",
                "        Path(out_path).write_text(json.dumps(report) + '\\n', encoding='utf-8')",
                "",
                "    event = {'id': '1', 'msg': {'type': 'agent_message', 'message': 'ok'}}",
                "    print(json.dumps(event))",
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
        wrapper = tmp_path / "dummy_codex_obfuscate.cmd"
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

    wrapper = tmp_path / "dummy_codex_obfuscate.sh"
    wrapper_text = f"#!/bin/sh\nexec \"{sys.executable}\" \"{script}\" \"$@\"\n"
    wrapper.write_text(wrapper_text, encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def _make_cfg(
    *,
    repo_root: Path,
    runs_dir: Path,
    dummy_binary: str,
    allow_edits: bool,
) -> RunnerConfig:
    policy_name = "write" if allow_edits else "safe"
    return RunnerConfig(
        repo_root=repo_root,
        runs_dir=runs_dir,
        agents={"codex": {"binary": dummy_binary}},
        policies={policy_name: {"codex": {"sandbox": "read-only", "allow_edits": allow_edits}}},
    )


def test_obfuscate_agent_docs_hides_root_agents_md(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    sentinel = "SENTINEL_AGENT_DOC"
    (target / "agents.md").write_text(f"{sentinel}\n", encoding="utf-8")

    dummy_binary = _make_dummy_codex_binary(tmp_path, sentinel=sentinel)
    cfg = _make_cfg(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        dummy_binary=dummy_binary,
        allow_edits=False,
    )

    result_no_obfuscate = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="safe",
            seed=0,
            obfuscate_agent_docs=False,
        ),
    )
    assert result_no_obfuscate.exit_code != 0

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="safe",
            seed=1,
            obfuscate_agent_docs=True,
        ),
    )

    assert result.exit_code == 0
    assert not result.report_validation_errors

    manifest_path = result.run_dir / "obfuscated_agent_docs.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "moved" in manifest
    assert any(item.get("original_relpath") == "agents.md" for item in manifest["moved"])

    preserved = result.run_dir / "obfuscated_agent_docs" / "original" / "agents.md"
    assert preserved.exists()
    assert sentinel in preserved.read_text(encoding="utf-8")


def test_obfuscation_baseline_commit_keeps_diff_clean(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    sentinel = "SENTINEL_AGENT_DOC"
    (target / "agents.md").write_text(f"{sentinel}\n", encoding="utf-8")

    dummy_binary = _make_dummy_codex_binary(tmp_path, sentinel=sentinel)
    cfg = _make_cfg(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        dummy_binary=dummy_binary,
        allow_edits=True,
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="write",
            obfuscate_agent_docs=True,
        ),
    )

    assert result.exit_code == 0
    assert not result.report_validation_errors

    diff_numstat_path = result.run_dir / "diff_numstat.json"
    assert diff_numstat_path.exists()
    assert json.loads(diff_numstat_path.read_text(encoding="utf-8")) == []

    assert (result.run_dir / "preprocess_commit.txt").exists()
