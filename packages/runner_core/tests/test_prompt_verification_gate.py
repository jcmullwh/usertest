from __future__ import annotations

import json
import os
import stat
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_adapters.codex_cli import (
    CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS,
    CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES,
    CodexExecResult,
    CodexLoginStatusResult,
)

import runner_core.runner as runner_mod
from runner_core import RunnerConfig, RunRequest, run_once
from runner_core.codex_execpolicy import (
    CONTROLLED_CODEX_NON_ROUTING_CONFIG_OVERRIDES,
    CONTROLLED_CODEX_WINDOWS_SANDBOX_CONFIG_OVERRIDE,
)


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
                "    if argv[-2:] == ['login', 'status']:",
                "        print('Logged in using ChatGPT')",
                "        return 0",
                "    out_path: str | None = None",
                "    if '--output-last-message' in argv:",
                "        idx = argv.index('--output-last-message')",
                "        if idx + 1 < len(argv):",
                "            out_path = argv[idx + 1]",
                "    report = {'ok': 'yes'}",
                "    if out_path is not None:",
                "        Path(out_path).write_text(json.dumps(report) + '\\n', encoding='utf-8')",
                ("    payload = {'id': '1', 'msg': {'type': 'agent_message', 'message': 'hi'}}"),
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
                    f'"{sys.executable}" "{script}" %*',
                    "exit /b %ERRORLEVEL%",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return str(wrapper)

    wrapper = tmp_path / "dummy_codex_prompt.sh"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_binary(tmp_path)

    verify_cmd = "python -c 'import sys; sys.exit(0)'"
    raw_history_sentinel = "RAW_HISTORICAL_ARTIFACT_DUMP_SENTINEL"
    raw_progress_sentinel = "RAW_PROGRESS_LOG_SENTINEL"
    timing_profile = {
        "schema_version": 1,
        "run_count": 9,
        "command_count": 27,
        "run_wall_seconds": {"count": 9, "p05": 62.0, "median": 510.0, "p95": 1330.0},
        "slowest_commands": [
            {
                "label": raw_history_sentinel,
                "command": "pytest --very-large-history",
                "wall_seconds": 1330.0,
                "artifact_path": "/raw/historical/verification.json",
            }
        ],
        "progress_log": raw_progress_sentinel * 1000,
        "recommendations": {
            "history_state": "sufficient",
            "insufficient_history_reason": None,
            "recommended_initial_wait_seconds": 1330.0,
            "reasonable_check_after_seconds": 1330.0,
            "high_hang_guard_seconds": 10_800.0,
            "expected_duration_range_seconds": {
                "low": 62.0,
                "typical": 510.0,
                "high": 1330.0,
            },
            "basis": "sufficient_history_p95",
        },
    }
    monkeypatch.setattr(
        runner_mod,
        "build_verification_timing_profile",
        lambda **_: timing_profile,
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
            verification_commands=(verify_cmd,),
            verification_reuse_mode="auto",
        ),
    )

    assert result.exit_code == 0

    prompt_text = (result.run_dir / "prompt.txt").read_text(encoding="utf-8")
    assert "Final handoff verification" in prompt_text
    assert "timeout_seconds: 10800" in prompt_text
    assert verify_cmd not in prompt_text
    assert "runner-owned blocking wait" in prompt_text
    assert "do not launch or poll a verification command yourself" in prompt_text
    assert "return the required final JSON report" in prompt_text
    assert "The runner will request verification once" in prompt_text
    assert "finalize automatically if it passes" in prompt_text
    assert "re-enter the agent with one compact fix prompt" in prompt_text
    assert "timing guidance" in prompt_text
    assert (
        "expected duration range: p05=62s (~1.0 min), median=510s (~8.5 min), p95=1330s"
    ) in prompt_text
    assert "runner expected blocking wait" in prompt_text
    assert "the runner owns the wait and will only re-enter you if a fix is needed" in prompt_text
    assert "internal check cadence" in prompt_text
    assert "do not call verification hung until it exceeds" in prompt_text
    assert "or shows concrete failure evidence" in prompt_text
    assert "artifact paths to inspect after the result returns" in prompt_text
    assert "summary_path/artifacts_dir" in prompt_text
    assert "verification_timing_profile.json" in prompt_text
    assert raw_history_sentinel not in prompt_text
    assert raw_progress_sentinel not in prompt_text
    environment_json = prompt_text.split("```json\n", 1)[1].split("\n```", 1)[0]
    environment = json.loads(environment_json)
    compact_timing_profile = environment["verification_gate"]["timing_profile"]
    assert set(compact_timing_profile) == {"run_count", "command_count", "recommendations"}
    assert compact_timing_profile["run_count"] == 9
    assert compact_timing_profile["command_count"] == 27
    if os.name == "nt":
        assert "Codex unrestricted local sandbox mode is enabled" in prompt_text
        assert (
            "native Windows workspace-write cannot perform write missions reliably" in prompt_text
        )
        assert "runner-owned branch, diff, verification, review, and PR gates" in prompt_text
    else:
        assert "Codex workspace sandbox is enabled" in prompt_text
        assert "Do not treat a blocked shell command as proof" in prompt_text
        assert "allow_edits=true" in prompt_text
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
        captured["prompt"] = kwargs.get("prompt")
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
    assert "X" * 100 not in str(captured["prompt"])
    assert (result.run_dir / "prompt.txt").read_text(encoding="utf-8") == captured["prompt"]
    assert not (result.run_dir / "prompt.base.txt").exists()


