from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from runner_core import RunnerConfig, RunRequest, run_once


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_dummy_codex_retry_binary(tmp_path: Path) -> str:
    script = tmp_path / "dummy_codex_retry.py"
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
                "def _next_attempt(state_path: str) -> int:",
                "    path = Path(state_path)",
                "    if not path.exists():",
                "        path.write_text('1', encoding='utf-8')",
                "        return 1",
                "    raw = path.read_text(encoding='utf-8').strip()",
                "    cur = int(raw) if raw else 0",
                "    nxt = cur + 1",
                "    path.write_text(str(nxt), encoding='utf-8')",
                "    return nxt",
                "",
                "",
                "def _append_prompt(prompt_path: str | None, prompt_text: str) -> None:",
                "    if not prompt_path:",
                "        return",
                "    path = Path(prompt_path)",
                "    with path.open('a', encoding='utf-8', newline='\\n') as f:",
                "        f.write('===PROMPT===\\n')",
                "        f.write(prompt_text)",
                "        if not prompt_text.endswith('\\n'):",
                "            f.write('\\n')",
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
                "    cd_path: str | None = None",
                "    if '--cd' in argv:",
                "        idx = argv.index('--cd')",
                "        if idx + 1 < len(argv):",
                "            cd_path = argv[idx + 1]",
                "    if cd_path:",
                "        try:",
                "            os.chdir(cd_path)",
                "        except Exception:",
                "            pass",
                "",
                "    prompt_text = sys.stdin.read()",
                "    _append_prompt(os.environ.get('DUMMY_PROMPTS_FILE'), prompt_text)",
                "",
                "    state_file = os.environ.get('DUMMY_STATE_FILE', '')",
                "    if not state_file:",
                (
                    "        print(json.dumps({'id': '1', 'msg': {'type': 'agent_message', "
                    "'message': 'missing state'}}))"
                ),
                "        return 1",
                "    attempt = _next_attempt(state_file)",
                "    mode = os.environ.get('DUMMY_MODE', 'rate_limit_then_success')",
                "    env_name = 'DUMMY_INCLUDE_CODEX_PERSONALITY_WARNING'",
                "    include_warning = os.environ.get(env_name, '').strip()",
                "    if include_warning and include_warning not in {'0', 'false', 'False'}:",
                "        sys.stderr.write(",
                "            '2026-02-11T07:26:19.697569Z WARN codex_protocol::openai_models: '",
                (
                    "            'Model personality requested but model_messages is missing, "
                    "falling back '"
                ),
                "            'to base instructions. model=gpt-5.2 personality=pragmatic\\n'",
                "        )",
                "        sys.stderr.flush()",
                "",
                (
                    "    print(json.dumps({'id': str(attempt), 'msg': {'type': 'agent_message', "
                    "'message': f'attempt-{attempt}'}}))"
                ),
                "",
                "    if mode == 'rate_limit_then_success' and attempt == 1:",
                (
                    "        sys.stderr.write('Attempt 1 failed: 429 exhausted your capacity "
                    "quota\\n')"
                ),
                "        return 1",
                "",
                "    if mode == 'limit_message_failure' and attempt == 1:",
                "        if out_path is not None:",
                (
                    "            Path(out_path).write_text(\"You've hit your limit · resets 4am "
                    "(America/New_York)\\n\", encoding='utf-8')"
                ),
                "        return 1",
                "",
                "    if mode == 'invalid_then_valid' and attempt == 1:",
                "        if out_path is not None:",
                "            Path(out_path).write_text('not valid json\\n', encoding='utf-8')",
                "        return 0",
                "",
                "    if mode == 'empty_last_message_auth' and attempt == 1:",
                "        sys.stderr.write('HTTP 401 Unauthorized\\n')",
                "        if out_path is not None:",
                "            Path(out_path).write_text('', encoding='utf-8')",
                "        return 0",
                "",
                "    if mode == 'missing_last_message_file' and attempt == 1:",
                "        sys.stderr.write('HTTP 401 Unauthorized\\n')",
                "        return 1",
                "",
                "    if mode == 'verification_fail_then_pass':",
                "        if attempt >= 2:",
                "            Path('marker.txt').write_text('ok\\n', encoding='utf-8')",
                "        report = {'ok': 'yes'}",
                "        if out_path is not None:",
                (
                    "            Path(out_path).write_text("
                    "json.dumps(report) + '\\n', encoding='utf-8')"
                ),
                "        return 0",
                "",
                "    report = {'ok': 'yes'}",
                "    if out_path is not None:",
                (
                    "        Path(out_path).write_text("
                    "json.dumps(report) + '\\n', encoding='utf-8')"
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
        wrapper = tmp_path / "dummy_codex_retry.cmd"
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

    wrapper = tmp_path / "dummy_codex_retry.sh"
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
        "PROMPT\n${report_schema_json}\n",
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


def test_run_once_retries_provider_capacity_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    state_file = tmp_path / "attempt_state.txt"
    monkeypatch.setenv("DUMMY_STATE_FILE", str(state_file))
    monkeypatch.setenv("DUMMY_MODE", "rate_limit_then_success")

    cfg = RunnerConfig(
        repo_root=runner_root,
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
            persona_id="p",
            mission_id="m",
            agent_rate_limit_retries=2,
            agent_followup_attempts=0,
        ),
    )

    assert result.exit_code == 0
    assert result.report_validation_errors == []
    assert (result.run_dir / "run_meta.json").exists()
    run_meta = json.loads((result.run_dir / "run_meta.json").read_text(encoding="utf-8"))
    assert isinstance(run_meta.get("run_started_utc"), str)
    assert isinstance(run_meta.get("run_finished_utc"), str)
    assert isinstance(run_meta.get("run_wall_seconds"), (int, float))
    assert run_meta["run_wall_seconds"] >= 0

    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert len(attempts["attempts"]) == 2
    assert attempts["attempts"][0]["failure_subtype"] == "provider_capacity"
    for attempt in attempts["attempts"]:
        assert isinstance(attempt.get("attempt_started_utc"), str)
        assert isinstance(attempt.get("attempt_finished_utc"), str)
        assert isinstance(attempt.get("attempt_wall_seconds"), (int, float))
        assert isinstance(attempt.get("agent_exec_wall_seconds"), (int, float))
        assert attempt["attempt_wall_seconds"] >= 0
        assert attempt["agent_exec_wall_seconds"] >= 0


