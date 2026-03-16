from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import runner_core.python_runtime as runtime_mod
import runner_core.runner as runner_mod
from runner_core import RunnerConfig, RunRequest, find_repo_root, run_once
from runner_core.execution_backend import ExecutionBackendContext

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


def _context_probe_with_runtime_metadata(
    context_probe: dict[str, Any],
    runtime_selection: runtime_mod.PythonRuntimeSelection,
) -> dict[str, Any]:
    probe = dict(context_probe)
    if not probe.get("passed", False):
        return probe
    metadata = probe.get("metadata")
    if isinstance(metadata, dict) and metadata.get("executable"):
        return probe
    selected = runtime_selection.selected
    if selected is None:
        return probe
    probe["metadata"] = {
        "executable": selected.path,
        "version": selected.version,
    }
    return probe


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


@dataclass(frozen=True)
class _ToolchainMatrixCase:
    scenario_name: str
    verification_commands: tuple[str, ...]
    preflight_required_commands: tuple[str, ...] = ()
    expect_exit_code: int = 1
    expect_error_subtype: str | None = None
    expect_python_status: str | None = None
    expect_python_reason_code: str | None = None
    expect_python_reason_type: str | None = None
    expect_python_validation_enabled: bool = False
    expect_python_validation_reason_code: str | None = None
    expect_pdm_status: str | None = None
    expect_pdm_reason_code: str | None = None
    expect_pdm_reason_type: str | None = None
    expect_verification_calls: int = 0
    expect_verification_artifacts: bool = False
    expect_report_artifacts: bool = False
    use_dummy_agent: bool = False


def _run_fixture_backed_toolchain_case(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario_name: str,
    verification_commands: tuple[str, ...],
    preflight_required_commands: tuple[str, ...] = (),
    use_dummy_agent: bool = False,
) -> tuple[Any, runtime_mod.PythonRuntimeSelection, list[str | None], dict[str, int]]:
    scenario = _load_scenario(scenario_name)
    runtime_fixture = scenario.get("python_runtime")
    if not isinstance(runtime_fixture, dict):
        raise AssertionError(f"{scenario_name} missing python_runtime fixture")

    _patch_local_probe(monkeypatch, scenario=scenario)
    runtime_selection = _runtime_selection(runtime_fixture)
    monkeypatch.setattr(
        runner_mod,
        "select_python_runtime",
        lambda *args, **kwargs: runtime_selection,
    )

    context_probe = scenario.get("context_probe")
    if isinstance(context_probe, dict):
        monkeypatch.setattr(
            runner_mod,
            "_probe_python_context_capability",
            lambda *args, **kwargs: _context_probe_with_runtime_metadata(
                dict(context_probe),
                runtime_selection.selected,
            ),
        )

    pip_probe = scenario.get("pip_probe")
    if isinstance(pip_probe, dict):
        monkeypatch.setattr(
            runner_mod,
            "probe_pip_module",
            lambda *args, **kwargs: dict(pip_probe),
        )

    pytest_probe = scenario.get("pytest_probe")
    if isinstance(pytest_probe, dict):
        monkeypatch.setattr(
            runner_mod,
            "probe_pytest_module",
            lambda *args, **kwargs: dict(pytest_probe),
        )

    verification_calls = {"count": 0}
    verification_python_executables: list[str | None] = []

    def _fake_run_verification_commands(
        *,
        run_dir: Path,
        attempt_number: int,
        command_prefix: list[str],
        commands: list[str],
        cwd: Path,
        timeout_seconds: float | None,
        python_executable: str | None,
        python_toolchain_capability: dict[str, Any] | None = None,
        env_overrides: dict[str, str] | None = None,
        execution_shell: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del (
            command_prefix,
            cwd,
            timeout_seconds,
            python_toolchain_capability,
            env_overrides,
            execution_shell,
            kwargs,
        )
        verification_calls["count"] += 1
        verification_python_executables.append(python_executable)
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
    target = tmp_path / f"target_repo_{scenario_name}"
    target.mkdir()
    (target / "README.md").write_text("# hi\n", encoding="utf-8")
    _install_no_requirements_mission(target)

    agent_binary = (
        _make_dummy_codex_binary(tmp_path)
        if use_dummy_agent
        else str(tmp_path / "missing_codex.exe")
    )
    cfg = RunnerConfig(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": agent_binary}},
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="safe",
            verification_commands=verification_commands,
            preflight_required_commands=preflight_required_commands,
        ),
    )

    return result, runtime_selection, verification_python_executables, verification_calls