def test_codex_report_followups_resume_exact_author_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_binary(tmp_path)
    session_id = "019f2cca-9011-7e32-88ae-6c25af578b49"
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
    resumes: list[str | None] = []

    def _fake_run_codex_exec(**kwargs: object) -> CodexExecResult:
        resumes.append(kwargs.get("resume_session_id"))
        raw_events_path = kwargs["raw_events_path"]
        last_message_path = kwargs["last_message_path"]
        stderr_path = kwargs["stderr_path"]
        assert isinstance(raw_events_path, Path)
        assert isinstance(last_message_path, Path)
        assert isinstance(stderr_path, Path)
        raw_events_path.write_text(
            json.dumps({"type": "thread.started", "thread_id": session_id}) + "\n",
            encoding="utf-8",
        )
        report = {"wrong": "shape"} if len(resumes) == 1 else {"ok": "yes"}
        last_message_path.write_text(json.dumps(report) + "\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return CodexExecResult(
            argv=["codex", "exec"] if len(resumes) == 1 else ["codex", "exec", "resume"],
            exit_code=0,
            raw_events_path=raw_events_path,
            last_message_path=last_message_path,
            stderr_path=stderr_path,
            thread_id=session_id,
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
            evidence_role="research",
            origin_stage="repro_research_verifier_continuation",
            parent_case_id="case:one",
        ),
    )

    assert result.exit_code == 0
    assert result.agent_session_id == session_id
    assert resumes == [None, session_id]
    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert [attempt["agent_session_id"] for attempt in attempts["attempts"]] == [
        session_id,
        session_id,
    ]
    assert attempts["attempts"][1]["continued_session"] is True
    target_ref = json.loads((result.run_dir / "target_ref.json").read_text(encoding="utf-8"))
    assert target_ref["backlog_lineage"] == {
        "evidence_role": "research",
        "origin_stage": "repro_research_verifier_continuation",
        "parent_case_id": "case:one",
    }