def test_run_once_fails_fast_when_codex_personality_warning_detected_during_retry_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    state_file = tmp_path / "attempt_state_capacity_warning.txt"
    monkeypatch.setenv("DUMMY_STATE_FILE", str(state_file))
    monkeypatch.setenv("DUMMY_MODE", "rate_limit_then_success")
    monkeypatch.setenv("DUMMY_INCLUDE_CODEX_PERSONALITY_WARNING", "1")

    cfg = RunnerConfig(
        repo_root=runner_root,
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
            persona_id="p",
            mission_id="m",
            agent_rate_limit_retries=2,
            agent_followup_attempts=0,
        ),
    )

    assert result.exit_code == 1
    assert any(
        "code=codex_model_messages_missing" in str(line) for line in result.report_validation_errors
    )
    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert len(attempts["attempts"]) == 1
    assert attempts["attempts"][0]["failure_subtype"] == "invalid_agent_config"

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "AgentConfigInvalid"
    assert error_obj.get("code") == "codex_model_messages_missing"


def test_run_once_followup_prompt_recovers_invalid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    state_file = tmp_path / "attempt_state_followup.txt"
    prompts_file = tmp_path / "prompts.log"
    monkeypatch.setenv("DUMMY_STATE_FILE", str(state_file))
    monkeypatch.setenv("DUMMY_MODE", "invalid_then_valid")
    monkeypatch.setenv("DUMMY_PROMPTS_FILE", str(prompts_file))

    cfg = RunnerConfig(
        repo_root=runner_root,
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
            persona_id="p",
            mission_id="m",
            agent_rate_limit_retries=0,
            agent_followup_attempts=2,
        ),
    )

    assert result.exit_code == 0
    assert result.report_validation_errors == []
    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert len(attempts["attempts"]) == 2
    assert attempts["attempts"][0]["report_validation_errors"]
    prompts_text = prompts_file.read_text(encoding="utf-8")
    assert prompts_text.count("===PROMPT===") >= 2
    assert "Follow-up required." in prompts_text


