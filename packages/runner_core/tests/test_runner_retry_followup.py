from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from normalized_events import iter_events_jsonl
from reporter import validate_report
from run_artifacts.history import load_run_record

import runner_core.target_acquire as target_acquire_mod
from runner_core import RunnerConfig, RunRequest, RunResult, run_once


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
                "    metadata_warning = os.environ.get(",
                "        'DUMMY_INCLUDE_CODEX_METADATA_WARNING', ''",
                "    ).strip()",
                "    if metadata_warning and metadata_warning not in {'0', 'false', 'False'}:",
                "        sys.stderr.write(",
                "            '2026-02-18T00:00:00Z WARN codex_core::shell_snapshot: '",
                "            'Shell snapshot not supported yet for PowerShell\\n'",
                "        )",
                "        sys.stderr.write(",
                "            '2026-02-18T00:00:01Z WARN codex_core::turn_metadata: '",
                "            'timed out after 250ms while building turn metadata header\\n'",
                "        )",
                "        sys.stderr.flush()",
                "",
                (
                    "    print(json.dumps({'type': 'thread.started', "
                    "'thread_id': '019f2cca-9011-7e32-88ae-6c25af578b49'}))"
                ),
                (
                    "    print(json.dumps({'id': str(attempt), 'msg': {'type': 'agent_message', "
                    "'message': f'attempt-{attempt}'}}))"
                ),
                "    if mode == 'invalid_then_valid' and attempt == 1:",
                "        print(json.dumps({",
                "            'type': 'item.completed',",
                "            'item': {",
                "                'id': 'command-from-attempt-1',",
                "                'type': 'command_execution',",
                "                'command': 'python observed_probe.py',",
                "                'aggregated_output': 'observed',",
                "                'exit_code': 0,",
                "                'status': 'completed',",
                "            },",
                "        }))",
                "    if mode == 'invalid_then_valid_with_failed_commands':",
                "        marker = 'FIRST' if attempt == 1 else 'SECOND'",
                "        print(json.dumps({",
                "            'type': 'item.completed',",
                "            'item': {",
                "                'id': f'failed-command-{attempt}',",
                "                'type': 'command_execution',",
                "                'command': f'python failed_probe_{attempt}.py',",
                "                'aggregated_output': marker,",
                "                'stderr': marker,",
                "                'exit_code': 1,",
                "                'status': 'failed',",
                "            },",
                "        }))",
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
                "    if mode == 'structured_subscription_usage_limit':",
                "        message = (",
                '            "You\'ve hit your usage limit. Visit "',
                "            'https://chatgpt.com/codex/settings/usage to purchase more credits '",
                "            'or try again at Jul 18th, 2026 2:33 AM.'",
                "        )",
                "        print(json.dumps({'type': 'error', 'message': message}))",
                "        print(json.dumps({'type': 'turn.failed', 'error': {'message': message}}))",
                "        return 1",
                "",
                "    if (",
                "        mode in {",
                "            'invalid_then_valid',",
                "            'invalid_then_valid_with_failed_commands',",
                "        }",
                "        and attempt == 1",
                "    ):",
                "        if out_path is not None:",
                "            Path(out_path).write_text('not valid json\\n', encoding='utf-8')",
                "        return 0",
                "",
                "    if mode == 'missing_eof_brace':",
                "        if out_path is not None:",
                "            Path(out_path).write_text('{\"ok\": \"yes\"', encoding='utf-8')",
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
                (
                    "    if mode in {"
                    "'task_run_valid', 'task_run_missing_kind', 'task_run_live_output'"
                    "}:"
                ),
                "        outputs = []",
                "        if mode == 'task_run_live_output':",
                "            output_path = Path('artifacts/representative_events.jsonl').resolve()",
                "            output_path.parent.mkdir(parents=True, exist_ok=True)",
                "            events = [",
                "                {'type': 'agent.started'},",
                "                {'type': 'agent.completed'},",
                "            ]",
                "            output_path.write_text(",
                "                ''.join(json.dumps(event) + '\\n' for event in events),",
                "                encoding='utf-8',",
                "            )",
                "            outputs = [{",
                "                'label': 'Representative events',",
                "                'path': str(output_path),",
                "                'description': 'Two representative agent lifecycle events.',",
                "            }]",
                "        report = {",
                "            'schema_version': 1,",
                "            'kind': 'task_run_v1',",
                "            'status': 'success',",
                "            'goal': 'Exercise runner finalization',",
                "            'summary': 'Dummy agent completed successfully.',",
                "            'steps': [{",
                "                'name': 'dummy',",
                "                'attempts': [{'action': 'return report'}],",
                "                'outcome': 'report returned',",
                "            }],",
                "            'outputs': outputs,",
                "            'next_actions': ['No action required.'],",
                "        }",
                "        if mode == 'task_run_missing_kind':",
                "            report.pop('kind')",
                "        if out_path is not None:",
                "            Path(out_path).write_text(",
                "                json.dumps(report) + '\\n', encoding='utf-8'",
                "            )",
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