def _context_probe_with_runtime_metadata(
    context_probe: dict[str, Any],
    selected_runtime: runtime_mod.PythonRuntimeCandidate | None,
) -> dict[str, Any]:
    probe = dict(context_probe)
    if not bool(probe.get("passed", False)):
        return probe
    metadata = probe.get("metadata")
    if isinstance(metadata, dict):
        return probe
    if selected_runtime is None:
        return probe
    probe["metadata"] = {
        "executable": selected_runtime.path,
        "version": selected_runtime.version,
        "prefix": selected_runtime.prefix,
        "base_prefix": selected_runtime.base_prefix,
        "real_prefix": selected_runtime.real_prefix,
        "exec_prefix": selected_runtime.exec_prefix,
        "base_exec_prefix": selected_runtime.base_exec_prefix,
        "virtual_env": selected_runtime.virtual_env,
    }
    return probe


@pytest.mark.parametrize(
    "case",
    [
        _ToolchainMatrixCase(
            scenario_name="healthy_pass",
            verification_commands=("pytest -q",),
            expect_exit_code=0,
            expect_python_status="present",
            expect_python_validation_enabled=True,
            expect_pdm_status="present",
            expect_verification_calls=1,
            expect_verification_artifacts=True,
            expect_report_artifacts=True,
            use_dummy_agent=True,
        ),
        _ToolchainMatrixCase(
            scenario_name="broken_stdlib_runtime",
            verification_commands=("pytest -q",),
            expect_exit_code=1,
            expect_error_subtype="python_unavailable",
            expect_python_status="unusable",
            expect_python_reason_code="missing_stdlib",
            expect_python_reason_type="runtime",
            expect_python_validation_enabled=False,
            expect_python_validation_reason_code="missing_stdlib",
        ),
        _ToolchainMatrixCase(
            scenario_name="windowsapps_venv_unusable",
            verification_commands=("pytest -q",),
            expect_exit_code=1,
            expect_error_subtype="python_unavailable",
            expect_python_status="unusable",
            expect_python_reason_code="windowsapps_alias",
            expect_python_reason_type="discovery",
            expect_python_validation_enabled=False,
            expect_python_validation_reason_code="windowsapps_alias",
        ),
        _ToolchainMatrixCase(
            scenario_name="wrapper_present_unusable",
            verification_commands=("pytest -q",),
            preflight_required_commands=("pdm",),
            expect_exit_code=1,
            expect_error_subtype="required_command_unavailable",
            expect_python_status="present",
            expect_python_validation_enabled=True,
            expect_pdm_status="unusable",
            expect_pdm_reason_code="probe_failed",
            expect_pdm_reason_type="runtime",
        ),
        _ToolchainMatrixCase(
            scenario_name="context_mismatch",
            verification_commands=("pytest -q",),
            expect_exit_code=1,
            expect_error_subtype="python_unavailable",
            expect_python_status="unusable",
            expect_python_reason_code="launch_failed",
            expect_python_reason_type="execution",
            expect_python_validation_enabled=False,
            expect_python_validation_reason_code="launch_failed",
        ),
    ],
    ids=lambda case: case.scenario_name,
)
def test_python_toolchain_capability_matrix_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: _ToolchainMatrixCase,
) -> None:
    result, runtime_selection, verification_python_executables, verification_calls = (
        _run_fixture_backed_toolchain_case(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            scenario_name=case.scenario_name,
            verification_commands=case.verification_commands,
            preflight_required_commands=case.preflight_required_commands,
            use_dummy_agent=case.use_dummy_agent,
        )
    )

    assert result.exit_code == case.expect_exit_code

    preflight = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    python_diag = preflight.get("command_diagnostics", {}).get("python", {})
    if case.expect_python_status is not None:
        assert python_diag.get("status") == case.expect_python_status
    if case.expect_python_reason_code is not None:
        assert python_diag.get("reason_code") == case.expect_python_reason_code
    if case.expect_python_reason_type is not None:
        assert python_diag.get("reason_type") == case.expect_python_reason_type

    python_validation = preflight.get("python_validation", {})
    assert python_validation.get("required") is True
    assert python_validation.get("enabled") is case.expect_python_validation_enabled
    assert python_validation.get("reason_code") == case.expect_python_validation_reason_code

    selected_runtime_path = runtime_selection.selected.path if runtime_selection.selected else None
    if case.expect_python_validation_enabled and selected_runtime_path is not None:
        assert python_validation.get("validated_python_executable") == selected_runtime_path
    else:
        assert python_validation.get("validated_python_executable") is None

    pdm_diag = preflight.get("command_diagnostics", {}).get("pdm", {})
    if case.expect_pdm_status is not None:
        assert pdm_diag.get("status") == case.expect_pdm_status
    if case.expect_pdm_reason_code is not None:
        assert pdm_diag.get("reason_code") == case.expect_pdm_reason_code
    if case.expect_pdm_reason_type is not None:
        assert pdm_diag.get("reason_type") == case.expect_pdm_reason_type

    assert verification_calls["count"] == case.expect_verification_calls
    if case.expect_verification_calls:
        assert verification_python_executables == [selected_runtime_path]
    else:
        assert verification_python_executables == []

    verification_path = result.run_dir / "verification.json"
    attempt_verification_path = result.run_dir / "verification" / "attempt1" / "verification.json"
    assert verification_path.exists() is case.expect_verification_artifacts
    assert attempt_verification_path.exists() is case.expect_verification_artifacts
    assert (result.run_dir / "report.json").exists() is case.expect_report_artifacts
    assert (result.run_dir / "report.md").exists() is case.expect_report_artifacts

    if case.expect_error_subtype is None:
        assert not (result.run_dir / "error.json").exists()
        return

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "AgentPreflightFailed"
    assert error_obj.get("subtype") == case.expect_error_subtype
    if case.expect_error_subtype == "required_command_unavailable":
        failure_diag = error_obj.get("failures", {}).get("pdm", {})
        assert failure_diag.get("status") == case.expect_pdm_status
        assert failure_diag.get("reason_code") == case.expect_pdm_reason_code
        assert failure_diag.get("reason_type") == case.expect_pdm_reason_type
    else:
        error_python_diag = error_obj.get("preflight", {}).get("command_diagnostics", {}).get(
            "python", {}
        )
        if case.expect_python_status is not None:
            assert error_python_diag.get("status") == case.expect_python_status
        if case.expect_python_reason_code is not None:
            assert error_python_diag.get("reason_code") == case.expect_python_reason_code
        if case.expect_python_reason_type is not None:
            assert error_python_diag.get("reason_type") == case.expect_python_reason_type


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
    toolchain = preflight.get("python_toolchain", {})
    assert toolchain.get("runtime", {}).get("selected") is None
    assert toolchain.get("commands", {}).get("python", {}).get("reason_code") == "launch_failed"
    assert toolchain.get("validation", {}).get("reason_code") == "launch_failed"
    assert preflight.get("commands", {}).get("python") is True
    python_diag = preflight.get("command_diagnostics", {}).get("python", {})
    assert python_diag.get("status") == "unusable"
    assert python_diag.get("reason_code") == "launch_failed"
    assert python_diag.get("reason_type") == "execution"
    assert "sandbox/policy access" in str(python_diag.get("remediation", ""))
    assert preflight.get("python_runtime", {}).get("selected") is None
    python_validation = preflight.get("python_validation", {})
    assert python_validation.get("required") is True
    assert python_validation.get("enabled") is False
    assert python_validation.get("reason_code") == "launch_failed"
    assert python_validation.get("reason_type") == "execution"
    assert python_validation.get("validated_python_executable") is None

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "AgentPreflightFailed"
    assert error_obj.get("subtype") == "python_unavailable"
    assert "full CPython interpreter" in str(error_obj.get("hint", ""))
    python_status = (
        error_obj.get("preflight", {})
        .get("command_diagnostics", {})
        .get("python", {})
        .get("status")
    )
    assert python_status == "unusable"
    error_validation = error_obj.get("preflight", {}).get("python_validation", {})
    assert error_validation.get("required") is True
    assert error_validation.get("enabled") is False
    assert error_validation.get("reason_code") == "launch_failed"
    assert error_validation.get("reason_type") == "execution"
    assert error_validation.get("validated_python_executable") is None
    rejected = error_obj.get("preflight", {}).get("python_runtime", {}).get("rejected", [])
    assert isinstance(rejected, list)
    assert rejected and rejected[0].get("reason_code") == "launch_failed"


