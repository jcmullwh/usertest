from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import runner_core.runner as runner_mod
import runner_core.verification_broker as broker_mod
from runner_core import RunnerConfig, RunRequest, run_once
from runner_core.verification_broker import VerificationBrokerAttempt
from runner_core.workspace_state_hash import WorkspaceStateHash


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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
                "properties": {
                    "ok": {"type": "string"},
                    "extensions": {"type": "object", "additionalProperties": True},
                },
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
    subprocess.run(["git", "init"], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=target, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=target,
        check=True,
        capture_output=True,
    )
    return target


def _verification_command() -> str:
    return 'python -c "print(\'ok\')"'


def _marker_verification_command() -> str:
    return (
        'python -c "from pathlib import Path; import sys; '
        "sys.exit(0 if Path('marker.txt').exists() else 1)\""
    )


def _stub_codex_binary_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_probe_commands_local(
        commands: list[str],
        *,
        workspace_dir: Path,
        env_overrides: dict[str, str] | None = None,
    ) -> tuple[dict[str, bool], dict[str, object]]:
        command_map = {cmd: True for cmd in commands}
        return command_map, {
            "command_probe_details": {
                cmd: {"present": True, "source": "test_stub"} for cmd in commands
            }
        }

    monkeypatch.setattr(runner_mod, "_probe_commands_local", _fake_probe_commands_local)
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


def _run_broker_wrapper(*, run_dir: Path, workspace_dir: Path) -> subprocess.CompletedProcess[str]:
    client_root = run_dir / "verification_broker" / "client"
    if os.name == "nt":
        wrapper = client_root / "verify_client.ps1"
        return subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(wrapper),
            ],
            cwd=str(workspace_dir),
            check=False,
            capture_output=True,
            text=True,
        )
    wrapper = client_root / "verify_client.sh"
    return subprocess.run(
        ["sh", str(wrapper)],
        cwd=str(workspace_dir),
        check=False,
        capture_output=True,
        text=True,
    )


def _make_broker_attempt(
    *,
    run_dir: Path,
    verifier: object,
    wait_timeout_seconds: float | None = 30.0,
) -> VerificationBrokerAttempt:
    client_root = run_dir / "verification_broker" / "client"
    attempt_root = run_dir / "verification_broker" / "attempt1"
    contract = broker_mod.resolve_verification_broker_contract(
        command_prefix=[],
        exec_backend="local",
        validated_python_executable=sys.executable,
        verification_timeout_seconds=wait_timeout_seconds,
        verification_command_count=1,
        is_windows=os.name == "nt",
    )
    return VerificationBrokerAttempt(
        run_dir=run_dir,
        attempt_number=1,
        client_root=client_root,
        client_root_for_agent=str(client_root),
        attempt_root_for_agent=str(attempt_root),
        contract=contract,
        verifier=verifier,
        workspace_hash_fn=lambda: WorkspaceStateHash(
            sha256="abc123",
            mode="filesystem",
            file_count=1,
            deleted_count=0,
        ),
        utc_now_fn=lambda: "2026-03-07T00:00:00Z",
        run_async_verifier=True,
    )


