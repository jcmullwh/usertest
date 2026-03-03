from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

import runner_core.python_runtime as runtime_mod
import runner_core.runner as runner_mod
from runner_core import RunnerConfig, RunRequest, find_repo_root, run_once

_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "python_capability_regressions.json"


def _load_scenario(name: str) -> dict[str, Any]:
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    scenario = payload.get(name)
    if not isinstance(scenario, dict):
        raise AssertionError(f"Missing fixture scenario: {name}")
    return scenario


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


def _runtime_candidate(raw: dict[str, Any]) -> runtime_mod.PythonRuntimeCandidate:
    return runtime_mod.PythonRuntimeCandidate(
        source=str(raw.get("source", "")),
        path=str(raw.get("path", "")),
        present=bool(raw.get("present", False)),
        usable=bool(raw.get("usable", False)),
        reason_code=raw.get("reason_code") if isinstance(raw.get("reason_code"), str) else None,
        reason=raw.get("reason") if isinstance(raw.get("reason"), str) else None,
        version=raw.get("version") if isinstance(raw.get("version"), str) else None,
        executable=raw.get("executable") if isinstance(raw.get("executable"), str) else None,
    )


def _runtime_selection(raw: dict[str, Any]) -> runtime_mod.PythonRuntimeSelection:
    selected_raw = raw.get("selected")
    candidates_raw = raw.get("candidates")
    if not isinstance(candidates_raw, list):
        raise AssertionError("runtime fixture missing candidates list")
    selected = _runtime_candidate(selected_raw) if isinstance(selected_raw, dict) else None
    candidates = tuple(
        _runtime_candidate(item) for item in candidates_raw if isinstance(item, dict)
    )
    return runtime_mod.PythonRuntimeSelection(selected=selected, candidates=candidates)


def _patch_local_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scenario: dict[str, Any],
) -> None:
    preflight = scenario.get("preflight")
    if not isinstance(preflight, dict):
        raise AssertionError("scenario missing preflight fixture")

    command_truth = preflight.get("commands")
    command_details = preflight.get("command_probe_details")
    python_interpreter = preflight.get("python_interpreter")
    command_truth_map = command_truth if isinstance(command_truth, dict) else {}
    command_detail_map = command_details if isinstance(command_details, dict) else {}

    def _fake_probe_commands_local(
        commands: list[str],
        *,
        workspace_dir: Path | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> tuple[dict[str, bool], dict[str, Any]]:
        del workspace_dir, env_overrides
        out: dict[str, bool] = {}
        details: dict[str, dict[str, Any]] = {}
        for cmd in commands:
            val = command_truth_map.get(cmd)
            usable = bool(val) if isinstance(val, bool) else True
            out[cmd] = usable
            raw = command_detail_map.get(cmd)
            if isinstance(raw, dict):
                details[cmd] = dict(raw)
            else:
                details[cmd] = {
                    "command": cmd,
                    "resolved_path": f"/mock/bin/{cmd}",
                    "present": bool(usable),
                    "usable": bool(usable),
                    "reason_code": None if usable else "not_found",
                    "reason": None if usable else f"`{cmd}` was not found on PATH.",
                }

        meta: dict[str, Any] = {"command_probe_details": details}
        if isinstance(python_interpreter, dict):
            meta["python_interpreter"] = python_interpreter
        return out, meta

    monkeypatch.setattr(runner_mod, "_probe_commands_local", _fake_probe_commands_local)


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
                "        'confidence_signals': {'found': ['Has files'], 'missing': []},",
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


def test_two_stage_python_preflight_detects_context_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _load_scenario("context_mismatch")
    runtime_fixture = scenario.get("python_runtime")
    if not isinstance(runtime_fixture, dict):
        raise AssertionError("context_mismatch missing python_runtime fixture")

    _patch_local_probe(monkeypatch, scenario=scenario)
    monkeypatch.setattr(
        runner_mod,
        "select_python_runtime",
        lambda *args, **kwargs: _runtime_selection(runtime_fixture),
    )

    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    cfg = RunnerConfig(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": str(tmp_path / "missing_codex.exe")}},
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="safe",
            verification_commands=("pytest -q",),
        ),
    )

    assert result.exit_code == 1
    preflight = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    assert preflight.get("commands", {}).get("python") is True
    assert preflight.get("command_diagnostics", {}).get("python", {}).get("status") == "unusable"
    assert (
        preflight.get("command_diagnostics", {}).get("python", {}).get("reason_code")
        == "launch_failed"
    )
    assert preflight.get("python_runtime", {}).get("selected") is None

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "AgentPreflightFailed"
    assert error_obj.get("subtype") == "python_unavailable"
    python_status = (
        error_obj.get("preflight", {})
        .get("command_diagnostics", {})
        .get("python", {})
        .get("status")
    )
    assert python_status == "unusable"
    rejected = error_obj.get("preflight", {}).get("python_runtime", {}).get("rejected", [])
    assert isinstance(rejected, list)
    assert rejected and rejected[0].get("reason_code") == "launch_failed"