def test_two_stage_python_preflight_blocks_verification_on_context_mismatch(
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
    verification_calls = {"count": 0}

    def _fake_run_verification_commands(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        verification_calls["count"] += 1
        return {"schema_version": 1, "passed": True, "commands": []}

    monkeypatch.setattr(runner_mod, "_run_verification_commands", _fake_run_verification_commands)

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
    assert verification_calls["count"] == 0
    preflight = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    python_validation = preflight.get("python_validation", {})
    assert python_validation.get("enabled") is False
    assert python_validation.get("validated_python_executable") is None


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
    toolchain = preflight.get("python_toolchain", {})
    assert toolchain.get("commands", {}).get("python", {}).get("reason_code") == "windowsapps_alias"
    assert toolchain.get("runtime", {}).get("selected") is None
    python_diag = preflight.get("command_diagnostics", {}).get("python", {})
    assert python_diag.get("status") == "unusable"
    assert python_diag.get("reason_code") == "windowsapps_alias"
    assert python_diag.get("reason_type") == "discovery"
    assert "full CPython interpreter" in str(python_diag.get("remediation", ""))
    python_validation = preflight.get("python_validation", {})
    assert python_validation.get("required") is True
    assert python_validation.get("enabled") is False
    assert python_validation.get("reason_code") == "windowsapps_alias"
    assert python_validation.get("reason_type") == "discovery"
    assert python_validation.get("validated_python_executable") is None

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "AgentPreflightFailed"
    assert error_obj.get("subtype") == "python_unavailable"
    error_diag = error_obj.get("preflight", {}).get("command_diagnostics", {}).get("python", {})
    assert error_diag.get("status") == "unusable"
    assert error_diag.get("reason_code") == "windowsapps_alias"
    assert error_diag.get("reason_type") == "discovery"
    rejected = error_obj.get("preflight", {}).get("python_runtime", {}).get("rejected", [])
    assert isinstance(rejected, list)
    assert any(item.get("reason_code") == "windowsapps_alias" for item in rejected)


def test_two_stage_python_preflight_reports_fully_missing_toolchain_inline_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _load_scenario("fully_missing_toolchain")
    runtime_fixture = scenario.get("python_runtime")
    if not isinstance(runtime_fixture, dict):
        raise AssertionError("fully_missing_toolchain missing python_runtime fixture")

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
            verification_commands=("python -m pytest -q",),
        ),
    )

    assert result.exit_code == 1
    preflight = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    assert preflight.get("command_diagnostics", {}).get("python", {}).get("status") == "missing"
    assert (
        preflight.get("command_diagnostics", {}).get("python", {}).get("reason_code")
        == "not_found"
    )
    assert preflight.get("python_runtime", {}).get("selected") is None
    python_validation = preflight.get("python_validation", {})
    assert python_validation.get("required") is True
    assert python_validation.get("enabled") is False
    assert python_validation.get("reason_code") == "python_unavailable"
    assert python_validation.get("reason_type") == "discovery"
    assert python_validation.get("validated_python_executable") is None

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "AgentPreflightFailed"
    assert error_obj.get("subtype") == "python_unavailable"
    assert "Python" in str(error_obj.get("message", ""))


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
    selected_runtime_path = (
        runtime_fixture.get("selected", {}).get("path")
        if isinstance(runtime_fixture.get("selected"), dict)
        else None
    )

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
            "metadata": {
                "executable": selected_runtime_path,
                "version": runtime_fixture.get("selected", {}).get("version"),
            },
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
    toolchain = preflight.get("python_toolchain", {})
    assert toolchain.get("modules", {}).get("pip", {}).get("passed") is True
    assert toolchain.get("modules", {}).get("pytest", {}).get("reason_code") == "pytest_missing"
    assert preflight.get("pip_probe", {}).get("passed") is True
    assert preflight.get("pip_probe", {}).get("reason_type") is None
    assert preflight.get("pytest_probe", {}).get("reason_code") == "pytest_missing"
    assert preflight.get("pytest_probe", {}).get("reason_type") == "dependency"
    python_validation = preflight.get("python_validation", {})
    assert python_validation.get("required") is True
    assert python_validation.get("enabled") is True
    assert python_validation.get("reason_code") is None
    assert python_validation.get("reason_type") is None
    assert (
        python_validation.get("validated_python_executable")
        == preflight.get("python_runtime", {}).get("selected", {}).get("path")
    )

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "AgentPreflightFailed"
    assert error_obj.get("subtype") == "pytest_unavailable"
    pytest_reason_code = (
        error_obj.get("preflight", {}).get("pytest_probe", {}).get("reason_code")
    )
    assert pytest_reason_code == "pytest_missing"
    pytest_reason_type = (
        error_obj.get("preflight", {}).get("pytest_probe", {}).get("reason_type")
    )
    assert pytest_reason_type == "dependency"
    error_validation = error_obj.get("preflight", {}).get("python_validation", {})
    assert error_validation.get("required") is True
    assert error_validation.get("enabled") is True
    assert error_validation.get("reason_code") is None
    assert error_validation.get("reason_type") is None
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
    verification_python_executables: list[str | None] = []

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
            "metadata": {
                "executable": selected_runtime_path,
                "version": runtime_fixture.get("selected", {}).get("version"),
            },
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
        **kwargs: Any,
    ) -> dict[str, Any]:
        del command_prefix, cwd, timeout_seconds, env_overrides, execution_shell, kwargs
        verification_python_executables.append(python_executable)
        assert python_executable == selected_runtime_path
        artifacts_dir_rel = Path("verification") / f"attempt{attempt_number}"
        artifacts_dir = run_dir / artifacts_dir_rel
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "schema_version": 1,
            "passed": True,
            "wall_seconds": 0.01,
            "artifacts_dir": str(artifacts_dir_rel),
            "python_executable": python_executable,
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
    toolchain = preflight.get("python_toolchain", {})
    assert toolchain.get("runtime", {}).get("selected", {}).get("path") == selected_runtime_path
    assert toolchain.get("modules", {}).get("pip", {}).get("passed") is True
    assert toolchain.get("modules", {}).get("pytest", {}).get("passed") is True
    assert preflight.get("command_diagnostics", {}).get("python", {}).get("status") == "present"
    assert preflight.get("command_diagnostics", {}).get("python", {}).get("reason_type") is None
    assert (
        preflight.get("python_interpreter", {}).get("selected", {}).get("resolved_path")
        == selected_runtime_path
    )
    assert preflight.get("python_runtime", {}).get("selected", {}).get("source") == "sandbox_env"
    assert (
        preflight.get("python_runtime", {}).get("selected", {}).get("path")
        == selected_runtime_path
    )
    assert preflight.get("pip_probe", {}).get("passed") is True
    assert preflight.get("pip_probe", {}).get("reason_type") is None
    assert preflight.get("pytest_probe", {}).get("passed") is True
    assert preflight.get("pytest_probe", {}).get("reason_type") is None
    python_validation = preflight.get("python_validation", {})
    assert python_validation.get("required") is True
    assert python_validation.get("enabled") is True
    assert python_validation.get("reason_code") is None
    assert python_validation.get("reason_type") is None
    assert python_validation.get("validated_python_executable") == selected_runtime_path
    assert verification_python_executables == [selected_runtime_path]
    verification = json.loads(
        (result.run_dir / "verification" / "attempt1" / "verification.json").read_text(
            encoding="utf-8"
        )
    )
    assert verification.get("python_executable") == selected_runtime_path