def test_verification_broker_client_waits_for_async_pass(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    summary = {
        "schema_version": 1,
        "attempt_number": 1,
        "commands_configured": [_verification_command()],
        "passed": True,
        "started_utc": "2026-03-07T00:00:00Z",
        "finished_utc": "2026-03-07T00:00:01Z",
        "wall_seconds": 0.01,
        "artifacts_dir": "verification/attempt1/broker_request_01",
        "commands": [
            {
                "command": _verification_command(),
                "exit_code": 0,
                "timed_out": False,
            }
        ],
    }
    broker = _make_broker_attempt(run_dir=run_dir, verifier=lambda _: summary)
    broker.start()
    try:
        completed = _run_broker_wrapper(
            run_dir=run_dir,
            workspace_dir=tmp_path,
        )
    finally:
        broker.stop()

    assert completed.returncode == 0, completed.stderr
    assert "verification requested" in completed.stdout
    assert "verification passed" in completed.stdout


def test_verification_broker_client_waits_for_async_failure(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    summary = {
        "schema_version": 1,
        "attempt_number": 1,
        "commands_configured": [_verification_command()],
        "passed": False,
        "started_utc": "2026-03-07T00:00:00Z",
        "finished_utc": "2026-03-07T00:00:01Z",
        "wall_seconds": 0.01,
        "artifacts_dir": "verification/attempt1/broker_request_01",
        "commands": [
            {
                "command": _verification_command(),
                "exit_code": 1,
                "timed_out": False,
            }
        ],
    }
    broker = _make_broker_attempt(run_dir=run_dir, verifier=lambda _: summary)
    broker.start()
    try:
        completed = _run_broker_wrapper(
            run_dir=run_dir,
            workspace_dir=tmp_path,
        )
    finally:
        broker.stop()

    assert completed.returncode != 0
    assert "failure_reason=verification_failed" in completed.stderr
    assert "summary_path=" in completed.stderr
    assert "broker_request_01" in completed.stderr
    assert "verification.json" in completed.stderr


def test_verification_broker_client_rejects_passed_response_missing_required_artifacts(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    summary = {
        "schema_version": 1,
        "attempt_number": 1,
        "commands_configured": [_verification_command()],
        "passed": True,
        "status": "passed",
        "terminal_reason": "passed",
        "started_utc": "2026-03-07T00:00:00Z",
        "finished_utc": "2026-03-07T00:00:01Z",
        "wall_seconds": 0.01,
        "artifacts_dir": None,
        "commands": [
            {
                "command": _verification_command(),
                "exit_code": 0,
                "timed_out": False,
            }
        ],
    }
    broker = _make_broker_attempt(run_dir=run_dir, verifier=lambda _: summary)
    broker.start()
    try:
        completed = _run_broker_wrapper(run_dir=run_dir, workspace_dir=tmp_path)
    finally:
        broker.stop()

    assert completed.returncode != 0
    assert "missing required artifact fields" in completed.stderr
    assert "artifacts_dir" in completed.stderr
    response_files = sorted(
        (run_dir / "verification_broker" / "attempt1" / "responses").glob("*.json")
    )
    payload = json.loads(response_files[-1].read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert payload["summary_path"] is None


def test_verification_broker_client_surfaces_progress_updates(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"

    def _verifier(_: int, **kwargs: object) -> dict[str, object]:
        progress_callback = kwargs["progress_callback"]
        assert callable(progress_callback)
        progress_callback(
            {
                "phase": "running_command",
                "message": "running verification command 1/1",
                "command_index": 1,
                "command_count": 1,
                "elapsed_seconds": 0.0,
                "updated_utc": "2026-03-07T00:00:00Z",
            },
            status="running",
        )
        time.sleep(0.4)
        return {
            "schema_version": 1,
            "attempt_number": 1,
            "commands_configured": [_verification_command()],
            "passed": True,
            "status": "passed",
            "terminal_reason": "passed",
            "started_utc": "2026-03-07T00:00:00Z",
            "finished_utc": "2026-03-07T00:00:01Z",
            "wall_seconds": 0.4,
            "artifacts_dir": "verification/attempt1/broker_request_01",
            "commands": [
                {
                    "command": _verification_command(),
                    "exit_code": 0,
                    "timed_out": False,
                    "cancelled": False,
                }
            ],
        }

    broker = _make_broker_attempt(run_dir=run_dir, verifier=_verifier)
    broker.start()
    try:
        completed = _run_broker_wrapper(run_dir=run_dir, workspace_dir=tmp_path)
    finally:
        broker.stop()

    assert completed.returncode == 0, completed.stderr
    assert "verification status=" in completed.stdout
    assert "phase=running_command" in completed.stdout


def test_write_json_atomic_retries_transient_permission_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "responses" / "req.json"
    original_replace = Path.replace
    calls = {"count": 0}

    def _flaky_replace(self: Path, target: Path) -> Path:
        if self == path.with_suffix(path.suffix + ".tmp") and calls["count"] == 0:
            calls["count"] += 1
            raise PermissionError(5, "Access is denied")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _flaky_replace)

    broker_mod._write_json_atomic(path, {"status": "passed"})

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert calls["count"] == 1


def test_verification_broker_stop_cancels_inflight_request(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"

    def _verifier(_: int, **kwargs: object) -> dict[str, object]:
        cancel_event = kwargs["cancel_event"]
        progress_callback = kwargs["progress_callback"]
        assert isinstance(cancel_event, threading.Event)
        assert callable(progress_callback)
        progress_callback(
            {
                "phase": "running_command",
                "message": "waiting for broker cancellation",
                "command_index": 1,
                "command_count": 1,
                "elapsed_seconds": 0.0,
                "updated_utc": "2026-03-07T00:00:00Z",
            },
            status="running",
        )
        while not cancel_event.is_set():
            time.sleep(0.05)
        return {
            "schema_version": 1,
            "attempt_number": 1,
            "commands_configured": [_verification_command()],
            "passed": False,
            "status": "cancelled",
            "terminal_reason": "cancelled",
            "cancelled": True,
            "timed_out": False,
            "failure_reason": "runner_shutdown",
            "started_utc": "2026-03-07T00:00:00Z",
            "finished_utc": "2026-03-07T00:00:01Z",
            "wall_seconds": 0.2,
            "artifacts_dir": "verification/attempt1/broker_request_01",
            "commands": [
                {
                    "command": _verification_command(),
                    "exit_code": 130,
                    "timed_out": False,
                    "cancelled": True,
                }
            ],
        }

    broker = _make_broker_attempt(run_dir=run_dir, verifier=_verifier)
    broker.start()
    completed_holder: dict[str, subprocess.CompletedProcess[str]] = {}

    def _run_wrapper() -> None:
        completed_holder["result"] = _run_broker_wrapper(run_dir=run_dir, workspace_dir=tmp_path)

    wrapper_thread = threading.Thread(target=_run_wrapper, daemon=True)
    wrapper_thread.start()
    request_path = run_dir / "verification_broker" / "attempt1" / "requests"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not list(request_path.glob("*.json")):
        time.sleep(0.05)

    started = time.monotonic()
    broker.stop()
    stop_seconds = time.monotonic() - started
    wrapper_thread.join(timeout=5.0)

    assert stop_seconds < 3.0
    assert "result" in completed_holder
    completed = completed_holder["result"]
    assert completed.returncode != 0
    assert "status=cancelled" in completed.stderr

    response_files = sorted(
        (run_dir / "verification_broker" / "attempt1" / "responses").glob("*.json")
    )
    assert response_files
    payload = json.loads(response_files[-1].read_text(encoding="utf-8"))
    assert payload["status"] == "cancelled"
    assert payload["terminal_reason"] == "cancelled"
    assert payload["cancel_requested"] is True


def test_run_verification_commands_times_out_on_broker_deadline(tmp_path: Path) -> None:
    command = 'python -c "import time; time.sleep(60)"'
    started = time.monotonic()
    summary = runner_mod._run_verification_commands(
        run_dir=tmp_path,
        attempt_number=1,
        commands=[command],
        command_prefix=[],
        cwd=tmp_path,
        timeout_seconds=None,
        python_executable=sys.executable,
        deadline_monotonic=time.monotonic() + 0.5,
        deadline_seconds=0.5,
    )
    wall_seconds = time.monotonic() - started

    assert wall_seconds < 10.0
    assert summary["terminal_reason"] == "timed_out"
    assert summary["timed_out"] is True
    commands_out = summary["commands"]
    assert isinstance(commands_out, list)
    assert commands_out[0]["timed_out"] is True


def test_run_once_reuses_broker_verification_without_post_agent_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    _stub_codex_binary_preflight(monkeypatch)

    def _fake_run_codex_exec(**kwargs: object) -> object:
        raw_events_path = Path(str(kwargs["raw_events_path"]))
        last_message_path = Path(str(kwargs["last_message_path"]))
        stderr_path = Path(str(kwargs["stderr_path"]))
        run_dir = last_message_path.parent
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        broker = _run_broker_wrapper(
            run_dir=run_dir,
            workspace_dir=Path(str(kwargs["workspace_dir"])),
        )
        assert broker.returncode == 0, broker.stderr or broker.stdout
        last_message_path.write_text(json.dumps({"ok": "yes"}) + "\n", encoding="utf-8")
        return SimpleNamespace(exit_code=0, argv=["codex", "exec"])

    monkeypatch.setattr(runner_mod, "run_codex_exec", _fake_run_codex_exec)

    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": "codex"}},
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
            verification_commands=(_verification_command(),),
            verification_reuse_mode="auto",
        ),
    )

    assert result.exit_code == 0
    verification = json.loads((result.run_dir / "verification.json").read_text(encoding="utf-8"))
    assert verification["source"] == "broker_reuse"
    assert verification["reused"] is True
    assert verification["passed"] is True
    assert verification["terminal_reason"] == "passed"
    assert not (result.run_dir / "verification" / "attempt1" / "post_agent_rerun").exists()

    reuse = json.loads((result.run_dir / "verification_reuse.json").read_text(encoding="utf-8"))
    assert reuse["selected_source"] == "broker_reuse"
    assert reuse["selected_request_id"]
    report = json.loads((result.run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["extensions"]["verification"]["terminal_reason"] == "passed"


def test_run_once_uses_latest_broker_result_within_single_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    _stub_codex_binary_preflight(monkeypatch)

    def _fake_run_codex_exec(**kwargs: object) -> object:
        raw_events_path = Path(str(kwargs["raw_events_path"]))
        last_message_path = Path(str(kwargs["last_message_path"]))
        stderr_path = Path(str(kwargs["stderr_path"]))
        run_dir = last_message_path.parent
        workspace_dir = Path(str(kwargs["workspace_dir"]))
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")

        first = _run_broker_wrapper(run_dir=run_dir, workspace_dir=workspace_dir)
        assert first.returncode != 0
        (workspace_dir / "marker.txt").write_text("ok\n", encoding="utf-8")
        second = _run_broker_wrapper(run_dir=run_dir, workspace_dir=workspace_dir)
        assert second.returncode == 0, second.stderr or second.stdout

        last_message_path.write_text(json.dumps({"ok": "yes"}) + "\n", encoding="utf-8")
        return SimpleNamespace(exit_code=0, argv=["codex", "exec"])

    monkeypatch.setattr(runner_mod, "run_codex_exec", _fake_run_codex_exec)

    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": "codex"}},
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
            verification_commands=(_marker_verification_command(),),
            verification_reuse_mode="auto",
            agent_followup_attempts=0,
        ),
    )

    assert result.exit_code == 0
    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert len(attempts["attempts"]) == 1
    attempt_verification = attempts["attempts"][0]["verification"]
    assert attempt_verification["source"] == "broker_reuse"
    assert attempt_verification["broker_response_status"] == "passed"
    assert attempt_verification["reuse_selected"] is True

    reuse = json.loads((result.run_dir / "verification_reuse.json").read_text(encoding="utf-8"))
    assert len(reuse["requests"]) == 2
    assert reuse["selected_source"] == "broker_reuse"
    assert reuse["selected_request_id"] == reuse["requests"][-1]["request_id"]
    assert not (result.run_dir / "verification" / "attempt1" / "post_agent_rerun").exists()


def test_run_once_uses_failed_broker_result_directly_before_followup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    _stub_codex_binary_preflight(monkeypatch)
    state = {"attempt": 0}

    def _fake_run_codex_exec(**kwargs: object) -> object:
        state["attempt"] += 1
        raw_events_path = Path(str(kwargs["raw_events_path"]))
        last_message_path = Path(str(kwargs["last_message_path"]))
        stderr_path = Path(str(kwargs["stderr_path"]))
        run_dir = last_message_path.parent
        workspace_dir = Path(str(kwargs["workspace_dir"]))
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")

        if state["attempt"] >= 2:
            (workspace_dir / "marker.txt").write_text("ok\n", encoding="utf-8")

        broker = _run_broker_wrapper(run_dir=run_dir, workspace_dir=workspace_dir)
        if state["attempt"] == 1:
            assert broker.returncode != 0
        else:
            assert broker.returncode == 0, broker.stderr or broker.stdout

        last_message_path.write_text(json.dumps({"ok": "yes"}) + "\n", encoding="utf-8")
        return SimpleNamespace(exit_code=0, argv=["codex", "exec"])

    monkeypatch.setattr(runner_mod, "run_codex_exec", _fake_run_codex_exec)

    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": "codex"}},
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
            verification_commands=(_marker_verification_command(),),
            verification_reuse_mode="auto",
            agent_followup_attempts=1,
        ),
    )

    assert result.exit_code == 0
    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert len(attempts["attempts"]) == 2
    first_verification = attempts["attempts"][0]["verification"]
    second_verification = attempts["attempts"][1]["verification"]
    assert first_verification["source"] == "broker_reuse"
    assert first_verification["broker_response_status"] == "failed"
    assert first_verification["broker_response_failure_reason"] == "verification_failed"
    assert attempts["attempts"][0]["followup_reason"] == "verification_failed"
    assert second_verification["source"] == "broker_reuse"
    assert second_verification["broker_response_status"] == "passed"
    assert not (result.run_dir / "verification" / "attempt1" / "post_agent_rerun").exists()


def test_run_once_falls_back_to_post_agent_rerun_when_broker_command_not_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    _stub_codex_binary_preflight(monkeypatch)

    def _fake_run_codex_exec(**kwargs: object) -> object:
        raw_events_path = Path(str(kwargs["raw_events_path"]))
        last_message_path = Path(str(kwargs["last_message_path"]))
        stderr_path = Path(str(kwargs["stderr_path"]))
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        last_message_path.write_text(json.dumps({"ok": "yes"}) + "\n", encoding="utf-8")
        return SimpleNamespace(exit_code=0, argv=["codex", "exec"])

    monkeypatch.setattr(runner_mod, "run_codex_exec", _fake_run_codex_exec)

    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": "codex"}},
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
            verification_commands=(_verification_command(),),
            verification_reuse_mode="auto",
        ),
    )

    assert result.exit_code == 0
    verification = json.loads((result.run_dir / "verification.json").read_text(encoding="utf-8"))
    assert verification["source"] == "post_agent_rerun"
    assert verification["reused"] is False
    assert verification["passed"] is True
    assert (result.run_dir / "verification" / "attempt1" / "post_agent_rerun").exists()

    reuse = json.loads((result.run_dir / "verification_reuse.json").read_text(encoding="utf-8"))
    assert reuse["selected_source"] == "post_agent_rerun"
    assert reuse["fallback_reason"] == "broker_not_requested"