def test_run_once_verification_gate_triggers_followup_until_checks_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    (target / "verify_gate.py").write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import sys",
                "from pathlib import Path",
                "",
                "if not Path('marker.txt').exists():",
                "    print('marker.txt missing', file=sys.stderr)",
                "    raise SystemExit(1)",
                "print('ok')",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    state_file = tmp_path / "attempt_state_verify.txt"
    prompts_file = tmp_path / "prompts_verify.log"
    monkeypatch.setenv("DUMMY_STATE_FILE", str(state_file))
    monkeypatch.setenv("DUMMY_MODE", "verification_fail_then_pass")
    monkeypatch.setenv("DUMMY_PROMPTS_FILE", str(prompts_file))

    if os.name == "nt":
        verify_cmd = f'& "{sys.executable}" verify_gate.py'
    else:
        verify_cmd = f'"{sys.executable}" verify_gate.py'

    cfg = RunnerConfig(
        repo_root=runner_root,
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
            persona_id="p",
            mission_id="m",
            agent_rate_limit_retries=0,
            agent_followup_attempts=2,
            verification_commands=(verify_cmd,),
        ),
    )

    assert result.exit_code == 0
    assert result.report_validation_errors == []
    assert (result.run_dir / "verification.json").exists()

    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert len(attempts["attempts"]) == 2
    assert attempts["attempts"][0].get("followup_reason") == "verification_failed"

    prompts_text = prompts_file.read_text(encoding="utf-8")
    assert prompts_text.count("===PROMPT===") >= 2
    assert "required verification checks failed" in prompts_text


def test_run_once_verification_rejection_sentinel_fails_fast_without_followup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    state_file = tmp_path / "attempt_state_rejected_sentinel.txt"
    prompts_file = tmp_path / "prompts_rejected_sentinel.log"
    monkeypatch.setenv("DUMMY_STATE_FILE", str(state_file))
    monkeypatch.setenv("DUMMY_MODE", "always_success")
    monkeypatch.setenv("DUMMY_PROMPTS_FILE", str(prompts_file))

    cfg = RunnerConfig(
        repo_root=runner_root,
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
            persona_id="p",
            mission_id="m",
            agent_rate_limit_retries=0,
            agent_followup_attempts=2,
            verification_commands=("rejected",),
        ),
    )

    assert result.exit_code == 1
    assert result.report_validation_errors
    assert any(
        "verification_rejected_sentinel" in str(line) for line in result.report_validation_errors
    )

    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert len(attempts["attempts"]) == 1
    verification = attempts["attempts"][0].get("verification")
    assert isinstance(verification, dict)
    assert verification.get("status") == "rejected_sentinel"
    assert verification.get("rejected_sentinel") is True
    assert attempts["attempts"][0].get("followup_scheduled") is not True

    prompts_text = prompts_file.read_text(encoding="utf-8")
    assert "Follow-up required." not in prompts_text

    error_payload = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_payload.get("type") == "VerificationRejectedSentinel"
    assert error_payload.get("code") == "verification_rejected_sentinel"


def test_run_once_fails_fast_when_codex_personality_warning_detected_during_verification_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    (target / "verify_gate.py").write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import sys",
                "from pathlib import Path",
                "",
                "if not Path('marker.txt').exists():",
                "    print('marker.txt missing', file=sys.stderr)",
                "    raise SystemExit(1)",
                "print('ok')",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    state_file = tmp_path / "attempt_state_verify_warning.txt"
    prompts_file = tmp_path / "prompts_verify_warning.log"
    monkeypatch.setenv("DUMMY_STATE_FILE", str(state_file))
    monkeypatch.setenv("DUMMY_MODE", "verification_fail_then_pass")
    monkeypatch.setenv("DUMMY_PROMPTS_FILE", str(prompts_file))
    monkeypatch.setenv("DUMMY_INCLUDE_CODEX_PERSONALITY_WARNING", "1")

    if os.name == "nt":
        verify_cmd = f'& "{sys.executable}" verify_gate.py'
    else:
        verify_cmd = f'"{sys.executable}" verify_gate.py'

    cfg = RunnerConfig(
        repo_root=runner_root,
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
            persona_id="p",
            mission_id="m",
            agent_rate_limit_retries=0,
            agent_followup_attempts=2,
            verification_commands=(verify_cmd,),
        ),
    )

    assert result.exit_code == 1
    assert any(
        "code=codex_model_messages_missing" in str(line) for line in result.report_validation_errors
    )

    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert len(attempts["attempts"]) == 1
    assert attempts["attempts"][0]["failure_subtype"] == "invalid_agent_config"