def test_validate_python_capability_clears_host_runtime_hints_for_docker_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_environment: dict[str, str] = {}
    observed_flags: dict[str, bool] = {}
    expected_selection = runtime_mod.PythonRuntimeSelection(selected=None, candidates=tuple())

    def _fake_select_python_runtime(
        *,
        workspace_dir: Path,
        timeout_seconds: float = 5.0,
        include_where_fallbacks: bool = True,
        include_sys_executable: bool = True,
        environment: dict[str, str] | None = None,
    ) -> runtime_mod.PythonRuntimeSelection:
        del workspace_dir, timeout_seconds
        observed_environment.clear()
        observed_flags["include_where_fallbacks"] = include_where_fallbacks
        observed_flags["include_sys_executable"] = include_sys_executable
        if isinstance(environment, dict):
            observed_environment.update(environment)
        return expected_selection

    monkeypatch.setattr(runner_mod, "select_python_runtime", _fake_select_python_runtime)

    capability = runner_mod._validate_python_capability(
        workspace_dir=tmp_path,
        verification_commands=("python -m pytest -q",),
        command_prefix=["docker", "exec", "-i", "sandbox"],
        cwd=tmp_path,
        env_overrides={"PATH": "/usr/bin", "FOO": "bar"},
    )

    assert capability["runtime_selection"] == expected_selection
    assert observed_flags == {
        "include_where_fallbacks": False,
        "include_sys_executable": False,
    }
    assert observed_environment.get("FOO") == "bar"
    assert observed_environment.get("PATH") == "/usr/bin"
    assert observed_environment.get("VIRTUAL_ENV") == ""
    assert observed_environment.get("USERTEST_PYTHON") == ""
    assert observed_environment.get("PDM_PYTHON") == ""
    assert observed_environment.get("UV_PYTHON") == ""
    assert observed_environment.get("PYTHONHOME") == ""
    assert observed_environment.get("__PYVENV_LAUNCHER__") == ""
    assert observed_environment.get("CONDA_PREFIX") == ""