def test_run_once_falls_back_to_post_agent_rerun_when_broker_response_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    _stub_codex_binary_preflight(monkeypatch)

    incomplete_result = broker_mod.VerificationBrokerRequestResult(
        request_id="req_incomplete",
        attempt=1,
        status="passed",
        terminal_reason="passed",
        started_utc="2026-03-07T00:00:00Z",
        deadline_utc="2026-03-07T00:10:30Z",
        deadline_seconds=630.0,
        last_updated_utc="2026-03-07T00:00:01Z",
        finished_utc="2026-03-07T00:00:01Z",
        workspace_hash_after_verification="abc123",
        workspace_hash_mode="filesystem",
        artifacts_dir=None,
        summary_path=None,
        timed_out=False,
        cancelled=False,
        cancel_requested=False,
        failure_reason=None,
        progress=None,
        verification_summary={
            "schema_version": 1,
            "attempt": 1,
            "passed": True,
            "status": "passed",
            "terminal_reason": "passed",
            "commands": [{"command": _verification_command(), "exit_code": 0}],
        },
    )

    class _FakeBrokerAttempt:
        def __init__(self, **_: object) -> None:
            self._result = incomplete_result

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def request_ids(self) -> list[str]:
            return [self._result.request_id]

        def results(self) -> list[broker_mod.VerificationBrokerRequestResult]:
            return [self._result]

        def artifact_rows(self) -> list[dict[str, object]]:
            return [self._result.to_artifact_dict()]

        def latest_result(self) -> broker_mod.VerificationBrokerRequestResult:
            return self._result

    def _fake_run_codex_exec(**kwargs: object) -> object:
        raw_events_path = Path(str(kwargs["raw_events_path"]))
        last_message_path = Path(str(kwargs["last_message_path"]))
        stderr_path = Path(str(kwargs["stderr_path"]))
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        last_message_path.write_text(json.dumps({"ok": "yes"}) + "\n", encoding="utf-8")
        return SimpleNamespace(exit_code=0, argv=["codex", "exec"])

    monkeypatch.setattr(runner_mod, "VerificationBrokerAttempt", _FakeBrokerAttempt)
    monkeypatch.setattr(runner_mod, "run_codex_exec", _fake_run_codex_exec)

    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": "codex"}},
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
            verification_commands=(_verification_command(),),
            verification_reuse_mode="auto",
        ),
    )

    assert result.exit_code == 0
    verification = json.loads((result.run_dir / "verification.json").read_text(encoding="utf-8"))
    assert verification["source"] == "post_agent_rerun"
    assert verification["reused"] is False
    reuse = json.loads((result.run_dir / "verification_reuse.json").read_text(encoding="utf-8"))
    assert reuse["selected_source"] == "post_agent_rerun"
    assert reuse["fallback_reason"] == "broker_response_incomplete"
    assert reuse["requests"][0]["required_artifacts_complete"] is False
    assert "artifacts_dir" in reuse["requests"][0]["missing_required_artifacts"]
    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    assert attempts["attempts"][0]["verification"]["broker_missing_required_artifacts"] == [
        "artifacts_dir",
        "summary_path",
    ]


def test_run_once_serializes_failed_terminal_reason_into_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    _stub_codex_binary_preflight(monkeypatch)

    def _fake_run_codex_exec(**kwargs: object) -> object:
        raw_events_path = Path(str(kwargs["raw_events_path"]))
        last_message_path = Path(str(kwargs["last_message_path"]))
        stderr_path = Path(str(kwargs["stderr_path"]))
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        last_message_path.write_text(json.dumps({"ok": "yes"}) + "\n", encoding="utf-8")
        return SimpleNamespace(exit_code=0, argv=["codex", "exec"])

    monkeypatch.setattr(runner_mod, "run_codex_exec", _fake_run_codex_exec)

    cfg = RunnerConfig(
        repo_root=runner_root,
        runs_dir=tmp_path / "runs",
        agents={"codex": {"binary": "codex"}},
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
            verification_commands=(_marker_verification_command(),),
            verification_reuse_mode="off",
            agent_followup_attempts=0,
        ),
    )

    assert result.exit_code == 0
    verification = json.loads((result.run_dir / "verification.json").read_text(encoding="utf-8"))
    assert verification["terminal_reason"] == "failed"
    report = json.loads((result.run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["extensions"]["verification"]["terminal_reason"] == "failed"