def test_codex_controlled_execpolicy_is_loaded_then_restored_before_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_windows = os.name == "nt"
    session_id = "019f2cca-9011-7e32-88ae-6c25af578b49"
    runner_root = _setup_runner_root(tmp_path)
    mission_path = runner_root / "configs" / "missions" / "m.mission.md"
    mission_path.write_text(
        mission_path.read_text(encoding="utf-8").replace(
            "execution_mode: single_pass_inline_report",
            "execution_mode: single_pass_inline_report\nrequires_shell: true",
        ),
        encoding="utf-8",
    )
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": "test-access",
                    "refresh_token": "test-refresh",
                    "account_id": "test-account",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    monkeypatch.setenv("OPENAI_API_KEY", "must-be-removed")
    monkeypatch.setenv("CODEX_API_KEY", "must-also-be-removed")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "must-also-be-removed")
    provider_parent_values = {
        "OPENAI_BASE_URL": "https://provider.invalid/v1",
        "OPENAI_API_BASE": "https://legacy-provider.invalid/v1",
        "OPENAI_ORG_ID": "org-id",
        "OPENAI_ORGANIZATION": "organization-id",
    }
    for name, value in provider_parent_values.items():
        monkeypatch.setenv(name, value)
    source_auth_before = (source_codex_home / "auth.json").read_bytes()
    source_config_path = source_codex_home / "config.toml"
    _write(source_config_path, 'model="host-user-config-sentinel"\n')
    source_config_before = source_config_path.read_bytes()
    _write(
        source_codex_home / "rules" / "default.rules",
        'prefix_rule(pattern=["host-safe"], decision="allow")\n',
    )
    target = _setup_target_repo(tmp_path)
    _write(target / "tools" / "scaffold" / "monorepo.toml", "[workspace]\n")
    assert not (target / "runs").exists()
    target_rules = target / ".codex" / "rules"
    target_rules.mkdir(parents=True)
    _write(
        target_rules / "target.rules",
        'prefix_rule(pattern=["target"], decision="forbidden")\n',
    )
    target_config = target / ".codex" / "config.toml"
    target_config_bytes = (
        b'model_provider="alternate"\r\n'
        b'chatgpt_base_url="https://alternate.invalid/backend-api"\r\n'
    )
    target_config.write_bytes(target_config_bytes)
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
    calls: list[dict[str, object]] = []

    def _fake_run_codex_exec(**kwargs: object) -> CodexExecResult:
        workspace = Path(str(kwargs["workspace_dir"]))
        captured["workspace"] = workspace
        captured["ignore_user_config"] = kwargs.get("ignore_user_config")
        captured["ignore_rules"] = kwargs.get("ignore_rules")
        captured["config_overrides"] = list(kwargs.get("config_overrides", ()))
        project_overrides = [
            str(value)
            for value in captured["config_overrides"]
            if str(value).startswith("projects.")
        ]
        assert len(project_overrides) == 1
        project_document = tomllib.loads(project_overrides[0])
        project_key = next(iter(project_document["projects"]))
        captured["project_trust_key"] = project_key
        host_config_document = tomllib.loads(source_config_path.read_text(encoding="utf-8"))
        host_projects = host_config_document.get("projects", {})
        if project_key not in host_projects:
            with source_config_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    "\n" + f"[projects.{json.dumps(project_key)}]\n" + 'trust_level = "trusted"\n'
                )
        assert (
            tomllib.loads(source_config_path.read_text(encoding="utf-8"))["projects"][project_key][
                "trust_level"
            ]
            == "trusted"
        )
        env_overrides = kwargs.get("env_overrides")
        captured["env_overrides"] = dict(env_overrides) if isinstance(env_overrides, dict) else {}
        calls.append(
            {
                "prompt": kwargs.get("prompt"),
                "config_overrides": list(kwargs.get("config_overrides", ())),
                "raw_events_path": kwargs.get("raw_events_path"),
                "ignore_user_config": kwargs.get("ignore_user_config"),
                "ignore_rules": kwargs.get("ignore_rules"),
                "env_overrides": dict(env_overrides) if isinstance(env_overrides, dict) else {},
                "resume_session_id": kwargs.get("resume_session_id"),
            }
        )
        controlled = workspace / ".codex" / "rules" / "usertest-controlled.rules"
        assert controlled.is_file() is (not native_windows)
        assert not (workspace / ".codex" / "rules" / "target.rules").exists()
        assert not (workspace / ".codex" / "config.toml").exists()
        assert not (workspace / "runs").exists()
        raw_events_path = kwargs["raw_events_path"]
        last_message_path = kwargs["last_message_path"]
        stderr_path = kwargs["stderr_path"]
        assert isinstance(raw_events_path, Path)
        assert isinstance(last_message_path, Path)
        assert isinstance(stderr_path, Path)
        if "agent_shell_probe" in raw_events_path.as_posix():
            command_events = [
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": (
                            '"C:\\\\Windows\\\\System32\\\\WindowsPowerShell\\\\v1.0'
                            '\\\\powershell.exe" -Command '
                            "'git rev-parse --is-inside-work-tree'"
                        ),
                        "aggregated_output": "true\n",
                        "exit_code": 0,
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": (
                            '"C:\\\\Windows\\\\System32\\\\WindowsPowerShell\\\\v1.0'
                            "\\\\powershell.exe\" -Command 'python --version'"
                        ),
                        "aggregated_output": "Python 3.14\n",
                        "exit_code": 0,
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": (
                            '"C:\\\\Windows\\\\System32\\\\WindowsPowerShell\\\\v1.0'
                            '\\\\powershell.exe" -Command "Write-Output \'shell_probe=ok\'"'
                        ),
                        "aggregated_output": "shell_probe=ok\n",
                        "exit_code": 0,
                    },
                },
            ]
            raw_events_path.write_text(
                "".join(json.dumps(event) + "\n" for event in command_events),
                encoding="utf-8",
            )
        else:
            raw_events_path.write_text(
                json.dumps({"id": "1", "msg": {"type": "agent_message", "message": "ok"}}) + "\n",
                encoding="utf-8",
            )
        last_message_path.write_text(json.dumps({"ok": "yes"}) + "\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        result_argv = ["codex", "exec"]
        if kwargs.get("ignore_user_config") is True:
            result_argv.append("--ignore-user-config")
        if kwargs.get("ignore_rules") is True:
            result_argv.append("--ignore-rules")
        result_argv.extend(["--sandbox", str(kwargs.get("sandbox"))])
        return CodexExecResult(
            argv=result_argv,
            exit_code=0,
            raw_events_path=raw_events_path,
            last_message_path=last_message_path,
            stderr_path=stderr_path,
            thread_id=session_id,
        )

    monkeypatch.setattr(runner_mod, "run_codex_exec", _fake_run_codex_exec)
    monkeypatch.setattr(
        "agent_adapters.shell_probe.run_codex_exec",
        _fake_run_codex_exec,
    )
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
            agent_append_system_prompt="RESEARCH MISSION SENTINEL",
            agent_user_prompt="RESEARCH MISSION SENTINEL",
            codex_execpolicy_allow_prefixes=(),
            codex_resume_session_id=session_id,
            keep_workspace=True,
        ),
    )

    assert result.exit_code == 0
    assert result.agent_session_id == session_id
    assert captured["ignore_user_config"] is True
    assert captured["ignore_rules"] is native_windows
    assert "sandbox_workspace_write.writable_roots=[]" in captured["config_overrides"]
    assert "notify=[]" in captured["config_overrides"]
    assert 'forced_login_method="chatgpt"' in captured["config_overrides"]
    assert 'model_provider="openai"' in captured["config_overrides"]
    assert any(str(value).startswith("projects.") for value in captured["config_overrides"])
    assert (
        CONTROLLED_CODEX_WINDOWS_SANDBOX_CONFIG_OVERRIDE in captured["config_overrides"]
    ) is native_windows
    assert captured["env_overrides"]["OPENAI_API_KEY"] == ""
    assert captured["env_overrides"]["CODEX_API_KEY"] == ""
    assert captured["env_overrides"]["CODEX_ACCESS_TOKEN"] == ""
    for name, value in provider_parent_values.items():
        assert captured["env_overrides"][name] == ""
        assert os.environ[name] == value
    assert Path(captured["env_overrides"]["CODEX_HOME"]).resolve() == source_codex_home.resolve()
    expected_project_key = (
        os.path.normcase(str(captured["workspace"].resolve()))
        if native_windows
        else str(captured["workspace"])
    )
    assert captured["project_trust_key"] == expected_project_key
    workspace = captured["workspace"]
    assert isinstance(workspace, Path)
    assert (workspace / ".codex" / "rules" / "target.rules").is_file()
    assert not (workspace / ".codex" / "rules" / "usertest-controlled.rules").exists()
    assert (workspace / ".codex" / "config.toml").read_bytes() == target_config_bytes
    assert not (workspace / "runs").exists()
    receipt_text = (result.run_dir / "codex_execpolicy_overlay.json").read_text(encoding="utf-8")
    assert "test-access" not in receipt_text
    assert "test-refresh" not in receipt_text
    receipt = json.loads(receipt_text)
    assert receipt["restore_status"] == "restored"
    assert receipt["restore_errors"] == []
    assert receipt["schema_version"] == 3
    assert receipt["runner_induced_project_trust_cleanup_verified"] is True
    assert receipt["runner_induced_project_trust_cleanup"]["status"] == "removed"
    assert receipt["runner_induced_project_trust_cleanup"]["entry_removed"] is True
    assert receipt["configuration_mode"] == "host_codex_home_with_isolated_config"
    assert receipt["host_user_config_ignored"] is True
    assert receipt["target_project_config_isolated"] is True
    assert receipt["platform_os_name"] == os.name
    assert receipt["native_windows_sandbox_mode"] == (
        "unelevated" if native_windows else "not_applicable"
    )
    assert receipt["canonical_subscription_route_verified"] is True
    assert receipt["controlled_rules_enforcement_mode"] == (
        "ignored_native_windows_sandbox" if native_windows else "project_execpolicy"
    )
    assert receipt["controlled_rules_ignored"] is native_windows
    assert receipt["controlled_rules_written"] is (not native_windows)
    assert receipt["controlled_execution_mode_verified"] is True
    assert receipt["target_config_manifest_while_isolated"] == []
    assert (
        receipt["target_config_manifest_after_restore"] == receipt["target_config_manifest_before"]
    )
    assert receipt["global_rules_loaded"] is (not native_windows)
    assert receipt["host_global_rules_unchanged"] is True
    assert receipt["chatgpt_subscription_auth_verified"] is True
    assert receipt["chatgpt_subscription_login_status_verified"] is True
    assert receipt["chatgpt_subscription_activation_probe_verified"] is True
    assert receipt["chatgpt_subscription_post_login_status_verified"] is True
    assert receipt["post_login_status"]["status_kind"] == "chatgpt"
    assert receipt["api_key_auth_environment_disabled"] is True
    assert receipt["auth_mode"] == "shared_host_chatgpt_subscription_cache"
    assert receipt["auth_cache_copied"] is False
    assert receipt["auth_cache_deleted"] is False
    assert receipt["host_auth_identity_unchanged"] is True
    assert (source_codex_home / "auth.json").read_bytes() == source_auth_before
    assert source_config_path.read_bytes() == source_config_before
    assert not (source_codex_home / ".tmp").exists()
    assert len(calls) == 2
    probe_call, mission_call = calls
    assert "agent_shell_probe" in Path(str(probe_call["raw_events_path"])).as_posix()
    for call in calls:
        assert call["ignore_user_config"] is True
        assert call["ignore_rules"] is native_windows
        assert call["env_overrides"]["OPENAI_API_KEY"] == ""
        assert call["env_overrides"]["CODEX_API_KEY"] == ""
        assert call["env_overrides"]["CODEX_ACCESS_TOKEN"] == ""
        assert all(
            call["env_overrides"][name] == "" for name in CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS
        )
        assert 'forced_login_method="chatgpt"' in call["config_overrides"]
        assert 'model_provider="openai"' in call["config_overrides"]
        assert all(
            override in call["config_overrides"]
            for override in CONTROLLED_CODEX_NON_ROUTING_CONFIG_OVERRIDES
        )
        assert any(str(value).startswith("projects.") for value in call["config_overrides"])
        assert (
            CONTROLLED_CODEX_WINDOWS_SANDBOX_CONFIG_OVERRIDE in call["config_overrides"]
        ) is native_windows
        assert call["config_overrides"][-len(CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES) :] == list(
            CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES
        )
        assert not any("alternate.invalid" in str(value) for value in call["config_overrides"])
    assert probe_call["resume_session_id"] is None
    assert mission_call["resume_session_id"] == session_id
    assert not any(
        str(value).startswith("model_instructions_file=")
        for value in probe_call["config_overrides"]
    )
    assert any(
        str(value).startswith("model_instructions_file=")
        for value in mission_call["config_overrides"]
    )
    assert "model_reasoning_effort=low" in probe_call["config_overrides"]
    assert "model_reasoning_effort=low" not in mission_call["config_overrides"]
    assert "RESEARCH MISSION SENTINEL" not in str(probe_call["prompt"])
    assert mission_call["prompt"] == "RESEARCH MISSION SENTINEL"
    assert (result.run_dir / "prompt.txt").read_text(encoding="utf-8") == (
        "RESEARCH MISSION SENTINEL"
    )
    assert "RESEARCH MISSION SENTINEL" not in (result.run_dir / "prompt.base.txt").read_text(
        encoding="utf-8"
    )
    assert receipt["activation_probe"]["ok"] is True
    assert receipt["activation_probe"]["workspace_unchanged"] is True
    # A dossier-correction resume deliberately authorizes no research commands; the marker-only
    # activation probe still proves the controlled subscription route before continuation.
    assert receipt["activation_probe"]["required_commands_seen"] == []
    assert not any(
        row.get("path", "").startswith(".codex/rules")
        for row in json.loads((result.run_dir / "diff_numstat.json").read_text(encoding="utf-8"))
    )