def test_validate_python_capability_prefers_context_verified_runtime_for_docker_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_selection = runtime_mod.PythonRuntimeSelection(
        selected=runtime_mod.PythonRuntimeCandidate(
            source="virtual_env",
            path=r"C:\external\venv\Scripts\python.exe",
            present=True,
            usable=True,
            version="3.13.2",
            executable=r"C:\external\venv\Scripts\python.exe",
        ),
        candidates=(
            runtime_mod.PythonRuntimeCandidate(
                source="virtual_env",
                path=r"C:\external\venv\Scripts\python.exe",
                present=True,
                usable=True,
                version="3.13.2",
                executable=r"C:\external\venv\Scripts\python.exe",
            ),
        ),
    )
    monkeypatch.setattr(
        runner_mod,
        "select_python_runtime",
        lambda *args, **kwargs: runtime_selection,
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
            "metadata": {
                "executable": "/usr/bin/python3.13",
                "version": "3.13.2",
                "prefix": "/usr",
                "base_prefix": "/usr",
                "real_prefix": None,
                "exec_prefix": "/usr",
                "base_exec_prefix": "/usr",
                "virtual_env": "/workspace/.venv",
            },
        },
    )

    capability = runner_mod._validate_python_capability(
        workspace_dir=tmp_path,
        verification_commands=("python -m pytest -q",),
        command_prefix=["docker", "exec", "-i", "sandbox"],
        cwd=tmp_path,
        env_overrides={"PATH": "/usr/bin"},
    )

    runtime_summary = capability["runtime_summary"]
    selected = runtime_summary.get("selected", {})
    assert selected.get("source") == "context_verified"
    assert selected.get("path") == "/usr/bin/python3.13"
    rejected = runtime_summary.get("rejected", [])
    assert isinstance(rejected, list)
    assert any(
        item.get("path") == r"C:\external\venv\Scripts\python.exe"
        and item.get("reason_code") == "context_mismatch"
        for item in rejected
        if isinstance(item, dict)
    )
    validation = capability["validation"]
    assert validation.get("required") is True
    assert validation.get("enabled") is True
    assert validation.get("validated_python_executable") == "/usr/bin/python3.13"