def test_run_once_uses_last_message_for_capacity_failures_with_empty_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    state_file = tmp_path / "attempt_state_limit.txt"
    monkeypatch.setenv("DUMMY_STATE_FILE", str(state_file))
    monkeypatch.setenv("DUMMY_MODE", "limit_message_failure")

    cfg = RunnerConfig(
        repo_root=runner_root,
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
            persona_id="p",
            mission_id="m",
            agent_rate_limit_retries=0,
            agent_followup_attempts=0,
        ),
    )

    assert result.exit_code == 1
    assert any("hit your limit" in line.lower() for line in result.report_validation_errors)

    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert attempts["attempts"][0]["failure_subtype"] == "provider_capacity"

    stderr_text = (result.run_dir / "agent_stderr.txt").read_text(encoding="utf-8")
    assert "[synthetic_stderr]" in stderr_text
    assert "You've hit your limit" in stderr_text

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("subtype") == "provider_capacity"
    assert "hit your limit" in str(error_obj.get("last_message", "")).lower()


def test_run_once_does_not_retry_non_retryable_capacity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    state_file = tmp_path / "attempt_state_non_retryable_limit.txt"
    monkeypatch.setenv("DUMMY_STATE_FILE", str(state_file))
    monkeypatch.setenv("DUMMY_MODE", "limit_message_failure")

    cfg = RunnerConfig(
        repo_root=runner_root,
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
            persona_id="p",
            mission_id="m",
            agent_rate_limit_retries=2,
            agent_followup_attempts=0,
        ),
    )

    assert result.exit_code == 1
    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert len(attempts["attempts"]) == 1
    assert attempts["rate_limit_retries_used"] == 0


def test_run_once_does_not_followup_when_agent_output_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    state_file = tmp_path / "attempt_state_empty.txt"
    prompts_file = tmp_path / "prompts_empty.log"
    monkeypatch.setenv("DUMMY_STATE_FILE", str(state_file))
    monkeypatch.setenv("DUMMY_MODE", "empty_last_message_auth")
    monkeypatch.setenv("DUMMY_PROMPTS_FILE", str(prompts_file))

    cfg = RunnerConfig(
        repo_root=runner_root,
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
            persona_id="p",
            mission_id="m",
            agent_rate_limit_retries=0,
            agent_followup_attempts=2,
        ),
    )

    assert result.exit_code == 0
    assert result.report_validation_errors

    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert len(attempts["attempts"]) == 1
    assert attempts["followup_attempts_used"] == 0
    assert attempts["attempts"][0]["failure_subtype"] == "provider_auth"

    prompts_text = prompts_file.read_text(encoding="utf-8")
    assert prompts_text.count("===PROMPT===") == 1


def test_run_once_handles_missing_last_message_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    state_file = tmp_path / "attempt_state_missing_last_message.txt"
    monkeypatch.setenv("DUMMY_STATE_FILE", str(state_file))
    monkeypatch.setenv("DUMMY_MODE", "missing_last_message_file")

    cfg = RunnerConfig(
        repo_root=runner_root,
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
            persona_id="p",
            mission_id="m",
            agent_rate_limit_retries=0,
            agent_followup_attempts=0,
        ),
    )

    assert result.exit_code == 1
    assert any("401" in line for line in result.report_validation_errors)

    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert len(attempts["attempts"]) == 1
    assert attempts["attempts"][0]["failure_subtype"] == "provider_auth"

    error_obj = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_obj.get("type") == "AgentExecFailed"
    assert error_obj.get("subtype") == "provider_auth"

    assert (result.run_dir / "agent_last_message.txt").exists()
    assert (result.run_dir / "report.md").exists()


def test_run_once_writes_fallback_metrics_when_compute_metrics_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    state_file = tmp_path / "attempt_state_metrics_fallback.txt"
    monkeypatch.setenv("DUMMY_STATE_FILE", str(state_file))
    monkeypatch.setenv("DUMMY_MODE", "invalid_then_valid")

    import runner_core.runner as runner_mod

    def _boom(_events: object) -> dict[str, object]:
        raise RuntimeError("metrics exploded")

    monkeypatch.setattr(runner_mod, "compute_metrics", _boom)

    cfg = RunnerConfig(
        repo_root=runner_root,
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
            persona_id="p",
            mission_id="m",
            agent_rate_limit_retries=0,
            agent_followup_attempts=2,
        ),
    )

    assert result.exit_code == 0
    metrics_obj = json.loads((result.run_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics_obj.get("metrics_error") == "metrics exploded"