def _setup_git_target_repo(tmp_path: Path) -> tuple[Path, str]:
    target = _setup_target_repo(tmp_path)
    subprocess.run(["git", "-C", str(target), "init"], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(target), "config", "user.email", "usertest@local"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(target), "config", "user.name", "usertest"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(target), "add", "-A"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(target), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
        text=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return target, sha


def _use_task_run_schema(runner_root: Path) -> dict[str, object]:
    repo_root = Path(__file__).resolve().parents[3]
    schema = json.loads(
        (repo_root / "configs" / "report_schemas" / "task_run_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    (runner_root / "configs" / "report_schemas" / "s.schema.json").write_text(
        json.dumps(schema, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return schema


def _run_task_report_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> RunResult:
    runner_root = _setup_runner_root(tmp_path)
    _use_task_run_schema(runner_root)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)
    monkeypatch.setenv("DUMMY_STATE_FILE", str(tmp_path / "task_report_attempt_state.txt"))
    monkeypatch.setenv("DUMMY_MODE", mode)
    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": dummy_binary}},
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )
    return run_once(
        cfg,
        RunRequest(
            repo=str(target),
            agent="codex",
            policy="safe",
            persona_id="p",
            mission_id="m",
            keep_workspace=False,
            agent_rate_limit_retries=0,
            agent_followup_attempts=0,
        ),
    )


def test_run_once_retains_workspace_for_live_reported_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_task_report_mode(tmp_path, monkeypatch, "task_run_live_output")

    assert result.exit_code == 0
    assert result.report_validation_errors == []
    report = json.loads((result.run_dir / "report.json").read_text(encoding="utf-8"))
    reported_path = Path(report["outputs"][0]["path"])
    reported_events = [
        json.loads(line) for line in reported_path.read_text(encoding="utf-8").splitlines()
    ]
    workspace_ref = json.loads(
        (result.run_dir / "workspace_ref.json").read_text(encoding="utf-8")
    )
    workspace_dir = Path(workspace_ref["workspace_dir"])
    observation = {
        "reported_path_exists_after_run_once": reported_path.is_file(),
        "reported_output_event_types": [event["type"] for event in reported_events],
        "workspace_exists_after_run_once": workspace_dir.is_dir(),
        "workspace_ref_cleanup_suppressed_reason": workspace_ref.get(
            "cleanup_suppressed_reason"
        ),
        "workspace_ref_will_cleanup_workspace": workspace_ref["will_cleanup_workspace"],
    }

    assert observation == {
        "reported_path_exists_after_run_once": True,
        "reported_output_event_types": ["agent.started", "agent.completed"],
        "workspace_exists_after_run_once": True,
        "workspace_ref_cleanup_suppressed_reason": "reported_output_retention",
        "workspace_ref_will_cleanup_workspace": False,
    }
    print(json.dumps(observation, sort_keys=True))


def test_reported_output_retention_qualification_rejects_nonqualifying_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runner_core.runner as runner_mod

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing = workspace / "missing.jsonl"
    external = tmp_path / "external.jsonl"
    external.write_text("external\n", encoding="utf-8")
    directory = workspace / "artifacts"
    directory.mkdir()
    symlink_candidate = workspace / "symlink.jsonl"
    symlink_candidate.write_text("target\n", encoding="utf-8")

    original_is_symlink = Path.is_symlink

    def controlled_is_symlink(path: Path) -> bool:
        if path == symlink_candidate:
            return True
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", controlled_is_symlink)

    def qualifies(path: Path) -> bool:
        return runner_mod._report_has_live_workspace_output(
            {"outputs": [{"path": str(path)}]}, workspace
        )

    observation = {
        "directory_output_qualifies": qualifies(directory.resolve()),
        "external_output_qualifies": qualifies(external.resolve()),
        "missing_output_qualifies": qualifies(missing.absolute()),
        "symlink_output_qualifies": qualifies(symlink_candidate.absolute()),
    }
    assert observation == {
        "directory_output_qualifies": False,
        "external_output_qualifies": False,
        "missing_output_qualifies": False,
        "symlink_output_qualifies": False,
    }
    print(json.dumps(observation, sort_keys=True))


def test_run_once_cleans_workspace_without_qualifying_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run_task_report_mode(tmp_path, monkeypatch, "task_run_valid")

    assert result.exit_code == 0
    assert result.report_validation_errors == []
    workspace_ref = json.loads(
        (result.run_dir / "workspace_ref.json").read_text(encoding="utf-8")
    )
    observation = {
        "nonqualifying_workspace_exists_after_run_once": Path(
            workspace_ref["workspace_dir"]
        ).exists(),
        "workspace_ref_cleanup_suppressed_reason_present": (
            "cleanup_suppressed_reason" in workspace_ref
        ),
        "workspace_ref_will_cleanup_workspace": workspace_ref["will_cleanup_workspace"],
    }
    assert observation == {
        "nonqualifying_workspace_exists_after_run_once": False,
        "workspace_ref_cleanup_suppressed_reason_present": False,
        "workspace_ref_will_cleanup_workspace": True,
    }
    print(json.dumps(observation, sort_keys=True))


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


def test_run_once_allows_runtime_only_codex_personality_warning_during_retry_flow(
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

    assert result.exit_code == 0
    assert result.report_validation_errors == []
    assert not (result.run_dir / "error.json").exists()

    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert len(attempts["attempts"]) == 2
    assert attempts["attempts"][0]["failure_subtype"] == "provider_capacity"

    stderr_text = (result.run_dir / "agent_stderr.txt").read_text(encoding="utf-8")
    assert "Model personality requested but model_messages is missing" not in stderr_text
    assert "classification=runtime_notice" in stderr_text


def test_run_once_records_codex_metadata_capture_for_capability_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    state_file = tmp_path / "attempt_state_metadata_warning.txt"
    monkeypatch.setenv("DUMMY_STATE_FILE", str(state_file))
    monkeypatch.setenv("DUMMY_MODE", "always_success")
    monkeypatch.setenv("DUMMY_INCLUDE_CODEX_METADATA_WARNING", "1")

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

    assert result.exit_code == 0
    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert len(attempts["attempts"]) == 1
    attempt_capture = attempts["attempts"][0].get("codex_metadata_capture")
    assert isinstance(attempt_capture, dict)
    assert attempt_capture["shell_snapshot"]["missing"] is True
    assert attempt_capture["shell_snapshot"]["warning_occurrences"] == 1
    assert attempt_capture["turn_metadata_header"]["missing"] is True
    assert attempt_capture["turn_metadata_header"]["warning_occurrences"] == 1

    run_meta = json.loads((result.run_dir / "run_meta.json").read_text(encoding="utf-8"))
    run_capture = run_meta.get("codex_metadata_capture")
    assert isinstance(run_capture, dict)
    assert run_capture["shell_snapshot"]["missing"] is True
    assert run_capture["shell_snapshot"]["warning_occurrences"] == 1
    assert run_capture["shell_snapshot"]["attempts_missing"] == [1]
    assert run_capture["turn_metadata_header"]["missing"] is True
    assert run_capture["turn_metadata_header"]["warning_occurrences"] == 1
    assert run_capture["turn_metadata_header"]["attempts_missing"] == [1]


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
    resumed_attempt = attempts["attempts"][1]
    assert resumed_attempt["continued_session"] is True
    resumed_argv = resumed_attempt["argv"]
    exec_index = resumed_argv.index("exec")
    cd_index = resumed_argv.index("--cd")
    sandbox_index = resumed_argv.index("--sandbox")
    assert resumed_argv[exec_index + 1] == "resume"
    assert cd_index < exec_index
    assert sandbox_index < exec_index
    assert resumed_argv[sandbox_index + 1] == "read-only"
    workspace_ref = json.loads((result.run_dir / "workspace_ref.json").read_text(encoding="utf-8"))
    assert Path(resumed_argv[cd_index + 1]).resolve() == Path(
        workspace_ref["workspace_dir"]
    ).resolve()
    prompts_text = prompts_file.read_text(encoding="utf-8")
    assert prompts_text.count("===PROMPT===") >= 2
    assert "Follow-up required." in prompts_text
    events = list(iter_events_jsonl(result.run_dir / "normalized_events.jsonl"))
    assert any(
        event.get("type") == "run_command"
        and event.get("data", {}).get("command") == "python observed_probe.py"
        for event in events
    )
    assert (result.run_dir / "raw_events.all_attempts.jsonl").is_file()


@pytest.mark.parametrize("keep_workspace", [False, True])
def test_run_once_uses_relocated_workspace_after_clone_enospc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keep_workspace: bool,
) -> None:
    import runner_core.runner as runner_mod

    runner_root = _setup_runner_root(tmp_path)
    target, expected_sha = _setup_git_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)
    state_file = tmp_path / "relocated_attempt_state.txt"
    runs_dir = tmp_path / "runs"
    fallback = Path(tempfile.gettempdir()) / f"ut_runner_enospc_{uuid4().hex}"
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "sentinel").write_text("keep\n", encoding="utf-8")
    clone_calls: list[Path] = []
    runner_cleanup_calls: list[Path] = []
    original_clone = target_acquire_mod._git_clone
    original_runner_cleanup = runner_mod.remove_acquired_workspace

    def controlled_clone(*, repo: str, dest_dir: Path, no_local: bool = False) -> None:
        clone_calls.append(dest_dir)
        if len(clone_calls) == 1:
            dest_dir.mkdir(parents=True)
            (dest_dir / "partial").write_text("partial\n", encoding="utf-8")
            raise RuntimeError("checkout: No space left on device")
        original_clone(repo=repo, dest_dir=dest_dir, no_local=no_local)

    def tracked_runner_cleanup(path: Path) -> None:
        runner_cleanup_calls.append(path)
        original_runner_cleanup(path)

    monkeypatch.setenv("DUMMY_STATE_FILE", str(state_file))
    monkeypatch.setenv("DUMMY_MODE", "always_success")
    monkeypatch.setattr(target_acquire_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(target_acquire_mod, "_workspace_candidates", lambda **_: [fallback])
    monkeypatch.setattr(
        target_acquire_mod,
        "_windows_volume_identity",
        lambda path: "fallback:" if path == fallback else "preferred:",
    )
    monkeypatch.setattr(target_acquire_mod, "_git_clone", controlled_clone)
    monkeypatch.setattr(runner_mod, "remove_acquired_workspace", tracked_runner_cleanup)

    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=runs_dir,
        agents={"codex": {"binary": dummy_binary}},
        policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
    )

    try:
        result = run_once(
            cfg,
            RunRequest(
                repo=str(target),
                agent="codex",
                policy="safe",
                persona_id="p",
                mission_id="m",
                seed=1 if keep_workspace else 0,
                keep_workspace=keep_workspace,
                agent_rate_limit_retries=0,
                agent_followup_attempts=0,
            ),
        )

        assert result.exit_code == 0
        assert result.report_validation_errors == []
        assert len(clone_calls) == 2
        preferred = clone_calls[0]
        assert clone_calls[1] == fallback
        assert not preferred.exists()
        assert target.exists()
        assert (unrelated / "sentinel").read_text(encoding="utf-8") == "keep\n"

        workspace_ref = json.loads(
            (result.run_dir / "workspace_ref.json").read_text(encoding="utf-8")
        )
        assert Path(workspace_ref["workspace_dir"]).resolve() == fallback.resolve()
        assert workspace_ref["keep_workspace_requested"] is keep_workspace
        assert workspace_ref["will_cleanup_workspace"] is (not keep_workspace)

        target_ref = json.loads((result.run_dir / "target_ref.json").read_text(encoding="utf-8"))
        assert target_ref["commit_sha"] == expected_sha
        assert target_ref["acquire_mode"] == "git"

        attempts = json.loads(
            (result.run_dir / "agent_attempts.json").read_text(encoding="utf-8")
        )
        assert len(attempts["attempts"]) == 1
        argv = attempts["attempts"][0]["argv"]
        cd_index = argv.index("--cd")
        assert Path(argv[cd_index + 1]).resolve() == fallback.resolve()
        assert fallback.exists() is keep_workspace
        assert runner_cleanup_calls == ([] if keep_workspace else [fallback])
    finally:
        if os.path.lexists(fallback):
            target_acquire_mod.remove_acquired_workspace(fallback)