def test_two_stage_python_preflight_docker_context_probe_failure_skips_runtime_metadata_probes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_local_probe(monkeypatch, scenario=_load_scenario("healthy_pass"))

    runtime_selection = runtime_mod.PythonRuntimeSelection(
        selected=runtime_mod.PythonRuntimeCandidate(
            source="virtual_env",
            path=r"C:\external\venv\Scripts\python.exe",
            present=True,
            usable=True,
            version="3.13.2",
            executable=r"C:\external\venv\Scripts\python.exe",
        ),
        candidates=(
            runtime_mod.PythonRuntimeCandidate(
                source="virtual_env",
                path=r"C:\external\venv\Scripts\python.exe",
                present=True,
                usable=True,
                version="3.13.2",
                executable=r"C:\external\venv\Scripts\python.exe",
            ),
        ),
    )
    monkeypatch.setattr(
        runner_mod,
        "select_python_runtime",
        lambda *args, **kwargs: runtime_selection,
    )
    monkeypatch.setattr(
        runner_mod,
        "_probe_python_context_capability",
        lambda *args, **kwargs: {
            "passed": False,
            "reason_code": "access_denied",
            "reason_type": "execution",
            "reason": "Permission denied",
            "remediation": "Python execution is blocked in this environment.",
        },
    )

    pip_probe_calls = {"count": 0}
    pytest_probe_calls = {"count": 0}

    def _fake_probe_pip_module(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        pip_probe_calls["count"] += 1
        return {"passed": False, "reason_code": "pip_probe_failed"}

    def _fake_probe_pytest_module(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        pytest_probe_calls["count"] += 1
        return {"passed": False, "reason_code": "pytest_probe_failed"}

    monkeypatch.setattr(runner_mod, "probe_pip_module", _fake_probe_pip_module)
    monkeypatch.setattr(runner_mod, "probe_pytest_module", _fake_probe_pytest_module)
    monkeypatch.setattr(
        runner_mod,
        "prepare_execution_backend",
        lambda **kwargs: ExecutionBackendContext(
            sandbox_instance=None,
            command_prefix=["docker", "exec", "-i", "sandbox"],
            workspace_mount="/workspace",
            run_dir_mount="/run_dir",
        ),
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
            verification_commands=("python -m pytest -q",),
        ),
    )

    assert result.exit_code == 1
    assert pip_probe_calls["count"] == 0
    assert pytest_probe_calls["count"] == 0

    preflight = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    assert preflight.get("pip_probe") is None
    assert preflight.get("pytest_probe") is None
    python_validation = preflight.get("python_validation", {})
    assert python_validation.get("required") is True
    assert python_validation.get("enabled") is False
    assert python_validation.get("validated_python_executable") is None


def test_two_stage_python_preflight_optional_verification_bypass_skips_pip_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_local_probe(monkeypatch, scenario=_load_scenario("healthy_pass"))

    runtime_selection = runtime_mod.PythonRuntimeSelection(
        selected=runtime_mod.PythonRuntimeCandidate(
            source="command_python",
            path=sys.executable,
            present=True,
            usable=True,
            version="3.13.2",
            executable=sys.executable,
        ),
        candidates=(
            runtime_mod.PythonRuntimeCandidate(
                source="command_python",
                path=sys.executable,
                present=True,
                usable=True,
                version="3.13.2",
                executable=sys.executable,
            ),
        ),
    )
    monkeypatch.setattr(
        runner_mod,
        "select_python_runtime",
        lambda *args, **kwargs: runtime_selection,
    )

    pip_probe_calls = {"count": 0}

    def _fake_probe_pip_module(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        pip_probe_calls["count"] += 1
        return {"passed": True, "reason_code": None}

    monkeypatch.setattr(runner_mod, "probe_pip_module", _fake_probe_pip_module)

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
            verification_commands=(),
        ),
    )

    assert result.exit_code == 1
    assert pip_probe_calls["count"] == 0

    preflight = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    assert preflight.get("pip_probe") is None
    python_validation = preflight.get("python_validation", {})
    assert python_validation.get("required") is False
    assert python_validation.get("enabled") is True
    assert python_validation.get("validated_python_executable") == sys.executable