def test_two_stage_python_preflight_classifies_windowsapps_backed_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _load_scenario("windowsapps_venv_unusable")
    runtime_fixture = scenario.get("python_runtime")
    if not isinstance(runtime_fixture, dict):
        raise AssertionError("windowsapps_venv_unusable missing python_runtime fixture")

    _patch_local_probe(monkeypatch, scenario=scenario)
    monkeypatch.setattr(
        runner_mod,
        "select_python_runtime",
        lambda *args, **kwargs: _runtime_selection(runtime_fixture),
    )

    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    cfg = RunnerConfig(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": str(tmp_path / "missing_codex.exe")}},
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="safe",
            verification_commands=("pytest -q",),
        ),
    )

    assert result.exit_code == 1
    preflight = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    python_diag = preflight.get("command_diagnostics", {}).get("python", {})
    assert python_diag.get("status") == "unusable"
    assert python_diag.get("reason_code") == "windowsapps_alias"
    assert "full CPython interpreter" in str(python_diag.get("remediation", ""))

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "AgentPreflightFailed"
    assert error_obj.get("subtype") == "python_unavailable"
    rejected = error_obj.get("preflight", {}).get("python_runtime", {}).get("rejected", [])
    assert isinstance(rejected, list)
    assert any(item.get("reason_code") == "windowsapps_alias" for item in rejected)


def test_two_stage_python_preflight_classifies_partial_runtime_pytest_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _load_scenario("partial_runtime_pytest_missing")
    runtime_fixture = scenario.get("python_runtime")
    pip_probe = scenario.get("pip_probe")
    pytest_probe = scenario.get("pytest_probe")
    if not isinstance(runtime_fixture, dict):
        raise AssertionError("partial_runtime_pytest_missing missing python_runtime fixture")
    if not isinstance(pip_probe, dict) or not isinstance(pytest_probe, dict):
        raise AssertionError("partial_runtime_pytest_missing missing probe fixtures")

    _patch_local_probe(monkeypatch, scenario=scenario)
    monkeypatch.setattr(
        runner_mod,
        "select_python_runtime",
        lambda *args, **kwargs: _runtime_selection(runtime_fixture),
    )
    monkeypatch.setattr(
        runner_mod, "probe_pip_module", lambda *args, **kwargs: dict(pip_probe)
    )
    monkeypatch.setattr(
        runner_mod, "probe_pytest_module", lambda *args, **kwargs: dict(pytest_probe)
    )
    monkeypatch.setattr(
        runner_mod,
        "_probe_python_context_capability",
        lambda *args, **kwargs: {
            "passed": True,
            "reason_code": None,
            "reason_type": None,
            "reason": None,
            "remediation": None,
        },
    )

    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    cfg = RunnerConfig(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": str(tmp_path / "missing_codex.exe")}},
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="safe",
            verification_commands=("pytest -q",),
        ),
    )

    assert result.exit_code == 1
    preflight = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    assert preflight.get("pytest_probe", {}).get("reason_code") == "pytest_missing"

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "AgentPreflightFailed"
    assert error_obj.get("subtype") == "pytest_unavailable"
    pytest_reason_code = (
        error_obj.get("preflight", {}).get("pytest_probe", {}).get("reason_code")
    )
    assert pytest_reason_code == "pytest_missing"
    assert "pip install -U pytest" in str(error_obj.get("hint", ""))