def test_docker_codex_resume_uses_host_login_without_local_research_overlay() -> None:
    session_id = "019f5000-0000-7000-8000-000000000004"
    docker_resume = RunRequest(
        repo="repo",
        agent="codex",
        codex_resume_session_id=session_id,
        exec_backend="docker",
        exec_use_host_agent_login=True,
    )
    local_resume = RunRequest(
        repo="repo",
        agent="codex",
        codex_resume_session_id=session_id,
        exec_backend="local",
        exec_use_host_agent_login=True,
    )

    assert (
        runner_mod._controlled_codex_overlay_required(
            docker_resume,
            has_sandbox_backend=True,
        )
        is False
    )
    assert (
        runner_mod._controlled_codex_overlay_required(
            local_resume,
            has_sandbox_backend=False,
        )
        is True
    )


@pytest.mark.parametrize(
    ("stdout", "exit_code"),
    [
        ("Logged in using an API key\n", 0),
        ("malformed login status\n", 0),
        ("", 9),
    ],
)
def test_controlled_codex_login_status_failure_prevents_probe_and_mission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    exit_code: int,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    monkeypatch.setenv("OPENAI_API_KEY", "must-be-removed")
    monkeypatch.setenv("CODEX_API_KEY", "must-be-removed")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "must-be-removed")
    target = _setup_target_repo(tmp_path)
    target_rules = target / ".codex" / "rules"
    target_rules.mkdir(parents=True)
    original_rules = target_rules / "target.rules"
    _write(original_rules, 'prefix_rule(pattern=["target"], decision="forbidden")\n')
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
    status_calls: list[dict[str, object]] = []

    def _fake_login_status(**kwargs: object) -> CodexLoginStatusResult:
        env_overrides = kwargs.get("env_overrides")
        env = dict(env_overrides) if isinstance(env_overrides, dict) else {}
        status_calls.append({"env": env, "codex_home": kwargs.get("codex_home")})
        return CodexLoginStatusResult(
            argv=[str(kwargs.get("binary", "codex")), "login", "status"],
            exit_code=exit_code,
            stdout=stdout,
            stderr="",
            codex_home=str(kwargs.get("codex_home")),
            auth_env_vars_blank={
                name: env.get(name) == "" for name in CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS
            },
        )

    monkeypatch.setattr(runner_mod, "probe_codex_login_status", _fake_login_status)
    agent_calls: list[dict[str, object]] = []

    def _unexpected_agent_call(**kwargs: object) -> CodexExecResult:
        agent_calls.append(dict(kwargs))
        raise AssertionError("agent probe or mission must not start after login-status failure")

    monkeypatch.setattr(runner_mod, "run_codex_exec", _unexpected_agent_call)
    monkeypatch.setattr(
        "agent_adapters.shell_probe.run_codex_exec",
        _unexpected_agent_call,
    )

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
            codex_execpolicy_allow_prefixes=(("git", "rev-parse"), ("python",)),
            keep_workspace=True,
        ),
    )

    assert result.exit_code == 1
    assert agent_calls == []
    assert len(status_calls) == 2
    for call in status_calls:
        assert Path(str(call["codex_home"])).resolve() == source_codex_home.resolve()
        assert call["env"]["OPENAI_API_KEY"] == ""
        assert call["env"]["CODEX_API_KEY"] == ""
        assert call["env"]["CODEX_ACCESS_TOKEN"] == ""
    receipt = json.loads(
        (result.run_dir / "codex_execpolicy_overlay.json").read_text(encoding="utf-8")
    )
    assert receipt["host_auth_file_before"]["state"] == "absent"
    assert receipt["chatgpt_subscription_login_status_verified"] is False
    assert receipt["chatgpt_subscription_activation_probe_verified"] is False
    assert receipt["chatgpt_subscription_post_login_status_verified"] is False
    assert receipt["chatgpt_subscription_auth_verified"] is False
    assert receipt["auth_verification_status"] == "failed"
    assert original_rules.is_file()
    assert not (target_rules / "usertest-controlled.rules").exists()