def test_run_once_reports_missing_required_pdm_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy = _load_scenario("healthy_pass")
    runtime_fixture = healthy.get("python_runtime")
    healthy_preflight = healthy.get("preflight")
    if not isinstance(runtime_fixture, dict) or not isinstance(healthy_preflight, dict):
        raise AssertionError("healthy_pass fixture missing required sections")

    commands = dict(healthy_preflight.get("commands", {}))
    commands["pdm"] = False
    details = dict(healthy_preflight.get("command_probe_details", {}))
    details["pdm"] = {
        "command": "pdm",
        "resolved_path": None,
        "present": False,
        "usable": False,
        "reason_code": "not_found",
        "reason": "`pdm` was not found on PATH.",
    }
    scenario = {
        "preflight": {
            "commands": commands,
            "command_probe_details": details,
            "python_interpreter": healthy_preflight.get("python_interpreter"),
        },
        "context_probe": healthy.get("context_probe"),
    }

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
        agents={"codex": {"binary": _make_dummy_codex_binary(tmp_path)}},
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="safe",
            preflight_required_commands=("pdm",),
        ),
    )

    assert result.exit_code == 1
    preflight = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    pdm_diag = preflight.get("command_diagnostics", {}).get("pdm", {})
    assert pdm_diag.get("status") == "missing"
    assert pdm_diag.get("reason_code") == "not_found"
    assert "Install `pdm`" in str(pdm_diag.get("remediation", ""))

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "AgentPreflightFailed"
    assert error_obj.get("subtype") == "required_command_unavailable"


def test_two_stage_python_preflight_reports_fully_missing_toolchain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = {
        "preflight": {
            "commands": {
                "python": False,
                "python3": False,
                "py": False,
            },
            "command_probe_details": {
                "python": {
                    "command": "python",
                    "resolved_path": None,
                    "present": False,
                    "usable": False,
                    "reason_code": "not_found",
                    "reason": "`python` was not found on PATH.",
                },
                "python3": {
                    "command": "python3",
                    "resolved_path": None,
                    "present": False,
                    "usable": False,
                    "reason_code": "not_found",
                    "reason": "`python3` was not found on PATH.",
                },
                "py": {
                    "command": "py",
                    "resolved_path": None,
                    "present": False,
                    "usable": False,
                    "reason_code": "not_found",
                    "reason": "`py` was not found on PATH.",
                },
            },
            "python_interpreter": {
                "selected": None,
                "candidates": [
                    {
                        "command": "python",
                        "resolved_path": None,
                        "present": False,
                        "usable": False,
                        "reason_code": "not_found",
                        "reason": "`python` was not found on PATH.",
                        "version": None,
                        "executable": None,
                    },
                    {
                        "command": "python3",
                        "resolved_path": None,
                        "present": False,
                        "usable": False,
                        "reason_code": "not_found",
                        "reason": "`python3` was not found on PATH.",
                        "version": None,
                        "executable": None,
                    },
                    {
                        "command": "py",
                        "resolved_path": None,
                        "present": False,
                        "usable": False,
                        "reason_code": "not_found",
                        "reason": "`py` was not found on PATH.",
                        "version": None,
                        "executable": None,
                    },
                ],
                "rejected": [
                    {
                        "command": "python",
                        "resolved_path": None,
                        "present": False,
                        "usable": False,
                        "reason_code": "not_found",
                        "reason": "`python` was not found on PATH.",
                        "version": None,
                        "executable": None,
                    },
                    {
                        "command": "python3",
                        "resolved_path": None,
                        "present": False,
                        "usable": False,
                        "reason_code": "not_found",
                        "reason": "`python3` was not found on PATH.",
                        "version": None,
                        "executable": None,
                    },
                    {
                        "command": "py",
                        "resolved_path": None,
                        "present": False,
                        "usable": False,
                        "reason_code": "not_found",
                        "reason": "`py` was not found on PATH.",
                        "version": None,
                        "executable": None,
                    },
                ],
            },
        },
    }
    runtime_fixture = {
        "selected": None,
        "candidates": [
            {
                "source": "workspace_venv",
                "path": "/workspace/.venv/bin/python",
                "present": False,
                "usable": False,
                "reason_code": "not_found",
                "reason": "Interpreter not found at: /workspace/.venv/bin/python",
            },
            {
                "source": "command_python",
                "path": "/missing/python",
                "present": False,
                "usable": False,
                "reason_code": "not_found",
                "reason": "Interpreter not found at: /missing/python",
            },
            {
                "source": "command_python3",
                "path": "/missing/python3",
                "present": False,
                "usable": False,
                "reason_code": "not_found",
                "reason": "Interpreter not found at: /missing/python3",
            },
        ],
    }

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
        agents={"codex": {"binary": _make_dummy_codex_binary(tmp_path)}},
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="safe",
            verification_commands=('python -c "print(\'ok\')"',),
        ),
    )

    assert result.exit_code == 1
    preflight = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    assert preflight.get("python_runtime", {}).get("selected") is None
    python_validation = preflight.get("python_validation", {})
    assert python_validation.get("required") is True
    assert python_validation.get("enabled") is False
    assert python_validation.get("reason_code") == "not_found"
    assert python_validation.get("reason_type") == "discovery"
    assert python_validation.get("validated_python_executable") is None

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "AgentPreflightFailed"
    assert error_obj.get("subtype") == "python_unavailable"