def test_two_stage_python_preflight_pass_path_records_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _load_scenario("healthy_pass")
    runtime_fixture = scenario.get("python_runtime")
    pip_probe = scenario.get("pip_probe")
    pytest_probe = scenario.get("pytest_probe")
    if not isinstance(runtime_fixture, dict):
        raise AssertionError("healthy_pass missing python_runtime fixture")
    if not isinstance(pip_probe, dict) or not isinstance(pytest_probe, dict):
        raise AssertionError("healthy_pass missing probe fixtures")
    selected_runtime_path = (
        runtime_fixture.get("selected", {}).get("path")
        if isinstance(runtime_fixture.get("selected"), dict)
        else None
    )
    if not isinstance(selected_runtime_path, str) or not selected_runtime_path.strip():
        raise AssertionError("healthy_pass missing selected runtime path")

    _patch_local_probe(monkeypatch, scenario=scenario)
    runtime_selection = _runtime_selection(runtime_fixture)
    selection_calls = {"count": 0}

    def _fake_select_python_runtime(
        *args: Any, **kwargs: Any
    ) -> runtime_mod.PythonRuntimeSelection:
        del args, kwargs
        selection_calls["count"] += 1
        return runtime_selection

    monkeypatch.setattr(
        runner_mod,
        "select_python_runtime",
        _fake_select_python_runtime,
    )
    monkeypatch.setattr(
        runner_mod, "probe_pip_module", lambda *args, **kwargs: dict(pip_probe)
    )
    monkeypatch.setattr(
        runner_mod, "probe_pytest_module", lambda *args, **kwargs: dict(pytest_probe)
    )
    monkeypatch.setattr(
        runner_mod,
        "_probe_python_context_capability",
        lambda *args, **kwargs: {
            "passed": True,
            "reason_code": None,
            "reason_type": None,
            "reason": None,
            "remediation": None,
        },
    )

    def _fake_run_verification_commands(
        *,
        run_dir: Path,
        attempt_number: int,
        command_prefix: list[str],
        commands: list[str],
        cwd: Path,
        timeout_seconds: float | None,
        python_executable: str | None,
        env_overrides: dict[str, str] | None = None,
        execution_shell: str | None = None,
    ) -> dict[str, Any]:
        del command_prefix, cwd, timeout_seconds, env_overrides, execution_shell
        assert python_executable == selected_runtime_path
        artifacts_dir_rel = Path("verification") / f"attempt{attempt_number}"
        artifacts_dir = run_dir / artifacts_dir_rel
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "schema_version": 1,
            "passed": True,
            "wall_seconds": 0.01,
            "artifacts_dir": str(artifacts_dir_rel),
            "commands": [
                {
                    "command": cmd,
                    "argv": ["sh", "-lc", cmd],
                    "exit_code": 0,
                    "timed_out": False,
                    "rejected_sentinel": False,
                }
                for cmd in commands
            ],
        }
        (artifacts_dir / "verification.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return summary

    monkeypatch.setattr(runner_mod, "_run_verification_commands", _fake_run_verification_commands)

    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target_repo"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    cfg = RunnerConfig(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": _make_dummy_codex_binary(tmp_path)}},
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="safe",
            verification_commands=("pytest -q",),
        ),
    )

    assert result.exit_code == 0
    assert not (result.run_dir / "error.json").exists()
    assert selection_calls["count"] == 1

    preflight = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    assert preflight.get("command_diagnostics", {}).get("python", {}).get("status") == "present"
    assert preflight.get("python_runtime", {}).get("selected", {}).get("source") == "sandbox_env"
    assert preflight.get("pytest_probe", {}).get("passed") is True