def test_run_once_repairs_unique_eof_delimiter_without_model_followup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    state_file = tmp_path / "attempt_state_eof_repair.txt"
    monkeypatch.setenv("DUMMY_STATE_FILE", str(state_file))
    monkeypatch.setenv("DUMMY_MODE", "missing_eof_brace")

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
    assert attempts["followup_attempts_used"] == 0
    assert len(attempts["attempts"]) == 1
    repair = attempts["attempts"][0]["json_syntax_repair"]
    assert repair["repair_kind"] == "append_missing_eof_delimiters"
    assert repair["appended_delimiters"] == "}"
    report = json.loads((result.run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["ok"] == "yes"


def test_run_once_cumulative_retry_events_keep_failure_artifacts_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    monkeypatch.setenv("DUMMY_STATE_FILE", str(tmp_path / "attempt_state_failures.txt"))
    monkeypatch.setenv("DUMMY_MODE", "invalid_then_valid_with_failed_commands")

    result = run_once(
        RunnerConfig(
            repo_root=runner_root,
            runs_dir=tmp_path / "runs",
            agents={"codex": {"binary": dummy_binary}},
            policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
        ),
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
    commands = [
        event
        for event in iter_events_jsonl(result.run_dir / "normalized_events.jsonl")
        if event.get("type") == "run_command"
    ]
    assert [event["data"]["command"] for event in commands] == [
        "python failed_probe_1.py",
        "python failed_probe_2.py",
    ]
    artifacts = [event["data"]["failure_artifacts"] for event in commands]
    assert artifacts[0]["stderr"] == "command_failures/cmd_01/stderr.txt"
    assert artifacts[1]["stderr"] == "command_failures/cmd_02/stderr.txt"
    assert (result.run_dir / artifacts[0]["stderr"]).read_text(encoding="utf-8") == "FIRST"
    assert (result.run_dir / artifacts[1]["stderr"]).read_text(encoding="utf-8") == "SECOND"


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
    assert not (result.run_dir / "verification_errors.json").exists()
    assert not (result.run_dir / "report_validation_errors.json").exists()
    assert not (result.run_dir / "error.json").exists()
    history_record = load_run_record(result.run_dir, runs_dir=cfg.runs_dir)
    assert history_record is not None
    assert history_record["status"] == "ok"

    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert len(attempts["attempts"]) == 2
    assert attempts["attempts"][0].get("followup_reason") == "verification_failed"

    prompts_text = prompts_file.read_text(encoding="utf-8")
    assert prompts_text.count("===PROMPT===") >= 2
    assert "required verification checks failed" in prompts_text


def test_run_once_failed_verification_uses_typed_error_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    schema = _use_task_run_schema(runner_root)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    (target / "verify_fail.py").write_text(
        "import sys\nprint('verification failed', file=sys.stderr)\nraise SystemExit(1)\n",
        encoding="utf-8",
    )
    state_file = tmp_path / "attempt_state_failed_verification.txt"
    monkeypatch.setenv("DUMMY_STATE_FILE", str(state_file))
    monkeypatch.setenv("DUMMY_MODE", "task_run_valid")

    if os.name == "nt":
        verify_cmd = f'& "{sys.executable}" verify_fail.py'
    else:
        verify_cmd = f'"{sys.executable}" verify_fail.py'

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
            verification_commands=(verify_cmd,),
        ),
    )

    report = json.loads((result.run_dir / "report.json").read_text(encoding="utf-8"))
    verification = json.loads(
        (result.run_dir / "verification.json").read_text(encoding="utf-8")
    )
    verification_errors = json.loads(
        (result.run_dir / "verification_errors.json").read_text(encoding="utf-8")
    )
    error = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    history_record = load_run_record(result.run_dir, runs_dir=cfg.runs_dir)

    assert validate_report(report, schema) == []
    assert result.exit_code == 1
    assert result.report_validation_errors == []
    assert verification["terminal_reason"] == "failed"
    assert verification["commands"][-1]["command"] == verify_cmd
    assert verification_errors["errors"][0] == "verification_failed"
    assert f"command={verify_cmd}" in verification_errors["errors"]
    assert error["type"] == "VerificationFailed"
    assert error["subtype"] == "failed"
    assert error["code"] == "verification_failed"
    assert error["exit_code"] == 1
    assert error["failure_phase"] == "verification"
    assert error["verification"]["terminal_reason"] == "failed"
    assert error["verification"]["failure_reason"] == "verification_failed"
    assert error["verification"]["command"] == verify_cmd
    assert error["verification"]["exit_code"] == 1
    assert error["verification"]["stderr_path"] == "cmd_01.stderr.txt"
    assert not (result.run_dir / "report_validation_errors.json").exists()
    assert history_record is not None
    assert history_record["status"] == "error"

    print(
        json.dumps(
            {
                "lifecycle_status": history_record["status"],
                "report_validation_artifact_exists": (
                    result.run_dir / "report_validation_errors.json"
                ).exists(),
                "report_schema_errors": validate_report(report, schema),
                "result_exit_code": result.exit_code,
                "verification_error_code": error["code"],
                "verification_error_type": error["type"],
                "verification_terminal_reason": verification["terminal_reason"],
            },
            sort_keys=True,
        )
    )


def test_run_once_genuine_task_run_schema_failure_stays_report_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    _use_task_run_schema(runner_root)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    monkeypatch.setenv("DUMMY_STATE_FILE", str(tmp_path / "attempt_state_missing_kind.txt"))
    monkeypatch.setenv("DUMMY_MODE", "task_run_missing_kind")

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

    assert result.report_validation_errors
    assert (result.run_dir / "report_validation_errors.json").exists()
    assert not (result.run_dir / "error.json").exists()
    history_record = load_run_record(result.run_dir, runs_dir=cfg.runs_dir)
    assert history_record is not None
    assert history_record["status"] == "report_validation_error"


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
    assert result.report_validation_errors == []
    assert not (result.run_dir / "report_validation_errors.json").exists()

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


def test_run_once_allows_runtime_only_codex_personality_warning_during_verification_flow(
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

    assert result.exit_code == 0
    assert result.report_validation_errors == []
    assert not (result.run_dir / "error.json").exists()

    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert len(attempts["attempts"]) == 2
    assert attempts["attempts"][0].get("followup_reason") == "verification_failed"

    stderr_text = (result.run_dir / "agent_stderr.txt").read_text(encoding="utf-8")
    assert "Model personality requested but model_messages is missing" not in stderr_text
    assert "classification=runtime_notice" in stderr_text


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


def test_run_once_parks_codex_subscription_limit_from_structured_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    dummy_binary = _make_dummy_codex_retry_binary(tmp_path)

    state_file = tmp_path / "attempt_state_subscription_limit.txt"
    monkeypatch.setenv("DUMMY_STATE_FILE", str(state_file))
    monkeypatch.setenv("DUMMY_MODE", "structured_subscription_usage_limit")
    monkeypatch.setenv("DUMMY_INCLUDE_CODEX_METADATA_WARNING", "1")

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
            agent_followup_attempts=2,
        ),
    )

    assert result.exit_code == 1
    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert len(attempts["attempts"]) == 1
    assert attempts["rate_limit_retries_used"] == 0
    assert attempts["attempts"][0]["failure_subtype"] == ("provider_subscription_usage_limit")
    assert attempts["attempts"][0]["retry_scheduled"] is False
    wait = attempts["external_wait"]
    assert wait["state"] == "parked"
    assert wait["retry_mode"] == "resume_same_session"
    assert wait["resume_after"] == {
        "raw": "Jul 18th, 2026 2:33 AM",
        "timezone": "provider_account_local_unspecified",
    }
    assert wait["route"] == "chatgpt_subscription"
    assert wait["api_fallback_allowed"] is False

    error = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error["type"] == "AgentExternalWait"
    assert error["code"] == "codex_chatgpt_subscription_usage_limit"
    assert error["route"] == "chatgpt_subscription"
    assert error["api_fallback_allowed"] is False
    assert "You've hit your usage limit" in error["provider_message"]
    stderr = (result.run_dir / "agent_stderr.txt").read_text(encoding="utf-8")
    assert "[agent_external_wait]" in stderr
    assert "do not switch to API billing" in stderr


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
