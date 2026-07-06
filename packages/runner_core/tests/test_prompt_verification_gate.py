from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_adapters.codex_cli import CodexExecResult

import runner_core.runner as runner_mod
from runner_core import RunnerConfig, RunRequest, run_once


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_dummy_codex_binary(tmp_path: Path) -> str:
    script = tmp_path / "dummy_codex_prompt.py"
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
                "    report = {'ok': 'yes'}",
                "    if out_path is not None:",
                "        Path(out_path).write_text(json.dumps(report) + '\\n', encoding='utf-8')",
                (
                    "    payload = {'id': '1', 'msg': {'type': 'agent_message', "
                    "'message': 'hi'}}"
                ),
                "    print(json.dumps(payload))",
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
        wrapper = tmp_path / "dummy_codex_prompt.cmd"
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

    wrapper = tmp_path / "dummy_codex_prompt.sh"
    wrapper.write_text(
        f"#!/bin/sh\nexec \"{sys.executable}\" \"{script}\" \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    return str(wrapper)


def _setup_runner_root(tmp_path: Path) -> Path:
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
        "\n".join(["---", "id: p", "name: P", "extends: null", "---", "Persona", ""]),
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
    _write(
        runner_root / "configs" / "prompt_templates" / "t.prompt.md",
        "\n".join(
            [
                "PROMPT",
                "",
                "## Preflight summary",
                "",
                "${preflight_summary_md}",
                "",
                "## Environment",
                "",
                "```json",
                "${environment_json}",
                "```",
                "",
            ]
        ),
    )
    _write(
        runner_root / "configs" / "report_schemas" / "s.schema.json",
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "string"}},
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
    )
    return runner_root


def _setup_target_repo(tmp_path: Path) -> Path:
    target = tmp_path / "target"
    target.mkdir()
    _write(target / "README.md", "# hi\n")
    _write(target / "USERS.md", "# Users\n")
    return target


def test_prompt_includes_final_handoff_verification_and_codex_workspace_sandbox_note(
    tmp_path: Path,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_binary(tmp_path)

    verify_cmd = "python -c 'import sys; sys.exit(0)'"

    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": dummy_binary}},
        policies={"write": {"codex": {"sandbox": "workspace-write", "allow_edits": True}}},
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="write",
            persona_id="p",
            mission_id="m",
            verification_commands=(verify_cmd,),
            verification_reuse_mode="auto",
        ),
    )

    assert result.exit_code == 0

    prompt_text = (result.run_dir / "prompt.txt").read_text(encoding="utf-8")
    expected_wrapper = "verify_client.ps1" if os.name == "nt" else "verify_client.sh"
    assert "Final handoff verification" in prompt_text
    assert expected_wrapper in prompt_text
    assert "timeout_seconds: 10800" in prompt_text
    assert verify_cmd not in prompt_text
    assert "it blocks until verification finishes" in prompt_text
    assert "it must pass before you finish" in prompt_text
    assert "Codex workspace sandbox is enabled" in prompt_text
    assert "--exec-backend docker" in prompt_text


def test_codex_uses_model_instructions_file_for_large_append_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_binary(tmp_path)

    monkeypatch.setattr(
        runner_mod,
        "_probe_commands_local",
        lambda commands, **kwargs: (
            {cmd: True for cmd in commands},
            {"command_probe_details": {cmd: {"present": True} for cmd in commands}},
        ),
    )
    monkeypatch.setattr(
        runner_mod,
        "_probe_agent_cli_version",
        lambda **kwargs: {
            "ok": True,
            "argv": [str(kwargs.get("binary", "codex")), "--version"],
            "returncode": 0,
            "stdout": "codex test stub\n",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        runner_mod,
        "_agent_auth_present_local",
        lambda **kwargs: (True, "test_stub"),
    )

    captured: dict[str, object] = {}

    def _fake_run_codex_exec(**kwargs: object) -> CodexExecResult:
        captured["config_overrides"] = list(kwargs.get("config_overrides", ()))
        raw_events_path = kwargs["raw_events_path"]
        last_message_path = kwargs["last_message_path"]
        stderr_path = kwargs["stderr_path"]
        assert isinstance(raw_events_path, Path)
        assert isinstance(last_message_path, Path)
        assert isinstance(stderr_path, Path)
        payload = {"id": "1", "msg": {"type": "agent_message", "message": "ok"}}
        raw_events_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        last_message_path.write_text(json.dumps({"ok": "yes"}) + "\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return CodexExecResult(
            argv=["codex"],
            exit_code=0,
            raw_events_path=raw_events_path,
            last_message_path=last_message_path,
            stderr_path=stderr_path,
        )

    monkeypatch.setattr(runner_mod, "run_codex_exec", _fake_run_codex_exec)

    result = run_once(
        RunnerConfig(
            repo_root=runner_root,
            runs_dir=tmp_path / "runs",
            agents={"codex": {"binary": dummy_binary}},
            policies={"write": {"codex": {"sandbox": "workspace-write", "allow_edits": True}}},
        ),
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="write",
            persona_id="p",
            mission_id="m",
            agent_append_system_prompt="X" * 50_000,
        ),
    )

    assert result.exit_code == 0
    overrides = [str(item) for item in captured["config_overrides"]]
    assert any(item.startswith("model_instructions_file=") for item in overrides)
    assert not any(item.startswith("developer_instructions=") for item in overrides)


