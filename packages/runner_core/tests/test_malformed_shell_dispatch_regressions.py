from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import runner_core.runner as runner_mod

_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "malformed_shell_dispatch_regressions.json"
)


def _load_fixture_section(name: str) -> list[dict[str, Any]]:
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    section = payload.get(name)
    if not isinstance(section, list):
        raise AssertionError(f"fixture section {name!r} is missing")
    out = [item for item in section if isinstance(item, dict)]
    if len(out) != len(section):
        raise AssertionError(f"fixture section {name!r} contains non-object rows")
    return out


@pytest.mark.parametrize(
    "scenario",
    _load_fixture_section("blocked"),
    ids=lambda item: str(item.get("name", "blocked")),
)
def test_run_verification_commands_blocks_malformed_dispatch_variants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: dict[str, Any],
) -> None:
    host_os = str(scenario.get("host_os", "Linux"))
    command_prefix = list(scenario.get("command_prefix", []))
    command = str(scenario["command"])
    expected_kind = str(scenario["expected_kind"])
    expected_stderr_fragment = str(scenario["expected_stderr_fragment"])

    calls: list[tuple[list[str], dict[str, Any]]] = []

    def _unexpected_run(argv: list[str], **kwargs: Any) -> object:  # pragma: no cover
        calls.append((list(argv), dict(kwargs)))
        raise AssertionError("subprocess.run should not be called for blocked dispatch variants")

    monkeypatch.setattr(
        runner_mod,
        "_is_windows",
        lambda: host_os.strip().lower().startswith("windows"),
    )
    monkeypatch.setattr(runner_mod.subprocess, "run", _unexpected_run)

    summary = runner_mod._run_verification_commands(
        run_dir=tmp_path,
        workspace_dir=tmp_path,
        attempt_number=1,
        commands=[command],
        command_prefix=command_prefix,
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=None,
    )

    assert calls == []
    assert summary.get("passed") is False
    commands = summary.get("commands")
    assert isinstance(commands, list) and len(commands) == 1
    result = commands[0]
    assert result.get("command") == command
    assert result.get("exit_code") == 126
    assert result.get("dispatch_blocked") is True
    assert result.get("rejected_sentinel") is False
    validation = result.get("dispatch_validation")
    assert isinstance(validation, dict)
    assert validation.get("kind") == expected_kind
    assert expected_stderr_fragment in (result.get("stderr_tail") or "")


@pytest.mark.parametrize(
    "scenario",
    _load_fixture_section("allowed"),
    ids=lambda item: str(item.get("name", "allowed")),
)
def test_run_verification_commands_allows_shell_native_variants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: dict[str, Any],
) -> None:
    host_os = str(scenario.get("host_os", "Linux"))
    command_prefix = list(scenario.get("command_prefix", []))
    command = str(scenario["command"])
    expected_argv_prefix = [str(item) for item in scenario.get("expected_argv_prefix", [])]

    calls: list[list[str]] = []

    class _Proc:
        def __init__(self, argv: list[str]) -> None:
            self.returncode = 0
            self.stdout = "ok\n"
            self.stderr = ""
            self.argv = argv

    def _fake_run(argv: list[str], **_kwargs: Any) -> _Proc:
        calls.append(list(argv))
        return _Proc(list(argv))

    monkeypatch.setattr(
        runner_mod,
        "_is_windows",
        lambda: host_os.strip().lower().startswith("windows"),
    )
    monkeypatch.setattr(runner_mod.subprocess, "run", _fake_run)

    summary = runner_mod._run_verification_commands(
        run_dir=tmp_path,
        workspace_dir=tmp_path,
        attempt_number=1,
        commands=[command],
        command_prefix=command_prefix,
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=None,
    )

    assert summary.get("passed") is True
    assert len(calls) == 1
    assert calls[0][: len(expected_argv_prefix)] == expected_argv_prefix
    assert calls[0][-1] == command

    commands = summary.get("commands")
    assert isinstance(commands, list) and len(commands) == 1
    result = commands[0]
    assert result.get("dispatch_blocked") is False
    assert result.get("dispatch_validation") is None
    assert result.get("exit_code") == 0
    assert result.get("effective_command") == command


@pytest.mark.parametrize(
    "scenario",
    _load_fixture_section("metadata"),
    ids=lambda item: str(item.get("name", "metadata")),
)
def test_shell_metadata_propagates_to_prompt_summary_and_broker_command(
    scenario: dict[str, Any],
) -> None:
    host_os = str(scenario["host_os"])
    exec_backend = str(scenario["exec_backend"])
    command_prefix = [str(item) for item in scenario.get("command_prefix", [])]
    expected_shell = str(scenario["expected_shell"])
    expected_summary_fragment = str(scenario["expected_summary_fragment"])
    expected_wrapper = str(scenario["expected_wrapper"])
    expected_broker_prefix = str(scenario["expected_broker_prefix"])

    execution_shell = runner_mod._execution_shell_family(
        exec_backend=exec_backend,
        host_os=host_os,
    )
    assert execution_shell == expected_shell

    summary_md = runner_mod._format_preflight_summary_md(
        execution_shell=execution_shell,
        shell_status="unknown",
        python_runtime_summary={"selected": None},
        python_toolchain_capability={
            "toolchain_status": "not_required",
            "interpreter_usable": True,
        },
        pip_probe=None,
        pytest_probe=None,
        command_diagnostics={},
        verification_commands=["pytest -q"],
        verification_timeout_seconds=None,
        verification_reuse_mode="auto",
        verification_broker_command="placeholder",
        agent="codex",
        codex_sandbox_mode="workspace-write",
    )
    assert expected_summary_fragment in summary_md

    launcher = runner_mod.resolve_verification_launcher(
        command_prefix=command_prefix,
        is_windows=host_os.strip().lower().startswith("windows"),
    )
    assert launcher.broker_wrapper_name == expected_wrapper

    broker_command = runner_mod.render_verification_broker_command(
        client_root_for_agent="/run_dir/verification_broker/client",
        launcher=launcher,
    )
    assert broker_command.startswith(expected_broker_prefix)
    assert expected_wrapper in broker_command