def test_run_once_uses_validated_python_for_live_shell_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_runtime_path = str(tmp_path / "toolchain" / "python")
    scenario = {
        "preflight": {
            "commands": {
                "python": False,
                "python3": False,
                "py": False,
            },
            "command_probe_details": {
                "python": {
                    "command": "python",
                    "resolved_path": None,
                    "present": False,
                    "usable": False,
                    "reason_code": "not_found",
                    "reason": "`python` was not found on PATH.",
                },
                "python3": {
                    "command": "python3",
                    "resolved_path": None,
                    "present": False,
                    "usable": False,
                    "reason_code": "not_found",
                    "reason": "`python3` was not found on PATH.",
                },
                "py": {
                    "command": "py",
                    "resolved_path": None,
                    "present": False,
                    "usable": False,
                    "reason_code": "not_found",
                    "reason": "`py` was not found on PATH.",
                },
            },
            "python_interpreter": {
                "selected": None,
                "candidates": [],
                "rejected": [],
            },
        },
    }
    runtime_fixture = {
        "selected": {
            "source": "sandbox_env",
            "path": selected_runtime_path,
            "present": True,
            "usable": True,
            "reason_code": None,
            "reason": None,
            "version": "3.13.2",
            "executable": selected_runtime_path,
        },
        "candidates": [
            {
                "source": "sandbox_env",
                "path": selected_runtime_path,
                "present": True,
                "usable": True,
                "reason_code": None,
                "reason": None,
                "version": "3.13.2",
                "executable": selected_runtime_path,
            }
        ],
    }
    pip_probe = {
        "command": "python -m pip --version",
        "argv": [selected_runtime_path, "-m", "pip", "--version"],
        "python_executable": selected_runtime_path,
        "cwd": str(tmp_path),
        "passed": True,
        "exit_code": 0,
        "timed_out": False,
        "reason_code": None,
        "remediation": None,
        "stdout_tail": "pip 25.0",
        "stderr_tail": "",
        "exception": None,
    }

    _patch_local_probe(monkeypatch, scenario=scenario)
    monkeypatch.setattr(
        runner_mod,
        "select_python_runtime",
        lambda *args, **kwargs: _runtime_selection(runtime_fixture),
    )
    monkeypatch.setattr(runner_mod, "probe_pip_module", lambda *args, **kwargs: dict(pip_probe))
    monkeypatch.setattr(
        runner_mod,
        "_probe_python_context_capability",
        lambda *args, **kwargs: {
            "command": "python -c <health_probe>",
            "effective_command": f"'{selected_runtime_path}' -c <health_probe>",
            "argv": ["sh", "-lc", f"'{selected_runtime_path}' -c '<health_probe>'"],
            "cwd": str(tmp_path),
            "passed": True,
            "exit_code": 0,
            "timed_out": False,
            "reason_code": None,
            "reason_type": None,
            "reason": None,
            "remediation": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "exception": None,
            "metadata": {
                "executable": selected_runtime_path,
                "version": "3.13.2",
                "prefix": str(tmp_path / "toolchain"),
                "base_prefix": str(tmp_path / "toolchain"),
                "real_prefix": None,
                "exec_prefix": str(tmp_path / "toolchain"),
                "base_exec_prefix": str(tmp_path / "toolchain"),
                "virtual_env": None,
            },
        },
    )

    subprocess_calls: list[list[str]] = []

    class _Proc:
        def __init__(self, argv: list[str]) -> None:
            self.args = list(argv)
            self.returncode = 0
            self.stdout = "ok\n"
            self.stderr = ""

    def _fake_run(argv: list[str], **kwargs: Any) -> _Proc:
        del kwargs
        subprocess_calls.append(list(argv))
        return _Proc(argv)

    monkeypatch.setattr(runner_mod.subprocess, "run", _fake_run)

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
            verification_commands=('python -c "print(\'ok\')"',),
        ),
    )

    assert result.exit_code == 0

    preflight = json.loads((result.run_dir / "preflight.json").read_text(encoding="utf-8"))
    assert (
        preflight.get("python_runtime", {}).get("selected", {}).get("path")
        == selected_runtime_path
    )
    assert (
        preflight.get("python_context_probe", {}).get("metadata", {}).get("executable")
        == selected_runtime_path
    )
    assert (
        preflight.get("python_validation", {}).get("validated_python_executable")
        == selected_runtime_path
    )

    verification = json.loads(
        (result.run_dir / "verification" / "attempt1" / "verification.json").read_text(
            encoding="utf-8"
        )
    )
    assert verification.get("python_executable") == selected_runtime_path
    command_entry = verification.get("commands", [])[0]
    effective_command = str(command_entry.get("effective_command"))
    assert selected_runtime_path in effective_command
    assert not effective_command.lstrip().startswith("python ")
