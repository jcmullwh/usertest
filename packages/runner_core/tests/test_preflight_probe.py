from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from runner_core import RunnerConfig, RunRequest, find_repo_root, run_once
from runner_core.runner import _build_preflight_command_list


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
    wrapper.write_text(
        f"#!/bin/sh\nexec \"{sys.executable}\" \"{script}\" \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def test_preflight_command_list_excludes_domain_specific_defaults() -> None:
    commands = _build_preflight_command_list(RunRequest(repo="x"))
    assert "ffmpeg" not in commands
    assert "ffprobe" not in commands


def test_preflight_command_list_merges_and_dedupes_request_commands() -> None:
    commands = _build_preflight_command_list(
        RunRequest(repo="x", preflight_commands=("ffmpeg", "rg", "custom"))
    )
    assert "ffmpeg" in commands
    assert "custom" in commands
    assert commands.count("rg") == 1


def test_run_once_writes_preflight_probe_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    cmd_dir = tmp_path / "bin"
    cmd_dir.mkdir()
    if os.name == "nt":
        (cmd_dir / "dummycmd.cmd").write_text("@echo off\necho ok\n", encoding="utf-8")
        monkeypatch.setenv(
            "PATHEXT",
            f"{os.environ.get('PATHEXT', '')};.CMD",
        )
    else:
        dummy = cmd_dir / "dummycmd"
        dummy.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        dummy.chmod(dummy.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("PATH", f"{cmd_dir}{os.pathsep}{os.environ.get('PATH', '')}")

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
            preflight_commands=("dummycmd",),
        ),
    )

    assert result.exit_code == 0
    preflight_path = result.run_dir / "preflight.json"
    payload = json.loads(preflight_path.read_text(encoding="utf-8"))
    assert "dummycmd" in payload.get("probe_commands", [])
    assert payload.get("commands", {}).get("dummycmd") is True
    assert "ffmpeg" not in payload.get("commands", {})
    diagnostics = payload.get("command_diagnostics", {})
    assert isinstance(diagnostics, dict)
    assert diagnostics.get("dummycmd", {}).get("status") == "present"
    python_probe = payload.get("python_interpreter")
    assert isinstance(python_probe, dict)
    assert isinstance(python_probe.get("candidates"), list)
    python_diag = diagnostics.get("python", {})
    assert isinstance(python_diag, dict)
    assert "reason_code" in python_diag
    assert "resolved_path" in python_diag
    caps = payload.get("capabilities", {})
    assert isinstance(caps, dict)
    assert caps.get("shell_commands", {}).get("status") == "unknown"


def test_run_once_fails_fast_when_required_agent_binary_missing(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    missing_binary = "definitely-missing-agent-binary-for-usertest"
    cfg = RunnerConfig(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": missing_binary}},
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

    assert result.exit_code != 0
    assert result.report_validation_errors

    error_path = result.run_dir / "error.json"
    payload = json.loads(error_path.read_text(encoding="utf-8"))
    assert payload.get("type") == "AgentPreflightFailed"
    assert payload.get("subtype") == "binary_missing"
    assert payload.get("required_binary") == missing_binary


def test_run_once_warns_when_codex_personality_missing_model_messages(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    dummy_binary = _make_dummy_codex_binary(tmp_path)
    cfg = RunnerConfig(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        agents={
            "codex": {
                "binary": dummy_binary,
                "config_overrides": ['model_personality="pragmatic"'],
            }
        },
        policies={"write": {"codex": {"sandbox": "workspace-write", "allow_edits": True}}},
    )

    result = run_once(cfg, RunRequest(repo=str(target), agent="codex", policy="write"))

    assert result.exit_code == 0
    assert result.report_validation_errors == []
    payload = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    warnings = payload.get("warnings", [])
    assert isinstance(warnings, list)
    assert any(
        w.get("code") == "codex_model_messages_missing"
        for w in warnings
        if isinstance(w, dict)
    )


def test_run_once_fails_fast_when_shell_blocked_in_inspect_policy(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    cfg = RunnerConfig(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        agents={"gemini": {"binary": "gemini"}},
        policies={
            "safe": {
                "gemini": {
                    "allow_edits": False,
                    "sandbox": True,
                    "approval_mode": "default",
                    "allowed_tools": ["read_file"],
                }
            }
        },
    )

    result = run_once(cfg, RunRequest(repo=str(target), agent="gemini", policy="inspect"))

    assert result.exit_code != 0
    payload = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert payload.get("type") == "AgentPreflightFailed"
    assert payload.get("subtype") == "policy_block"


def test_run_once_marks_present_commands_as_blocked_by_policy_when_shell_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    cmd_dir = tmp_path / "bin"
    cmd_dir.mkdir()
    if os.name == "nt":
        (cmd_dir / "dummycmd.cmd").write_text("@echo off\necho ok\n", encoding="utf-8")
        monkeypatch.setenv("PATHEXT", f"{os.environ.get('PATHEXT', '')};.CMD")
    else:
        dummy = cmd_dir / "dummycmd"
        dummy.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        dummy.chmod(dummy.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{cmd_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    cfg = RunnerConfig(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        agents={"gemini": {"binary": "gemini"}},
        policies={
            "safe": {
                "gemini": {
                    "allow_edits": False,
                    "sandbox": True,
                    "approval_mode": "default",
                    "allowed_tools": ["read_file"],
                }
            }
        },
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="gemini",
            policy="safe",
            preflight_commands=("dummycmd",),
        ),
    )

    payload = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    diagnostics = payload.get("command_diagnostics", {})
    assert isinstance(diagnostics, dict)
    assert diagnostics.get("dummycmd", {}).get("status") == "blocked_by_policy"
    remediation = diagnostics.get("dummycmd", {}).get("remediation")
    assert isinstance(remediation, str)
    assert "Enable shell commands in policy" in remediation


def test_run_once_fails_fast_on_invalid_codex_reasoning_effort_override(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    dummy_binary = _make_dummy_codex_binary(tmp_path)
    cfg = RunnerConfig(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        agents={
            "codex": {
                "binary": dummy_binary,
                "config_overrides": ["model_reasoning_effort=xhigh"],
            }
        },
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )

    result = run_once(cfg, RunRequest(repo=str(target), agent="codex", policy="safe"))

    assert result.exit_code != 0
    error_payload = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_payload.get("type") == "AgentPreflightFailed"
    assert error_payload.get("subtype") == "invalid_agent_config"
    assert error_payload.get("code") == "codex_model_reasoning_effort_invalid"
    hint = error_payload.get("hint")
    assert isinstance(hint, str)
    assert "model_reasoning_effort=high" in hint

    preflight_payload = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    validation = preflight_payload.get("agent_config_validation", {})
    assert isinstance(validation, dict)
    assert validation.get("ok") is False


def test_run_once_fails_fast_when_required_preflight_command_missing(
    tmp_path: Path,
) -> None:
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
            preflight_required_commands=("definitely-missing-required-command",),
        ),
    )

    assert result.exit_code != 0
    payload = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert payload.get("type") == "AgentPreflightFailed"
    assert payload.get("subtype") == "required_command_unavailable"
