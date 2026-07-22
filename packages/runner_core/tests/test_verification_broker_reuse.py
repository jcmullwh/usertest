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
from runner_core.pathing import LOCAL_BACKEND_RUN_DIR_ALIAS, normalize_agent_path
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


def _local_backend_broker_root(*, workspace_dir: Path) -> Path:
    """
    Full `run_once()` flows on local backend (no docker `run_dir` mount) stage the
    verification broker's client script and per-attempt request/response files inside the
    workspace instead of under `run_dir`, since a workspace-confined agent (and any
    subprocess it spawns, like the broker client script) cannot reach `run_dir` at all. See
    `_run_dir_agent_visible_root` in `runner.py`.
    """
    return workspace_dir / LOCAL_BACKEND_RUN_DIR_ALIAS


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


def _start_broker_wrapper(*, run_dir: Path, workspace_dir: Path) -> subprocess.Popen[str]:
    client_root = run_dir / "verification_broker" / "client"
    if os.name == "nt":
        wrapper = client_root / "verify_client.ps1"
        return subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(wrapper),
            ],
            cwd=str(workspace_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    wrapper = client_root / "verify_client.sh"
    return subprocess.Popen(
        ["sh", str(wrapper)],
        cwd=str(workspace_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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


def test_verification_broker_graceful_stop_drains_active_request_to_terminal_result(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    verifier_started = threading.Event()
    release_verifier = threading.Event()
    request_result: dict[str, object] = {}
    stop_finished = threading.Event()

    def _verifier(_: int, **kwargs: object) -> dict[str, object]:
        cancel_event = kwargs["cancel_event"]
        assert isinstance(cancel_event, threading.Event)
        verifier_started.set()
        assert release_verifier.wait(timeout=30.0)
        assert cancel_event.is_set() is False
        return {
            "schema_version": 1,
            "attempt_number": 1,
            "commands_configured": [_verification_command()],
            "passed": True,
            "status": "passed",
            "terminal_reason": "passed",
            "started_utc": "2026-03-07T00:00:00Z",
            "finished_utc": "2026-03-07T00:00:01Z",
            "wall_seconds": 0.01,
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

    def _request() -> None:
        request_result["result"] = broker.request_and_wait()

    request_thread = threading.Thread(target=_request)
    request_thread.start()
    assert verifier_started.wait(timeout=30.0)

    def _stop() -> None:
        broker.stop(cancel_pending=False)
        stop_finished.set()

    stop_thread = threading.Thread(target=_stop)
    stop_thread.start()
    assert broker._stop.wait(timeout=30.0)  # noqa: SLF001 - lifecycle synchronization
    assert stop_finished.is_set() is False
    release_verifier.set()

    assert stop_finished.wait(timeout=30.0)
    stop_thread.join()
    request_thread.join()
    result = request_result["result"]
    assert isinstance(result, broker_mod.VerificationBrokerRequestResult)
    assert result.status == "passed"
    assert result.cancelled is False
    assert result.cancel_requested is False
    assert broker.results() == [result]


def test_verification_broker_graceful_stop_has_no_shorter_join_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = _make_broker_attempt(run_dir=tmp_path / "run", verifier=lambda _: {})
    observed_join_timeouts: list[float | None] = []

    class _ThreadProbe:
        def join(self, timeout: float | None = None) -> None:
            observed_join_timeouts.append(timeout)

    monkeypatch.setattr(broker, "_thread", _ThreadProbe())
    broker.stop(cancel_pending=False)

    assert observed_join_timeouts == [None]


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


def test_verification_broker_client_failure_output_only_includes_failed_command_tails(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    passing_tail = "PASSING_COMMAND_TAIL_SHOULD_NOT_BE_MODEL_VISIBLE"
    truncated_prefix = "FAILED_PREFIX_SHOULD_BE_TRUNCATED"
    retained_tail = "FAILED_RETAINED_TAIL"
    summary = {
        "schema_version": 1,
        "attempt_number": 1,
        "commands_configured": ["pass", "fail"],
        "passed": False,
        "status": "failed",
        "terminal_reason": "failed",
        "failure_reason": "verification_failed",
        "started_utc": "2026-03-07T00:00:00Z",
        "finished_utc": "2026-03-07T00:00:01Z",
        "wall_seconds": 12.34,
        "artifacts_dir": "verification/attempt1/broker_request_01",
        "commands": [
            {
                "index": 1,
                "command": "pass",
                "exit_code": 0,
                "wall_seconds": 1.0,
                "timed_out": False,
                "stdout_tail": passing_tail,
                "stderr_tail": passing_tail,
            },
            {
                "index": 2,
                "command": "fail",
                "exit_code": 2,
                "wall_seconds": 2.0,
                "timed_out": False,
                "stderr_tail": truncated_prefix + ("x" * 1400) + retained_tail,
            },
        ],
    }
    broker = _make_broker_attempt(run_dir=run_dir, verifier=lambda _: summary)
    broker.start()
    try:
        completed = _run_broker_wrapper(run_dir=run_dir, workspace_dir=tmp_path)
    finally:
        broker.stop()

    assert completed.returncode != 0
    assert "commands=2" in completed.stderr
    assert "failed_commands=1" in completed.stderr
    assert "failed_command 2" in completed.stderr
    assert "\nfailed_command 2" in completed.stderr
    assert "\\nfailed_command 2" not in completed.stderr
    assert "stderr_tail:" in completed.stderr
    assert (
        "failed_command 2; command=fail; exit_code=2; "
        "wall_seconds=2.00s; timed_out=false\nstderr_tail:"
    ) in completed.stderr
    assert retained_tail in completed.stderr
    assert truncated_prefix not in completed.stderr
    assert passing_tail not in completed.stderr


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


def test_verification_broker_client_keeps_progress_updates_in_artifacts(
    tmp_path: Path,
) -> None:
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
    assert "verification passed" in completed.stdout
    assert "commands=1" in completed.stdout
    assert "wall_seconds=0.40s" in completed.stdout
    assert "verification status=" not in completed.stdout
    assert "phase=running_command" not in completed.stdout
    progress_files = sorted(
        (run_dir / "verification_broker" / "attempt1" / "progress").glob("*.jsonl")
    )
    assert progress_files
    progress_text = progress_files[-1].read_text(encoding="utf-8")
    assert "running_command" in progress_text
    assert "verification_compact" in progress_text


def test_verification_broker_client_rejects_incomplete_pass_response(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    summary = {
        "schema_version": 1,
        "attempt_number": 1,
        "commands_configured": [_verification_command()],
        "passed": True,
        "started_utc": "2026-03-07T00:00:00Z",
        "finished_utc": "2026-03-07T00:00:01Z",
        "wall_seconds": 0.01,
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
    assert "incomplete broker response" in completed.stderr
    assert "artifacts_dir" in completed.stderr


def test_verification_broker_uses_bounded_default_deadline_budget(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    captured: dict[str, float] = {}
    expected_deadline = broker_mod._compute_broker_internal_deadline_seconds(
        verification_timeout_seconds=None,
        verification_command_count=2,
    )
    assert expected_deadline == 21_630.0
    expected_wait_timeout = broker_mod._compute_client_wait_timeout(
        internal_deadline_seconds=expected_deadline,
    )

    def _verifier(_: int, **kwargs: object) -> dict[str, object]:
        captured["deadline_seconds"] = float(kwargs["deadline_seconds"])
        return {
            "schema_version": 1,
            "attempt_number": 1,
            "commands_configured": [_verification_command(), _verification_command()],
            "passed": True,
            "status": "passed",
            "terminal_reason": "passed",
            "started_utc": "2026-03-07T00:00:00Z",
            "finished_utc": "2026-03-07T00:00:01Z",
            "wall_seconds": 0.1,
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

    contract = broker_mod.resolve_verification_broker_contract(
        command_prefix=(),
        exec_backend="local",
        validated_python_executable=sys.executable,
        verification_timeout_seconds=None,
        verification_command_count=2,
        is_windows=(os.name == "nt"),
    )

    broker = VerificationBrokerAttempt(
        run_dir=run_dir,
        attempt_number=1,
        client_root=run_dir / "verification_broker" / "client",
        client_root_for_agent=str(run_dir / "verification_broker" / "client"),
        attempt_root_for_agent=str(run_dir / "verification_broker" / "attempt1"),
        contract=contract,
        verifier=_verifier,
        workspace_hash_fn=lambda: WorkspaceStateHash(
            sha256="abc123",
            mode="filesystem",
            file_count=1,
            deleted_count=0,
        ),
        utc_now_fn=lambda: "2026-03-07T00:00:00Z",
        run_async_verifier=True,
    )
    broker.start()
    try:
        completed = _run_broker_wrapper(run_dir=run_dir, workspace_dir=tmp_path)
    finally:
        broker.stop()

    assert completed.returncode == 0, completed.stderr
    assert captured["deadline_seconds"] == expected_deadline
    response_dir = run_dir / "verification_broker" / "attempt1" / "responses"
    response_files = sorted(response_dir.glob("*.json"))
    assert response_files
    payload = json.loads(response_files[-1].read_text(encoding="utf-8"))
    assert payload["deadline_seconds"] == expected_deadline
    client_python = (run_dir / "verification_broker" / "client" / "verify_client.py").read_text(
        encoding="utf-8"
    )
    assert f"WAIT_TIMEOUT_SECONDS = {expected_wait_timeout!r}" in client_python


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
        workspace_dir = Path(str(kwargs["workspace_dir"]))
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        broker = _run_broker_wrapper(
            run_dir=_local_backend_broker_root(workspace_dir=workspace_dir),
            workspace_dir=workspace_dir,
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


def test_run_once_waits_for_agent_requested_broker_verification_after_agent_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    _stub_codex_binary_preflight(monkeypatch)
    broker_processes: list[subprocess.Popen[str]] = []

    def _fake_run_codex_exec(**kwargs: object) -> object:
        raw_events_path = Path(str(kwargs["raw_events_path"]))
        last_message_path = Path(str(kwargs["last_message_path"]))
        stderr_path = Path(str(kwargs["stderr_path"]))
        workspace_dir = Path(str(kwargs["workspace_dir"]))
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        broker_processes.append(
            _start_broker_wrapper(
                run_dir=_local_backend_broker_root(workspace_dir=workspace_dir),
                workspace_dir=workspace_dir,
            )
        )
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
    assert broker_processes
    stdout, stderr = broker_processes[0].communicate(timeout=10)
    assert broker_processes[0].returncode == 0, stderr or stdout

    verification = json.loads((result.run_dir / "verification.json").read_text(encoding="utf-8"))
    assert verification["source"] == "broker_reuse"
    assert verification["reused"] is True
    assert verification["passed"] is True
    assert not (result.run_dir / "verification" / "attempt1" / "post_agent_rerun").exists()

    reuse = json.loads((result.run_dir / "verification_reuse.json").read_text(encoding="utf-8"))
    assert reuse["selected_source"] == "broker_reuse"
    assert reuse["selected_request_id"] == reuse["requests"][0]["request_id"]
    assert "request_origin" not in reuse["requests"][0]


def test_run_once_prefers_valid_late_agent_request_over_runner_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    _stub_codex_binary_preflight(monkeypatch)
    launch_late_client = threading.Event()
    late_client_result: dict[str, subprocess.CompletedProcess[str]] = {}
    late_client_threads: list[threading.Thread] = []
    broker_start_calls = 0
    client_write_calls = 0
    original_start = VerificationBrokerAttempt.start
    original_write_client_files = VerificationBrokerAttempt._write_client_files

    def _track_start(self: VerificationBrokerAttempt) -> None:
        nonlocal broker_start_calls
        original_start(self)
        broker_start_calls += 1
        if broker_start_calls == 2:
            launch_late_client.set()

    def _track_client_write(
        self: VerificationBrokerAttempt, *args: object, **kwargs: object
    ) -> object:
        nonlocal client_write_calls
        client_write_calls += 1
        return original_write_client_files(self, *args, **kwargs)

    monkeypatch.setattr(VerificationBrokerAttempt, "start", _track_start)
    monkeypatch.setattr(VerificationBrokerAttempt, "_write_client_files", _track_client_write)

    def _fake_run_codex_exec(**kwargs: object) -> object:
        raw_events_path = Path(str(kwargs["raw_events_path"]))
        last_message_path = Path(str(kwargs["last_message_path"]))
        stderr_path = Path(str(kwargs["stderr_path"]))
        workspace_dir = Path(str(kwargs["workspace_dir"]))
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")

        def _launch_after_fallback_broker_starts() -> None:
            assert launch_late_client.wait(timeout=30.0)
            late_client_result["result"] = _run_broker_wrapper(
                run_dir=_local_backend_broker_root(workspace_dir=workspace_dir),
                workspace_dir=workspace_dir,
            )

        thread = threading.Thread(target=_launch_after_fallback_broker_starts)
        late_client_threads.append(thread)
        thread.start()
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

    assert len(late_client_threads) == 1
    late_client_threads[0].join()
    completed = late_client_result["result"]
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert result.exit_code == 0
    assert broker_start_calls == 2
    assert client_write_calls == 1

    reuse = json.loads((result.run_dir / "verification_reuse.json").read_text(encoding="utf-8"))
    assert reuse["selected_source"] == "broker_reuse"
    assert len(reuse["requests"]) == 2
    assert reuse["selected_request_id"] == reuse["requests"][0]["request_id"]
    assert "request_origin" not in reuse["requests"][0]
    assert reuse["requests"][0]["status"] == "passed"
    assert reuse["requests"][0]["cancelled"] is False
    assert reuse["requests"][1]["request_origin"] == "runner_after_agent_ready"
    assert reuse["requests"][1]["status"] == "passed"
    assert reuse["requests"][1]["cancelled"] is False


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
        workspace_dir = Path(str(kwargs["workspace_dir"]))
        broker_root = _local_backend_broker_root(workspace_dir=workspace_dir)
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")

        first = _run_broker_wrapper(run_dir=broker_root, workspace_dir=workspace_dir)
        assert first.returncode != 0
        (workspace_dir / "marker.txt").write_text("ok\n", encoding="utf-8")
        second = _run_broker_wrapper(run_dir=broker_root, workspace_dir=workspace_dir)
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
    assert reuse["selected_request_id"] == reuse["requests"][0]["request_id"]
    assert reuse["requests"][0]["status"] == "passed"
    assert reuse["requests"][1]["status"] == "failed"
    assert not (result.run_dir / "verification" / "attempt1" / "post_agent_rerun").exists()


def test_run_once_uses_failed_broker_result_directly_before_followup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    _stub_codex_binary_preflight(monkeypatch)
    state = {"attempt": 0}
    session_id = "019f2cca-9011-7e32-88ae-6c25af578b49"

    def _fake_run_codex_exec(**kwargs: object) -> object:
        state["attempt"] += 1
        assert kwargs.get("resume_session_id") == (
            None if state["attempt"] == 1 else session_id
        )
        raw_events_path = Path(str(kwargs["raw_events_path"]))
        last_message_path = Path(str(kwargs["last_message_path"]))
        stderr_path = Path(str(kwargs["stderr_path"]))
        workspace_dir = Path(str(kwargs["workspace_dir"]))
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")

        if state["attempt"] >= 2:
            (workspace_dir / "marker.txt").write_text("ok\n", encoding="utf-8")

        broker = _run_broker_wrapper(
            run_dir=_local_backend_broker_root(workspace_dir=workspace_dir),
            workspace_dir=workspace_dir,
        )
        if state["attempt"] == 1:
            assert broker.returncode != 0
        else:
            assert broker.returncode == 0, broker.stderr or broker.stdout

        last_message_path.write_text(json.dumps({"ok": "yes"}) + "\n", encoding="utf-8")
        return SimpleNamespace(
            exit_code=0,
            argv=["codex", "exec"],
            thread_id=session_id,
        )

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


def test_run_once_runner_requests_broker_verification_when_agent_returns_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    _stub_codex_binary_preflight(monkeypatch)
    client_write_calls = 0
    original_write_client_files = VerificationBrokerAttempt._write_client_files

    def _track_client_write(
        self: VerificationBrokerAttempt, *args: object, **kwargs: object
    ) -> object:
        nonlocal client_write_calls
        client_write_calls += 1
        return original_write_client_files(self, *args, **kwargs)

    monkeypatch.setattr(VerificationBrokerAttempt, "_write_client_files", _track_client_write)

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
    assert verification["source"] == "broker_reuse"
    assert verification["reused"] is True
    assert verification["passed"] is True
    assert not (result.run_dir / "verification" / "attempt1" / "post_agent_rerun").exists()

    reuse = json.loads((result.run_dir / "verification_reuse.json").read_text(encoding="utf-8"))
    assert reuse["selected_source"] == "broker_reuse"
    assert reuse["fallback_reason"] is None
    assert reuse["selected_request_id"]
    assert reuse["requests"][0]["request_origin"] == "runner_after_agent_ready"
    assert client_write_calls == 1

    attempts = json.loads((result.run_dir / "agent_attempts.json").read_text(encoding="utf-8"))
    attempt_verification = attempts["attempts"][0]["verification"]
    assert attempt_verification["source"] == "broker_reuse"
    assert attempt_verification["broker_requested"] is True
    assert attempt_verification["broker_response_status"] == "passed"


def test_run_once_falls_back_to_post_agent_rerun_when_broker_response_is_incomplete(
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
        workspace_dir = Path(str(kwargs["workspace_dir"]))
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        broker = _run_broker_wrapper(
            run_dir=_local_backend_broker_root(workspace_dir=workspace_dir),
            workspace_dir=workspace_dir,
        )
        assert broker.returncode != 0
        assert "incomplete broker response" in broker.stderr
        last_message_path.write_text(json.dumps({"ok": "yes"}) + "\n", encoding="utf-8")
        return SimpleNamespace(exit_code=0, argv=["codex", "exec"])

    monkeypatch.setattr(runner_mod, "run_codex_exec", _fake_run_codex_exec)

    original_run_verification_commands = runner_mod._run_verification_commands

    def _incomplete_broker_verification(*args: object, **kwargs: object) -> dict[str, object]:
        artifacts_dir_rel = kwargs.get("artifacts_dir_rel")
        artifacts_dir_rel_s = str(artifacts_dir_rel) if artifacts_dir_rel is not None else ""
        if "broker_request_" in artifacts_dir_rel_s:
            return {
                "schema_version": 1,
                "attempt_number": int(kwargs["attempt_number"]),
                "commands_configured": [_verification_command()],
                "passed": True,
                "status": "passed",
                "terminal_reason": "passed",
                "started_utc": "2026-03-07T00:00:00Z",
                "finished_utc": "2026-03-07T00:00:01Z",
                "wall_seconds": 0.01,
                "commands": [
                    {
                        "command": _verification_command(),
                        "exit_code": 0,
                        "timed_out": False,
                        "cancelled": False,
                    }
                ],
            }
        return original_run_verification_commands(*args, **kwargs)

    monkeypatch.setattr(runner_mod, "_run_verification_commands", _incomplete_broker_verification)
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
    attempt_verification = attempts["attempts"][0]["verification"]
    assert attempt_verification["broker_missing_required_artifacts"] == [
        "artifacts_dir",
        "summary_path",
    ]
    assert attempt_verification["broker_response_contract_error"]
    assert attempt_verification["broker_response_failure_reason"] == "incomplete_broker_response"


@pytest.mark.parametrize("postprocess_failure", [False, True])
def test_run_once_fails_closed_when_selected_attempt_artifact_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    postprocess_failure: bool,
) -> None:
    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    _stub_codex_binary_preflight(monkeypatch)

    def _fake_run_codex_exec(**kwargs: object) -> object:
        raw_events_path = Path(str(kwargs["raw_events_path"]))
        last_message_path = Path(str(kwargs["last_message_path"]))
        stderr_path = Path(str(kwargs["stderr_path"]))
        workspace_dir = Path(str(kwargs["workspace_dir"]))
        stderr_path.write_text("", encoding="utf-8")
        broker = _run_broker_wrapper(
            run_dir=_local_backend_broker_root(workspace_dir=workspace_dir),
            workspace_dir=workspace_dir,
        )
        assert broker.returncode == 0, broker.stderr or broker.stdout
        if raw_events_path.exists():
            raw_events_path.unlink()
        last_message_path.write_text(json.dumps({"ok": "yes"}) + "\n", encoding="utf-8")
        return SimpleNamespace(exit_code=0, argv=["codex", "exec"])

    monkeypatch.setattr(runner_mod, "run_codex_exec", _fake_run_codex_exec)
    if postprocess_failure:

        def _fail_normalization(**_kwargs: object) -> None:
            raise FileNotFoundError("secondary normalization failure")

        monkeypatch.setattr(runner_mod, "normalize_codex_events", _fail_normalization)

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

    assert result.exit_code == 1
    error_payload = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error_payload["subtype"] == "selected_attempt_artifacts_incomplete"
    details = error_payload["details"]
    assert any(
        "missing_selected_attempt_artifact=raw_events:" in item
        for item in details["errors"]
    )
    assert details["selected_verification_source"] == "broker_reuse"
    secondary_error_path = result.run_dir / "postprocess_error.json"
    assert secondary_error_path.exists() is postprocess_failure
    if postprocess_failure:
        secondary_error = json.loads(secondary_error_path.read_text(encoding="utf-8"))
        assert secondary_error["preserved_terminal_error"] == "error.json"
        assert secondary_error["message"] == "secondary normalization failure"


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

    assert result.exit_code == 1
    assert result.report_validation_errors == []
    verification = json.loads((result.run_dir / "verification.json").read_text(encoding="utf-8"))
    assert verification["terminal_reason"] == "failed"
    report = json.loads((result.run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["extensions"]["verification"]["terminal_reason"] == "failed"
    verification_errors = json.loads(
        (result.run_dir / "verification_errors.json").read_text(encoding="utf-8")
    )
    assert verification_errors["errors"][0] == "verification_failed"
    error = json.loads((result.run_dir / "error.json").read_text(encoding="utf-8"))
    assert error["type"] == "VerificationFailed"
    assert error["code"] == "verification_failed"
    assert error["failure_phase"] == "verification"
    assert error["exit_code"] == 1
    assert error["verification"]["terminal_reason"] == "failed"
    assert error["verification"]["command"] == _marker_verification_command()
    assert not (result.run_dir / "report_validation_errors.json").exists()


# --- Canonical agent-visible path contract -------------------------------------------------
#
# On local backend there is no `run_dir` bind mount into a sandbox: an agent confined to its
# own workspace (and any subprocess it spawns, such as the broker client script) cannot reach
# `run_dir` at all. The tests below cover the canonical mechanism that both the verification
# broker command and surfaced verification artifacts must be derived from, per
# `_run_dir_agent_visible_root` / `_agent_path_for_staged_file` / `_run_verification_commands`
# in `runner.py`.


def test_run_dir_agent_visible_root_stays_under_run_dir_for_docker_mount(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    physical_root = runner_mod._run_dir_agent_visible_root(
        run_dir=run_dir,
        run_dir_mount="/run_dir",
        workspace_dir=workspace_dir,
    )
    assert physical_root == run_dir


def test_run_dir_agent_visible_root_uses_workspace_alias_for_local_backend(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    physical_root = runner_mod._run_dir_agent_visible_root(
        run_dir=run_dir,
        run_dir_mount=None,
        workspace_dir=workspace_dir,
    )
    assert physical_root == workspace_dir / LOCAL_BACKEND_RUN_DIR_ALIAS


def test_run_verification_commands_reports_mount_path_for_docker_backend(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    summary = runner_mod._run_verification_commands(
        run_dir=run_dir,
        attempt_number=1,
        commands=[_verification_command()],
        command_prefix=[],
        cwd=workspace_dir,
        timeout_seconds=30.0,
        python_executable=sys.executable,
        run_dir_mount="/run_dir",
        workspace_dir=workspace_dir,
    )

    assert summary["passed"] is True
    assert summary["artifacts_dir_for_agent"] == "/run_dir/verification/attempt1"
    # Docker's bind mount already makes `run_dir` reachable; no workspace mirror is needed.
    assert not (workspace_dir / LOCAL_BACKEND_RUN_DIR_ALIAS).exists()
    assert (run_dir / "verification" / "attempt1" / "verification.json").exists()


def test_run_verification_commands_mirrors_artifacts_into_workspace_for_local_backend(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    summary = runner_mod._run_verification_commands(
        run_dir=run_dir,
        attempt_number=1,
        commands=[_verification_command()],
        command_prefix=[],
        cwd=workspace_dir,
        timeout_seconds=30.0,
        python_executable=sys.executable,
        run_dir_mount=None,
        workspace_dir=workspace_dir,
    )

    assert summary["passed"] is True
    assert summary["commands_configured"] == [_verification_command()]
    # `artifacts_dir` remains the run_dir-relative bookkeeping label...
    assert summary["artifacts_dir"] == "verification/attempt1"
    # ...but `artifacts_dir_for_agent` must resolve to a real, readable file inside the
    # workspace, since run_dir itself is unreachable from a workspace-confined agent on
    # local backend.
    agent_path = Path(summary["artifacts_dir_for_agent"])
    assert agent_path.is_relative_to(workspace_dir.resolve())
    assert (agent_path / "verification.json").exists()
    mirrored = json.loads((agent_path / "verification.json").read_text(encoding="utf-8"))
    assert mirrored["passed"] is True
    assert mirrored["commands_configured"] == [_verification_command()]
    # The canonical, durable copy remains under run_dir regardless of backend.
    assert (run_dir / "verification" / "attempt1" / "verification.json").exists()


def test_build_verification_followup_prompt_surfaces_agent_visible_path_only(
    tmp_path: Path,
) -> None:
    prompt = runner_mod._build_verification_followup_prompt(
        base_prompt="base",
        verification_summary={
            "commands": [],
            "artifacts_dir": "verification/attempt1",
            "artifacts_dir_for_agent": str(tmp_path / "workspace" / "verification" / "attempt1"),
        },
        schema_dict={},
        prior_last_message_text="prior",
        attempt_number=1,
    )
    assert "Verification artifacts:" in prompt
    assert str(tmp_path / "workspace" / "verification" / "attempt1") in prompt
    # The old hardcoded dual Host/Docker guess must not reappear.
    assert "- Host:" not in prompt
    assert "- Docker:" not in prompt


def test_build_verification_followup_prompt_truncates_tails_and_prior_output() -> None:
    passing_tail = "PASSING_TAIL_NOT_INCLUDED"
    old_failed_tail = "OLD_FAILED_TAIL_TRUNCATED"
    retained_failed_tail = "RETAINED_FAILED_TAIL"
    prior = "p" * 4500
    prompt = runner_mod._build_verification_followup_prompt(
        base_prompt="base",
        verification_summary={
            "status": "failed",
            "terminal_reason": "failed",
            "failure_reason": "verification_failed",
            "wall_seconds": 9.87,
            "artifacts_dir_for_agent": "/agent/verification/attempt1",
            "commands": [
                {
                    "index": 1,
                    "command": "pass",
                    "exit_code": 0,
                    "wall_seconds": 1.0,
                    "stdout_tail": passing_tail,
                    "stderr_tail": passing_tail,
                },
                {
                    "index": 2,
                    "command": "fail",
                    "exit_code": 1,
                    "wall_seconds": 2.0,
                    "stderr_tail": old_failed_tail + ("x" * 1400) + retained_failed_tail,
                },
            ],
        },
        schema_dict={},
        prior_last_message_text=prior,
        attempt_number=1,
    )

    assert "status=failed" in prompt
    assert "command_count=2" in prompt
    assert "wall_seconds_total=9.87" in prompt
    assert "failed_command 2) fail" in prompt
    assert retained_failed_tail in prompt
    assert old_failed_tail not in prompt
    assert passing_tail not in prompt
    assert prompt.count("...[truncated]") == 1
    previous_output = prompt.split("Previous assistant output:\n```\n", 1)[1].split(
        "\n```\n\nFix the issues",
        1,
    )[0]
    assert len(previous_output) == 4000 + len("\n...[truncated]")


def test_verification_broker_response_prefers_agent_visible_artifacts_dir(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    agent_visible = (
        tmp_path / "workspace" / LOCAL_BACKEND_RUN_DIR_ALIAS / "verification" / "attempt1"
    )
    summary = {
        "schema_version": 1,
        "attempt_number": 1,
        "commands_configured": [_verification_command()],
        "passed": False,
        "started_utc": "2026-03-07T00:00:00Z",
        "finished_utc": "2026-03-07T00:00:01Z",
        "wall_seconds": 0.01,
        "artifacts_dir": "verification/attempt1/broker_request_01",
        "artifacts_dir_for_agent": str(agent_visible),
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
        completed = _run_broker_wrapper(run_dir=run_dir, workspace_dir=tmp_path)
    finally:
        broker.stop()

    assert completed.returncode != 0
    assert normalize_agent_path(str(agent_visible)) in completed.stderr
    assert "verification/attempt1/broker_request_01" not in completed.stderr


def test_workspace_state_hash_excludes_local_backend_run_dir_alias(tmp_path: Path) -> None:
    from runner_core.workspace_state_hash import compute_workspace_state_hash

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    (workspace_dir / "real_file.txt").write_text("hello\n", encoding="utf-8")

    before = compute_workspace_state_hash(workspace_dir)

    alias_dir = workspace_dir / LOCAL_BACKEND_RUN_DIR_ALIAS / "verification_broker" / "client"
    alias_dir.mkdir(parents=True)
    (alias_dir / "verify_client.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

    after = compute_workspace_state_hash(workspace_dir)

    assert after.sha256 == before.sha256
    assert after.file_count == before.file_count


def test_gemini_include_directories_includes_local_backend_alias_when_present(
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    assert LOCAL_BACKEND_RUN_DIR_ALIAS not in runner_mod._gemini_include_directories_for_workspace(
        workspace_dir=workspace_dir
    )

    (workspace_dir / LOCAL_BACKEND_RUN_DIR_ALIAS).mkdir()
    assert LOCAL_BACKEND_RUN_DIR_ALIAS in runner_mod._gemini_include_directories_for_workspace(
        workspace_dir=workspace_dir
    )


def test_run_once_local_backend_verification_broker_and_artifacts_are_workspace_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Reproduces the reported failure mode end to end: on local backend, with a codex
    workspace-write policy (no docker `run_dir` mount), the final verification broker
    command must reference a path inside the agent's own workspace, and the surfaced
    verification artifact path must resolve to a real, readable file there too.
    """

    runner_root = _setup_runner_root(tmp_path)
    target = _setup_target_repo(tmp_path)
    _stub_codex_binary_preflight(monkeypatch)

    captured: dict[str, Path] = {}

    def _fake_run_codex_exec(**kwargs: object) -> object:
        raw_events_path = Path(str(kwargs["raw_events_path"]))
        last_message_path = Path(str(kwargs["last_message_path"]))
        stderr_path = Path(str(kwargs["stderr_path"]))
        workspace_dir = Path(str(kwargs["workspace_dir"]))
        captured["workspace_dir"] = workspace_dir
        raw_events_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        broker = _run_broker_wrapper(
            run_dir=_local_backend_broker_root(workspace_dir=workspace_dir),
            workspace_dir=workspace_dir,
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
            keep_workspace=True,
        ),
    )

    assert result.exit_code == 0
    workspace_dir = captured["workspace_dir"]

    # The final broker command handed to the agent must live inside its own workspace, not
    # at a host-only run_dir path outside it.
    verification_config = json.loads(
        (result.run_dir / "verification_config.json").read_text(encoding="utf-8")
    )
    final_command = verification_config["final_handoff_command"]
    assert normalize_agent_path(str(workspace_dir.resolve())) in final_command
    assert str(result.run_dir.resolve()) not in final_command

    # The surfaced verification artifact path must resolve to a real, readable file inside
    # the workspace.
    verification = json.loads((result.run_dir / "verification.json").read_text(encoding="utf-8"))
    artifacts_dir_for_agent = verification["artifacts_dir_for_agent"]
    assert artifacts_dir_for_agent is not None
    assert Path(artifacts_dir_for_agent).is_relative_to(workspace_dir.resolve())
    assert (Path(artifacts_dir_for_agent) / "verification.json").exists()

    # The broker's request/response trail is mirrored back into run_dir once the attempt
    # completes, so run_dir remains the durable, complete audit trail regardless of backend
    # (e.g. usertest_implement's batch failure classification inspects
    # `run_dir/verification_broker/...` directly and must keep working on local backend).
    broker_requests = list(
        (result.run_dir / "verification_broker").glob("attempt*/requests/*.json")
    )
    assert broker_requests
    for request_path in broker_requests:
        response_path = request_path.parent.parent / "responses" / request_path.name
        assert response_path.exists()