def test_run_once_uses_agent_default_model_when_request_model_is_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_binary(tmp_path)

    monkeypatch.setattr(
        runner_mod,
        "_probe_commands_local",
        lambda commands, **kwargs: (
            {cmd: True for cmd in commands},
            {"command_probe_details": {cmd: {"present": True} for cmd in commands}},
        ),
    )
    monkeypatch.setattr(
        runner_mod,
        "_probe_agent_cli_version",
        lambda **kwargs: {
            "ok": True,
            "argv": [str(kwargs.get("binary", "codex")), "--version"],
            "returncode": 0,
            "stdout": "codex test stub\n",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        runner_mod,
        "_agent_auth_present_local",
        lambda **kwargs: (True, "test_stub"),
    )

    captured: dict[str, object] = {}

    def _fake_run_codex_exec(**kwargs: object) -> CodexExecResult:
        captured["model"] = kwargs.get("model")
        raw_events_path = kwargs["raw_events_path"]
        last_message_path = kwargs["last_message_path"]
        stderr_path = kwargs["stderr_path"]
        assert isinstance(raw_events_path, Path)
        assert isinstance(last_message_path, Path)
        assert isinstance(stderr_path, Path)
        payload = {"id": "1", "msg": {"type": "agent_message", "message": "ok"}}
        raw_events_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        last_message_path.write_text(json.dumps({"ok": "yes"}) + "\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return CodexExecResult(
            argv=["codex", "--model", str(kwargs.get("model"))],
            exit_code=0,
            raw_events_path=raw_events_path,
            last_message_path=last_message_path,
            stderr_path=stderr_path,
        )

    monkeypatch.setattr(runner_mod, "run_codex_exec", _fake_run_codex_exec)

    result = run_once(
        RunnerConfig(
            repo_root=runner_root,
            runs_dir=tmp_path / "runs",
            agents={"codex": {"binary": dummy_binary, "default_model": "gpt-5.5"}},
            policies={"write": {"codex": {"sandbox": "workspace-write", "allow_edits": True}}},
        ),
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="write",
            persona_id="p",
            mission_id="m",
        ),
    )

    assert result.exit_code == 0
    assert captured["model"] == "gpt-5.5"
    target_ref = json.loads((result.run_dir / "target_ref.json").read_text(encoding="utf-8"))
    assert target_ref["model"] == "gpt-5.5"
    assert target_ref["model_source"] == "agent_default"


def test_verification_broker_handoff_uses_shared_launcher_resolution() -> None:
    launcher = runner_mod.resolve_verification_launcher(command_prefix=[])
    contract = runner_mod.resolve_verification_broker_contract(
        command_prefix=[],
        exec_backend="local",
        validated_python_executable=sys.executable,
        verification_timeout_seconds=None,
        verification_command_count=1,
    )
    argv = runner_mod._verification_shell_argv(command_prefix=[], command="echo ok")

    assert argv[: len(launcher.shell_argv_prefix)] == list(launcher.shell_argv_prefix)

    command = runner_mod._verification_broker_client_command(
        run_dir=Path("/tmp/run"),
        run_dir_mount="/run_dir",
        workspace_dir=Path("/tmp/workspace"),
        contract=contract,
    )
    assert launcher.broker_wrapper_name in command


def test_run_once_fails_fast_when_final_broker_launcher_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_binary(tmp_path)

    monkeypatch.setattr(
        runner_mod,
        "_probe_verification_broker_launcher",
        lambda **_: (
            SimpleNamespace(executable="sh"),
            {
                "present": True,
                "usable": False,
                "resolved_path": r"C:\blocked\sh.exe",
                "reason_code": "blocked",
                "reason": "Access is denied",
            },
        ),
    )

    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": dummy_binary}},
        policies={"write": {"codex": {"sandbox": "workspace-write", "allow_edits": True}}},
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="write",
            persona_id="p",
            mission_id="m",
            verification_commands=("python -c 'import sys; sys.exit(0)'",),
            verification_reuse_mode="auto",
        ),
    )

    assert result.exit_code == 1
    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "VerificationBrokerLauncherUnavailable"
    assert error_obj.get("subtype") == "verification_broker_launcher_unavailable"
    assert error_obj.get("launcher") == "sh"
    assert "launcher=`sh`" in str(error_obj.get("message", ""))
    assert "Access is denied" in str(error_obj.get("message", ""))


def test_run_once_fails_fast_when_final_broker_python_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_binary(tmp_path)

    monkeypatch.setattr(
        runner_mod,
        "_probe_verification_broker_launcher",
        lambda **_: (
            SimpleNamespace(executable="sh"),
            {
                "present": False,
                "usable": False,
                "resolved_path": None,
                "reason_code": "not_found",
                "reason": (
                    "required dependency `python` unavailable in the verification runtime: "
                    "`python` was not found in the verification runtime."
                ),
                "failed_dependency": "python",
                "runtime_dependencies": {
                    "sh": {"present": True, "usable": True, "resolved_path": "/bin/sh"},
                    "python": {
                        "present": False,
                        "usable": False,
                        "resolved_path": None,
                        "reason_code": "not_found",
                        "reason": "`python` was not found in the verification runtime.",
                    },
                },
            },
        ),
    )

    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": dummy_binary}},
        policies={"write": {"codex": {"sandbox": "workspace-write", "allow_edits": True}}},
    )

    result = run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="write",
            persona_id="p",
            mission_id="m",
            verification_commands=("python -c 'import sys; sys.exit(0)'",),
            verification_reuse_mode="auto",
        ),
    )

    assert result.exit_code == 1
    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "VerificationBrokerLauncherUnavailable"
    assert error_obj.get("failed_dependency") == "python"
    assert "required dependency `python`" in str(error_obj.get("message", ""))
    runtime_dependencies = error_obj.get("runtime_dependencies")
    assert isinstance(runtime_dependencies, dict)
    assert runtime_dependencies["python"]["usable"] is False