def test_controlled_codex_activation_probe_failure_prevents_mission_and_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    mission_path = runner_root / "configs" / "missions" / "m.mission.md"
    mission_path.write_text(
        mission_path.read_text(encoding="utf-8").replace(
            "execution_mode: single_pass_inline_report",
            "execution_mode: single_pass_inline_report\nrequires_shell: true",
        ),
        encoding="utf-8",
    )
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
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
    agent_calls: list[dict[str, object]] = []

    def _probe_only_codex_exec(**kwargs: object) -> CodexExecResult:
        agent_calls.append(dict(kwargs))
        raw_events_path = kwargs["raw_events_path"]
        last_message_path = kwargs["last_message_path"]
        stderr_path = kwargs["stderr_path"]
        assert isinstance(raw_events_path, Path)
        assert isinstance(last_message_path, Path)
        assert isinstance(stderr_path, Path)
        assert "agent_shell_probe" in raw_events_path.as_posix()
        raw_events_path.write_text(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "powershell -Command \"Write-Output 'shell_probe=ok'\"",
                        "aggregated_output": "shell_probe=ok\n",
                        "exit_code": 0,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        last_message_path.write_text("probe only\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return CodexExecResult(
            argv=["codex"],
            exit_code=0,
            raw_events_path=raw_events_path,
            last_message_path=last_message_path,
            stderr_path=stderr_path,
        )

    monkeypatch.setattr(runner_mod, "run_codex_exec", _probe_only_codex_exec)
    monkeypatch.setattr(
        "agent_adapters.shell_probe.run_codex_exec",
        _probe_only_codex_exec,
    )
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
            codex_execpolicy_allow_prefixes=(("git", "rev-parse"), ("python",)),
            keep_workspace=True,
        ),
    )

    assert result.exit_code == 1
    assert len(agent_calls) == 1
    assert "agent_shell_probe" in Path(str(agent_calls[0]["raw_events_path"])).as_posix()
    receipt = json.loads(
        (result.run_dir / "codex_execpolicy_overlay.json").read_text(encoding="utf-8")
    )
    assert receipt["chatgpt_subscription_login_status_verified"] is True
    assert receipt["chatgpt_subscription_activation_probe_verified"] is False
    assert receipt["chatgpt_subscription_post_login_status_verified"] is True
    assert receipt["chatgpt_subscription_auth_verified"] is False
    assert receipt["auth_verification_status"] == "failed"


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
