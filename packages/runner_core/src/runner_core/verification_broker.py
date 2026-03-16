from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from runner_core.pathing import agent_path_join, normalize_agent_path
from runner_core.workspace_state_hash import WorkspaceStateHash

_SHELL_PROBE_TIMEOUT_SECONDS = 2.0
_POWERSHELL_PROBE_TIMEOUT_SECONDS = 10.0
_BROKER_DEFAULT_INTERNAL_DEADLINE_SECONDS = 10_800.0
_BROKER_CLIENT_WAIT_GRACE_SECONDS = 15.0
_BROKER_STOP_JOIN_TIMEOUT_SECONDS = 10.0
_BROKER_PROGRESS_HEARTBEAT_SECONDS = 5.0
_BROKER_TERMINAL_STATUSES = {"passed", "failed", "timed_out", "cancelled"}
_BROKER_NONTERMINAL_STATUSES = {"pending", "running", "cancelling"}
_BROKER_ALL_STATUSES = _BROKER_NONTERMINAL_STATUSES | _BROKER_TERMINAL_STATUSES
_BROKER_ARTIFACT_REQUIRED_STATUSES = {"passed", "failed", "timed_out"}
_BROKER_WORKSPACE_HASH_REQUIRED_STATUSES = {"passed"}


@dataclass(frozen=True)
class VerificationBrokerRequestResult:
    request_id: str
    attempt: int
    status: str
    terminal_reason: str | None
    started_utc: str | None
    deadline_utc: str | None
    deadline_seconds: float | None
    last_updated_utc: str | None
    finished_utc: str | None
    workspace_hash_after_verification: str | None
    workspace_hash_mode: str | None
    artifacts_dir: str | None
    summary_path: str | None
    timed_out: bool
    cancelled: bool
    cancel_requested: bool
    failure_reason: str | None
    progress: dict[str, Any] | None
    verification_summary: dict[str, Any] | None

    def to_artifact_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "attempt": self.attempt,
            "status": self.status,
            "terminal_reason": self.terminal_reason,
            "started_utc": self.started_utc,
            "deadline_utc": self.deadline_utc,
            "deadline_seconds": self.deadline_seconds,
            "last_updated_utc": self.last_updated_utc,
            "finished_utc": self.finished_utc,
            "workspace_hash_after_verification": self.workspace_hash_after_verification,
            "workspace_hash_mode": self.workspace_hash_mode,
            "artifacts_dir": self.artifacts_dir,
            "summary_path": self.summary_path,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "cancel_requested": self.cancel_requested,
            "failure_reason": self.failure_reason,
            "progress": dict(self.progress) if isinstance(self.progress, dict) else None,
        }

    def to_response_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "request_id": self.request_id,
            "attempt": self.attempt,
            "status": self.status,
            "terminal": _is_terminal_status(self.status),
            "terminal_reason": self.terminal_reason,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "cancel_requested": self.cancel_requested,
            "failure_reason": self.failure_reason,
            "artifacts_dir": self.artifacts_dir,
            "summary_path": self.summary_path,
            "workspace_hash_after_verification": self.workspace_hash_after_verification,
            "workspace_hash_mode": self.workspace_hash_mode,
            "started_utc": self.started_utc,
            "deadline_utc": self.deadline_utc,
            "deadline_seconds": self.deadline_seconds,
            "last_updated_utc": self.last_updated_utc,
            "finished_utc": self.finished_utc,
            "progress": dict(self.progress) if isinstance(self.progress, dict) else None,
        }


@dataclass(frozen=True)
class VerificationBrokerClient:
    command: str
    python_script: Path
    shell_script: Path | None
    powershell_script: Path | None


@dataclass(frozen=True)
class VerificationLauncher:
    executable: str
    shell_argv_prefix: tuple[str, ...]
    broker_wrapper_name: str


def resolve_verification_launcher(
    *,
    command_prefix: Sequence[str],
    is_windows: bool | None = None,
) -> VerificationLauncher:
    windows = os.name == "nt" if is_windows is None else bool(is_windows)
    if command_prefix:
        return VerificationLauncher(
            executable="sh",
            shell_argv_prefix=("sh", "-lc"),
            broker_wrapper_name="verify_client.sh",
        )
    if windows:
        return VerificationLauncher(
            executable="powershell",
            shell_argv_prefix=("powershell", "-NoProfile", "-NonInteractive", "-Command"),
            broker_wrapper_name="verify_client.ps1",
        )
    return VerificationLauncher(
        executable="sh",
        shell_argv_prefix=("sh", "-lc"),
        broker_wrapper_name="verify_client.sh",
    )


def render_verification_broker_command(
    *,
    client_root_for_agent: str,
    launcher: VerificationLauncher,
) -> str:
    script_path = agent_path_join(client_root_for_agent, launcher.broker_wrapper_name)
    if launcher.executable == "powershell":
        return (
            "powershell -NoProfile -ExecutionPolicy Bypass -File "
            + _quote_powershell_path(script_path)
        )
    return f"sh {shlex.quote(script_path)}"


def probe_windows_bash_usable() -> dict[str, Any]:
    resolved = shutil.which("bash")
    if resolved is None:
        return {
            "present": False,
            "usable": False,
            "resolved_path": None,
            "reason_code": "not_found",
            "reason": "`bash` was not found on PATH.",
        }

    try:
        proc = subprocess.run(
            [resolved, "-lc", "echo ok"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SHELL_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "present": True,
            "usable": False,
            "resolved_path": resolved,
            "reason_code": "unresponsive",
            "reason": (
                "bash probe timed out "
                f"({_SHELL_PROBE_TIMEOUT_SECONDS:.1f}s) running `bash -lc \"echo ok\"`."
            ),
        }
    except OSError as e:
        return {
            "present": True,
            "usable": False,
            "resolved_path": resolved,
            "reason_code": "blocked",
            "reason": f"bash probe failed: {e}",
        }

    exit_code = int(proc.returncode or 0)
    if exit_code == 0:
        return {
            "present": True,
            "usable": True,
            "resolved_path": resolved,
            "reason_code": None,
            "reason": None,
        }
    stderr = (proc.stderr or "").strip()
    return {
        "present": True,
        "usable": False,
        "resolved_path": resolved,
        "reason_code": "probe_failed",
        "reason": (
            "bash probe exited non-zero"
            + (f": {stderr}" if stderr else f" (exit_code={exit_code})")
        ),
    }


def probe_local_verification_launcher(*, launcher: VerificationLauncher) -> dict[str, Any]:
    executable = launcher.executable
    if executable == "sh" and os.name == "nt":
        return probe_windows_bash_usable()

    resolved = shutil.which(executable)
    if resolved is None:
        return {
            "present": False,
            "usable": False,
            "resolved_path": None,
            "reason_code": "not_found",
            "reason": f"`{executable}` was not found on PATH.",
        }

    probe_argv = (
        [resolved, "-NoProfile", "-NonInteractive", "-Command", "Write-Output ok"]
        if executable == "powershell"
        else [resolved, "-lc", "echo ok"]
    )
    probe_timeout = (
        _POWERSHELL_PROBE_TIMEOUT_SECONDS
        if executable == "powershell"
        else _SHELL_PROBE_TIMEOUT_SECONDS
    )
    try:
        proc = subprocess.run(
            probe_argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=probe_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "present": True,
            "usable": False,
            "resolved_path": resolved,
            "reason_code": "unresponsive",
            "reason": (
                f"{executable} probe timed out ({probe_timeout:.1f}s) running "
                + " ".join(shlex.quote(part) for part in probe_argv)
                + "."
            ),
        }
    except OSError as e:
        return {
            "present": True,
            "usable": False,
            "resolved_path": resolved,
            "reason_code": "blocked",
            "reason": f"{executable} probe failed: {e}",
        }

    exit_code = int(proc.returncode or 0)
    if exit_code == 0:
        return {
            "present": True,
            "usable": True,
            "resolved_path": resolved,
            "reason_code": None,
            "reason": None,
        }
    stderr = (proc.stderr or "").strip()
    return {
        "present": True,
        "usable": False,
        "resolved_path": resolved,
        "reason_code": "probe_failed",
        "reason": (
            f"{executable} probe exited non-zero"
            + (f": {stderr}" if stderr else f" (exit_code={exit_code})")
        ),
    }


def _is_terminal_status(status: str | None) -> bool:
    return isinstance(status, str) and status.strip() in _BROKER_TERMINAL_STATUSES


def _verification_broker_response_contract() -> dict[str, tuple[str, ...] | bool]:
    return {
        "allowed_statuses": tuple(sorted(_BROKER_ALL_STATUSES)),
        "artifact_required_statuses": tuple(sorted(_BROKER_ARTIFACT_REQUIRED_STATUSES)),
        "workspace_hash_required_statuses": tuple(
            sorted(_BROKER_WORKSPACE_HASH_REQUIRED_STATUSES)
        ),
        "require_deadline_utc": True,
        "require_deadline_seconds": True,
    }


def validate_verification_broker_response_payload(
    payload: Any,
    *,
    request_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(payload, dict):
        return None, f"invalid broker response payload for request_id={request_id}"

    response_request_id = payload.get("request_id")
    if isinstance(response_request_id, str) and response_request_id.strip():
        if response_request_id.strip() != request_id:
            return (
                None,
                "invalid broker response request_id mismatch: "
                f"expected {request_id}, got {response_request_id.strip()}",
            )

    contract = _verification_broker_response_contract()
    status = payload.get("status")
    if not isinstance(status, str) or not status.strip():
        return None, f"invalid broker response status for request_id={request_id}"
    status_s = status.strip()
    allowed_statuses = set(contract["allowed_statuses"])
    if status_s not in allowed_statuses:
        return (
            None,
            f"invalid broker response status for request_id={request_id}: {status!r}",
        )

    if contract["require_deadline_utc"]:
        deadline_utc = payload.get("deadline_utc")
        if not isinstance(deadline_utc, str) or not deadline_utc.strip():
            return (
                None,
                f"incomplete broker response deadline_utc for request_id={request_id}",
            )

    if contract["require_deadline_seconds"]:
        deadline_seconds = payload.get("deadline_seconds")
        if not isinstance(deadline_seconds, (int, float)) or float(deadline_seconds) <= 0.0:
            return (
                None,
                f"incomplete broker response deadline_seconds for request_id={request_id}",
            )

    if status_s in set(contract["artifact_required_statuses"]):
        artifacts_dir = payload.get("artifacts_dir")
        if not isinstance(artifacts_dir, str) or not artifacts_dir.strip():
            return (
                None,
                f"incomplete broker response artifacts_dir for request_id={request_id}",
            )
        summary_path = payload.get("summary_path")
        if not isinstance(summary_path, str) or not summary_path.strip():
            return (
                None,
                f"incomplete broker response summary_path for request_id={request_id}",
            )

    if status_s in set(contract["workspace_hash_required_statuses"]):
        workspace_hash_after_verification = payload.get("workspace_hash_after_verification")
        if (
            not isinstance(workspace_hash_after_verification, str)
            or not workspace_hash_after_verification.strip()
        ):
            return (
                None,
                "incomplete broker response workspace_hash_after_verification "
                f"for request_id={request_id}",
            )

    return payload, None


def _compute_broker_internal_deadline_seconds(
    *,
    verification_timeout_seconds: float | None,
    verification_command_count: int,
) -> float:
    if verification_timeout_seconds is None or verification_timeout_seconds <= 0:
        return _BROKER_DEFAULT_INTERNAL_DEADLINE_SECONDS
    command_count = max(1, int(verification_command_count))
    return max(
        300.0,
        float(verification_timeout_seconds) * float(command_count) + 300.0,
    )


def _compute_deadline_utc(started_utc: str | None, *, deadline_seconds: float) -> str | None:
    if not isinstance(started_utc, str) or not started_utc.strip():
        return None
    try:
        started = datetime.fromisoformat(started_utc.replace("Z", "+00:00"))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    deadline = started + timedelta(seconds=max(0.0, float(deadline_seconds)))
    return deadline.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_progress_snapshot(progress: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(progress, dict):
        return None
    payload: dict[str, Any] = {}
    for key in (
        "sequence",
        "phase",
        "message",
        "command_index",
        "command_count",
        "command",
        "elapsed_seconds",
        "updated_utc",
    ):
        value = progress.get(key)
        if value is None:
            continue
        payload[key] = value
    return payload or None


class VerificationBrokerAttempt:
    def __init__(
        self,
        *,
        run_dir: Path,
        attempt_number: int,
        client_root: Path,
        client_root_host_for_agent: Path | None = None,
        client_root_for_agent: str,
        attempt_root_for_agent: str,
        execution_shell: str,
        python_command: str,
        verification_timeout_seconds: float | None,
        verification_command_count: int,
        verifier: Callable[..., dict[str, Any]] | None,
        workspace_hash_fn: Callable[[], WorkspaceStateHash] | None,
        utc_now_fn: Callable[[], str],
        run_async_verifier: bool = True,
    ) -> None:
        self.run_dir = run_dir
        self.attempt_number = attempt_number
        self.execution_shell = execution_shell.strip().lower() or "bash"
        self.verifier = verifier
        self.workspace_hash_fn = workspace_hash_fn
        self.utc_now_fn = utc_now_fn
        self.run_async_verifier = bool(run_async_verifier)
        self.request_token = uuid.uuid4().hex
        self.attempt_root = run_dir / "verification_broker" / f"attempt{attempt_number}"
        self.requests_dir = self.attempt_root / "requests"
        self.responses_dir = self.attempt_root / "responses"
        self.client_root = client_root
        self.client_root_host_for_agent = client_root_host_for_agent
        self.client_root_for_agent = normalize_agent_path(client_root_for_agent)
        self.attempt_root_for_agent = normalize_agent_path(attempt_root_for_agent)
        self.python_script = client_root / "verify_client.py"
        self.shell_script = client_root / "verify_client.sh"
        self.powershell_script = client_root / "verify_client.ps1"
        self._poll_seconds = 0.2
        self._processed_ids: set[str] = set()
        self._request_counter = 0
        self._results: list[VerificationBrokerRequestResult] = []
        self._results_lock = threading.Lock()
        self._active_request_lock = threading.Lock()
        self._active_cancel_event: threading.Event | None = None
        self._stop = threading.Event()
        self._request_settle_seconds = 2.0
        self._drain_deadline_monotonic: float | None = None
        self._internal_deadline_seconds = _compute_broker_internal_deadline_seconds(
            verification_timeout_seconds=verification_timeout_seconds,
            verification_command_count=verification_command_count,
        )
        self._thread = (
            threading.Thread(
                target=self._worker_loop,
                name=f"verification-broker-attempt-{attempt_number}",
                daemon=True,
            )
            if self.run_async_verifier
            else None
        )
        self._client = self._write_client_files(
            python_command=python_command,
            wait_timeout_seconds=_compute_client_wait_timeout(
                internal_deadline_seconds=self._internal_deadline_seconds,
            ),
        )

    @property
    def client(self) -> VerificationBrokerClient:
        return self._client

    def start(self) -> None:
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        if self._thread is not None:
            self._thread.start()

    def stop(
        self,
        *,
        join_timeout_seconds: float | None = None,
        request_settle_seconds: float = 2.0,
    ) -> None:
        if self._thread is None:
            return
        self._request_settle_seconds = max(0.0, float(request_settle_seconds))
        self._drain_deadline_monotonic = time.monotonic() + self._request_settle_seconds
        self._stop.set()
        with self._active_request_lock:
            if self._active_cancel_event is not None:
                self._active_cancel_event.set()
        if join_timeout_seconds is None:
            join_timeout_seconds = _BROKER_STOP_JOIN_TIMEOUT_SECONDS
        self._thread.join(timeout=join_timeout_seconds)

    def latest_success_result(self) -> VerificationBrokerRequestResult | None:
        with self._results_lock:
            successes = [
                result
                for result in self._results
                if result.status == "passed" and result.workspace_hash_after_verification
            ]
            return successes[-1] if successes else None

    def latest_result(self) -> VerificationBrokerRequestResult | None:
        with self._results_lock:
            return self._results[-1] if self._results else None

    def results(self) -> list[VerificationBrokerRequestResult]:
        with self._results_lock:
            return list(self._results)

    def artifact_rows(self) -> list[dict[str, Any]]:
        with self._results_lock:
            return [result.to_artifact_dict() for result in self._results]

    def request_ids(self) -> list[str]:
        request_ids: list[str] = []
        if not self.requests_dir.exists():
            return request_ids
        for request_path in sorted(self.requests_dir.glob("*.json")):
            try:
                payload = json.loads(request_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            token = payload.get("request_token")
            request_id = payload.get("request_id")
            if token != self.request_token:
                continue
            if not isinstance(request_id, str) or not request_id.strip():
                continue
            request_ids.append(request_id.strip())
        return request_ids

    def _worker_loop(self) -> None:
        while True:
            processed = self._process_ready_requests(cancel_pending=self._stop.is_set())
            if self._stop.is_set():
                if processed or self._has_unprocessed_requests():
                    self._drain_deadline_monotonic = (
                        time.monotonic() + self._request_settle_seconds
                    )
                if (
                    not self._has_unprocessed_requests()
                    and not self._has_active_request()
                    and self._drain_deadline_monotonic is not None
                    and time.monotonic() >= self._drain_deadline_monotonic
                ):
                    break
                self._stop.wait(self._poll_seconds)
                continue
            if not processed:
                self._stop.wait(self._poll_seconds)

    def _process_ready_requests(self, *, cancel_pending: bool) -> bool:
        processed_any = False
        if not self.requests_dir.exists():
            return False
        for request_path in sorted(self.requests_dir.glob("*.json")):
            request_id = request_path.stem.strip()
            if not request_id or request_id in self._processed_ids:
                continue
            self._processed_ids.add(request_id)
            processed_any = True
            if cancel_pending:
                self._cancel_request(
                    request_id=request_id,
                    request_path=request_path,
                    reason="runner_shutdown",
                )
            else:
                self._handle_request(request_id=request_id, request_path=request_path)
        return processed_any

    def _has_unprocessed_requests(self) -> bool:
        if not self.requests_dir.exists():
            return False
        for request_path in self.requests_dir.glob("*.json"):
            request_id = request_path.stem.strip()
            if request_id and request_id not in self._processed_ids:
                return True
        return False

    def _has_active_request(self) -> bool:
        with self._active_request_lock:
            return self._active_cancel_event is not None

    def _cancel_request(
        self,
        *,
        request_id: str,
        request_path: Path,
        reason: str,
    ) -> None:
        response_path = self.responses_dir / f"{request_id}.json"
        started_utc = self.utc_now_fn()
        deadline_utc = _compute_deadline_utc(
            started_utc, deadline_seconds=self._internal_deadline_seconds
        )
        result = VerificationBrokerRequestResult(
            request_id=request_id,
            attempt=self.attempt_number,
            status="cancelled",
            terminal_reason="cancelled",
            started_utc=started_utc,
            deadline_utc=deadline_utc,
            deadline_seconds=self._internal_deadline_seconds,
            last_updated_utc=self.utc_now_fn(),
            finished_utc=self.utc_now_fn(),
            workspace_hash_after_verification=None,
            workspace_hash_mode=None,
            artifacts_dir=None,
            summary_path=None,
            timed_out=False,
            cancelled=True,
            cancel_requested=True,
            failure_reason=reason,
            progress={
                "sequence": 1,
                "phase": "cancelled",
                "message": "verification cancelled before execution started",
                "updated_utc": self.utc_now_fn(),
            },
            verification_summary={
                "schema_version": 1,
                "attempt": self.attempt_number,
                "artifacts_dir": None,
                "started_utc": started_utc,
                "finished_utc": self.utc_now_fn(),
                "wall_seconds": 0.0,
                "passed": False,
                "status": "cancelled",
                "terminal_reason": "cancelled",
                "cancelled": True,
                "timed_out": False,
                "failure_reason": reason,
                "commands": [],
            },
        )
        self._record_result(result=result, response_path=response_path)

    def _handle_request(self, *, request_id: str, request_path: Path) -> None:
        response_path = self.responses_dir / f"{request_id}.json"
        started_utc = self.utc_now_fn()
        deadline_utc = _compute_deadline_utc(
            started_utc, deadline_seconds=self._internal_deadline_seconds
        )
        if self.verifier is None or self.workspace_hash_fn is None:
            result = VerificationBrokerRequestResult(
                request_id=request_id,
                attempt=self.attempt_number,
                status="failed",
                terminal_reason="failed",
                started_utc=started_utc,
                deadline_utc=deadline_utc,
                deadline_seconds=self._internal_deadline_seconds,
                last_updated_utc=self.utc_now_fn(),
                finished_utc=self.utc_now_fn(),
                workspace_hash_after_verification=None,
                workspace_hash_mode=None,
                artifacts_dir=None,
                summary_path=None,
                timed_out=False,
                cancelled=False,
                cancel_requested=False,
                failure_reason="async_verifier_disabled",
                progress=None,
                verification_summary=None,
            )
            self._record_result(result=result, response_path=response_path)
            return
        try:
            payload = json.loads(request_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            result = VerificationBrokerRequestResult(
                request_id=request_id,
                attempt=self.attempt_number,
                status="failed",
                terminal_reason="failed",
                started_utc=started_utc,
                deadline_utc=deadline_utc,
                deadline_seconds=self._internal_deadline_seconds,
                last_updated_utc=self.utc_now_fn(),
                finished_utc=self.utc_now_fn(),
                workspace_hash_after_verification=None,
                workspace_hash_mode=None,
                artifacts_dir=None,
                summary_path=None,
                timed_out=False,
                cancelled=False,
                cancel_requested=False,
                failure_reason=f"invalid_request_json: {exc}",
                progress=None,
                verification_summary=None,
            )
            self._record_result(result=result, response_path=response_path)
            return

        token = payload.get("request_token")
        if not isinstance(token, str) or token != self.request_token:
            result = VerificationBrokerRequestResult(
                request_id=request_id,
                attempt=self.attempt_number,
                status="failed",
                terminal_reason="failed",
                started_utc=started_utc,
                deadline_utc=deadline_utc,
                deadline_seconds=self._internal_deadline_seconds,
                last_updated_utc=self.utc_now_fn(),
                finished_utc=self.utc_now_fn(),
                workspace_hash_after_verification=None,
                workspace_hash_mode=None,
                artifacts_dir=None,
                summary_path=None,
                timed_out=False,
                cancelled=False,
                cancel_requested=False,
                failure_reason="invalid_request_token",
                progress=None,
                verification_summary=None,
            )
            self._record_result(result=result, response_path=response_path)
            return

        if self._stop.is_set():
            self._cancel_request(
                request_id=request_id,
                request_path=request_path,
                reason="runner_shutdown",
            )
            return

        progress_sequence = 0
        cancel_event = threading.Event()
        deadline_monotonic = time.monotonic() + self._internal_deadline_seconds

        def _progress_snapshot(payload: dict[str, Any] | None = None, *, status: str) -> None:
            nonlocal progress_sequence
            progress_sequence += 1
            normalized = _normalize_progress_snapshot(payload)
            if normalized is None:
                normalized = {}
            normalized["sequence"] = progress_sequence
            normalized.setdefault("updated_utc", self.utc_now_fn())
            snapshot = VerificationBrokerRequestResult(
                request_id=request_id,
                attempt=self.attempt_number,
                status=status,
                terminal_reason=None,
                started_utc=started_utc,
                deadline_utc=deadline_utc,
                deadline_seconds=self._internal_deadline_seconds,
                last_updated_utc=normalized.get("updated_utc"),
                finished_utc=None,
                workspace_hash_after_verification=None,
                workspace_hash_mode=None,
                artifacts_dir=None,
                summary_path=None,
                timed_out=False,
                cancelled=False,
                cancel_requested=cancel_event.is_set() or self._stop.is_set(),
                failure_reason=None,
                progress=normalized,
                verification_summary=None,
            )
            self._write_response_snapshot(response_path=response_path, result=snapshot)

        with self._active_request_lock:
            self._active_cancel_event = cancel_event
        _progress_snapshot(
            {
                "phase": "accepted",
                "message": "verification request accepted; waiting for brokered execution",
            },
            status="pending",
        )

        try:
            self._request_counter += 1
            _progress_snapshot(
                {
                    "phase": "starting",
                    "message": "verification started",
                },
                status="running",
            )
            try:
                summary = self.verifier(
                    self._request_counter,
                    cancel_event=cancel_event,
                    deadline_monotonic=deadline_monotonic,
                    deadline_utc=deadline_utc,
                    deadline_seconds=self._internal_deadline_seconds,
                    progress_callback=_progress_snapshot,
                )
            except TypeError as exc:
                exc_text = str(exc)
                if "unexpected keyword argument" not in exc_text:
                    raise
                summary = self.verifier(self._request_counter)
        except Exception as exc:  # noqa: BLE001
            result = VerificationBrokerRequestResult(
                request_id=request_id,
                attempt=self.attempt_number,
                status="failed",
                terminal_reason="failed",
                started_utc=started_utc,
                deadline_utc=deadline_utc,
                deadline_seconds=self._internal_deadline_seconds,
                last_updated_utc=self.utc_now_fn(),
                finished_utc=self.utc_now_fn(),
                workspace_hash_after_verification=None,
                workspace_hash_mode=None,
                artifacts_dir=None,
                summary_path=None,
                timed_out=False,
                cancelled=False,
                cancel_requested=cancel_event.is_set(),
                failure_reason=f"broker_exception: {exc}",
                progress=None,
                verification_summary=None,
            )
            with self._active_request_lock:
                self._active_cancel_event = None
            self._record_result(result=result, response_path=response_path)
            return
        finally:
            with self._active_request_lock:
                self._active_cancel_event = None

        artifacts_dir = summary.get("artifacts_dir")
        artifacts_dir_s = (
            normalize_agent_path(artifacts_dir) if isinstance(artifacts_dir, str) else None
        )
        summary_path = (
            agent_path_join(artifacts_dir_s, "verification.json") if artifacts_dir_s else None
        )
        terminal_reason_raw = summary.get("terminal_reason")
        terminal_reason = (
            terminal_reason_raw.strip()
            if isinstance(terminal_reason_raw, str) and terminal_reason_raw.strip()
            else ("passed" if bool(summary.get("passed", False)) else "failed")
        )
        timed_out = bool(summary.get("timed_out", False) or terminal_reason == "timed_out")
        cancelled = bool(summary.get("cancelled", False) or terminal_reason == "cancelled")
        passed = terminal_reason == "passed"
        workspace_hash_after_verification: str | None = None
        workspace_hash_mode: str | None = None
        failure_reason: str | None = None
        status = terminal_reason if terminal_reason in _BROKER_TERMINAL_STATUSES else "failed"

        if passed:
            state_hash = self.workspace_hash_fn()
            workspace_hash_after_verification = state_hash.sha256
            workspace_hash_mode = state_hash.mode
        else:
            if cancelled:
                status = "cancelled"
                failure_reason = str(summary.get("failure_reason") or "cancelled")
            elif timed_out:
                status = "timed_out"
                failure_reason = str(summary.get("failure_reason") or "timed_out")
            elif _summary_has_rejected_sentinel(summary):
                failure_reason = "rejected_sentinel"
            else:
                failure_reason = str(summary.get("failure_reason") or "verification_failed")

        result = VerificationBrokerRequestResult(
            request_id=request_id,
            attempt=self.attempt_number,
            status=status,
            terminal_reason=status,
            started_utc=started_utc,
            deadline_utc=deadline_utc,
            deadline_seconds=self._internal_deadline_seconds,
            last_updated_utc=self.utc_now_fn(),
            finished_utc=self.utc_now_fn(),
            workspace_hash_after_verification=workspace_hash_after_verification,
            workspace_hash_mode=workspace_hash_mode,
            artifacts_dir=artifacts_dir_s,
            summary_path=summary_path,
            timed_out=timed_out,
            cancelled=cancelled,
            cancel_requested=cancel_event.is_set(),
            failure_reason=failure_reason,
            progress=_normalize_progress_snapshot(summary.get("progress")),
            verification_summary=summary,
        )
        self._record_result(result=result, response_path=response_path)

    def _write_response_snapshot(
        self,
        *,
        response_path: Path,
        result: VerificationBrokerRequestResult,
    ) -> None:
        _write_json_atomic(response_path, result.to_response_dict())

    def _record_result(
        self,
        *,
        result: VerificationBrokerRequestResult,
        response_path: Path,
    ) -> None:
        with self._results_lock:
            self._results.append(result)
        self._write_response_snapshot(response_path=response_path, result=result)

    def _write_client_files(
        self,
        *,
        python_command: str,
        wait_timeout_seconds: float,
    ) -> VerificationBrokerClient:
        self.client_root.mkdir(parents=True, exist_ok=True)
        mirror_python_script: Path | None = None
        mirror_shell_script: Path | None = None
        mirror_powershell_script: Path | None = None
        if self.client_root_host_for_agent is not None:
            self.client_root_host_for_agent.mkdir(parents=True, exist_ok=True)
            mirror_python_script = self.client_root_host_for_agent / "verify_client.py"
            mirror_shell_script = self.client_root_host_for_agent / "verify_client.sh"
            mirror_powershell_script = self.client_root_host_for_agent / "verify_client.ps1"
        request_dir_for_agent = agent_path_join(self.attempt_root_for_agent, "requests")
        response_dir_for_agent = agent_path_join(self.attempt_root_for_agent, "responses")
        python_payload = _render_client_python(
            request_token=self.request_token,
            request_dir=request_dir_for_agent,
            response_dir=response_dir_for_agent,
            wait_timeout_seconds=wait_timeout_seconds,
        )
        self.python_script.write_text(python_payload, encoding="utf-8", newline="\n")
        if mirror_python_script is not None:
            mirror_python_script.write_text(python_payload, encoding="utf-8", newline="\n")
        shell_payload = _render_client_shell_wrapper(python_command=python_command)
        self.shell_script.write_text(
            shell_payload,
            encoding="utf-8",
            newline="\n",
        )
        if mirror_shell_script is not None:
            mirror_shell_script.write_text(shell_payload, encoding="utf-8", newline="\n")
        powershell_payload = _render_client_powershell_wrapper(python_command=python_command)
        self.powershell_script.write_text(
            powershell_payload,
            encoding="utf-8",
            newline="\n",
        )
        if mirror_powershell_script is not None:
            mirror_powershell_script.write_text(
                powershell_payload,
                encoding="utf-8",
                newline="\n",
            )

        launcher = resolve_verification_launcher(
            command_prefix=(),
            is_windows=self.execution_shell == "powershell",
        )
        if launcher.executable == "powershell":
            command = render_verification_broker_command(
                client_root_for_agent=self.client_root_for_agent,
                launcher=launcher,
            )
            shell_script = None
            powershell_script = self.powershell_script
        else:
            command = render_verification_broker_command(
                client_root_for_agent=self.client_root_for_agent,
                launcher=launcher,
            )
            shell_script = self.shell_script
            powershell_script = self.powershell_script

        return VerificationBrokerClient(
            command=command,
            python_script=self.python_script,
            shell_script=shell_script,
            powershell_script=powershell_script,
        )


def _render_client_python(
    *,
    request_token: str,
    request_dir: str,
    response_dir: str,
    wait_timeout_seconds: float,
) -> str:
    contract = _verification_broker_response_contract()
    payload = """from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

REQUEST_TOKEN = __REQUEST_TOKEN__
REQUEST_DIR = Path(__REQUEST_DIR__)
RESPONSE_DIR = Path(__RESPONSE_DIR__)
WAIT_TIMEOUT_SECONDS = __WAIT_TIMEOUT_SECONDS__
POLL_INTERVAL_SECONDS = 0.2
ALLOWED_STATUSES = set(__ALLOWED_STATUSES__)
ARTIFACT_REQUIRED_STATUSES = set(__ARTIFACT_REQUIRED_STATUSES__)
WORKSPACE_HASH_REQUIRED_STATUSES = set(__WORKSPACE_HASH_REQUIRED_STATUSES__)
REQUIRE_DEADLINE_UTC = __REQUIRE_DEADLINE_UTC__
REQUIRE_DEADLINE_SECONDS = __REQUIRE_DEADLINE_SECONDS__


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False) + "\\n", encoding="utf-8")
    tmp_path.replace(path)


def _load_response(path: Path, request_id: str) -> tuple[dict[str, object] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"invalid broker response JSON for request_id={request_id}: {exc}"
    if not isinstance(payload, dict):
        return None, f"invalid broker response payload for request_id={request_id}"

    response_request_id = payload.get("request_id")
    if isinstance(response_request_id, str) and response_request_id.strip():
        if response_request_id.strip() != request_id:
            return (
                None,
                "invalid broker response request_id mismatch: "
                f"expected {request_id}, got {response_request_id.strip()}",
            )

    status = payload.get("status")
    if not isinstance(status, str) or not status.strip():
        return None, f"invalid broker response status for request_id={request_id}"
    status = status.strip()
    if status not in ALLOWED_STATUSES:
        return None, f"invalid broker response status for request_id={request_id}: {status!r}"
    if REQUIRE_DEADLINE_UTC:
        deadline_utc = payload.get("deadline_utc")
        if not isinstance(deadline_utc, str) or not deadline_utc.strip():
            return None, f"incomplete broker response deadline_utc for request_id={request_id}"
    if REQUIRE_DEADLINE_SECONDS:
        deadline_seconds = payload.get("deadline_seconds")
        if not isinstance(deadline_seconds, (int, float)) or float(deadline_seconds) <= 0.0:
            return (
                None,
                f"incomplete broker response deadline_seconds for request_id={request_id}",
            )
    if status in ARTIFACT_REQUIRED_STATUSES:
        artifacts_dir = payload.get("artifacts_dir")
        if not isinstance(artifacts_dir, str) or not artifacts_dir.strip():
            return (
                None,
                f"incomplete broker response artifacts_dir for request_id={request_id}",
            )
        summary_path = payload.get("summary_path")
        if not isinstance(summary_path, str) or not summary_path.strip():
            return (
                None,
                f"incomplete broker response summary_path for request_id={request_id}",
            )
    if status in WORKSPACE_HASH_REQUIRED_STATUSES:
        workspace_hash_after_verification = payload.get("workspace_hash_after_verification")
        if (
            not isinstance(workspace_hash_after_verification, str)
            or not workspace_hash_after_verification.strip()
        ):
            return (
                None,
                "incomplete broker response workspace_hash_after_verification "
                f"for request_id={request_id}",
            )
    return payload, None


def _render_failure_message(request_id: str, payload: dict[str, object]) -> str:
    status = str(payload.get("status") or "").strip() or "invalid"
    failure_reason = str(payload.get("failure_reason") or "").strip()
    summary_path = str(payload.get("summary_path") or "").strip()
    artifacts_dir = str(payload.get("artifacts_dir") or "").strip()
    parts = [f"verification failed (request_id={request_id}, status={status})"]
    if failure_reason:
        parts.append(f"failure_reason={failure_reason}")
    if summary_path:
        parts.append(f"summary_path={summary_path}")
    elif artifacts_dir:
        parts.append(f"artifacts_dir={artifacts_dir}")
    return "; ".join(parts)


def _render_progress_message(request_id: str, payload: dict[str, object]) -> str:
    status = str(payload.get("status") or "").strip() or "pending"
    progress = payload.get("progress")
    progress_dict = progress if isinstance(progress, dict) else {}
    message = str(progress_dict.get("message") or "").strip()
    phase = str(progress_dict.get("phase") or "").strip()
    command_index = progress_dict.get("command_index")
    command_count = progress_dict.get("command_count")
    elapsed = progress_dict.get("elapsed_seconds")
    deadline = str(payload.get("deadline_utc") or "").strip()

    parts = [f"verification status={status} (request_id={request_id})"]
    if phase:
        parts.append(f"phase={phase}")
    if message:
        parts.append(message)
    if isinstance(command_index, int) and isinstance(command_count, int):
        parts.append(f"command={command_index}/{command_count}")
    if isinstance(elapsed, (int, float)):
        parts.append(f"elapsed={float(elapsed):.1f}s")
    if deadline:
        parts.append(f"deadline_utc={deadline}")
    if bool(payload.get("cancel_requested")):
        parts.append("cancel_requested=true")
    return "; ".join(parts)


def main() -> int:
    request_id = "req_" + uuid.uuid4().hex
    request_path = REQUEST_DIR / f"{request_id}.json"
    response_path = RESPONSE_DIR / f"{request_id}.json"
    _write_json_atomic(
        request_path,
        {
            "schema_version": 1,
            "request_id": request_id,
            "request_token": REQUEST_TOKEN,
        },
    )
    print(f"verification requested (request_id={request_id})", flush=True)

    deadline = time.monotonic() + float(WAIT_TIMEOUT_SECONDS)
    last_progress_key = ""
    last_heartbeat_monotonic = 0.0
    while True:
        if response_path.exists():
            response_payload, error = _load_response(response_path, request_id)
            if error is not None:
                print(error, file=sys.stderr)
                return 1
            assert response_payload is not None
            status = str(response_payload.get("status") or "").strip()
            terminal = bool(response_payload.get("terminal")) or status in {
                "passed",
                "failed",
                "timed_out",
                "cancelled",
            }
            progress = response_payload.get("progress")
            progress_dict = progress if isinstance(progress, dict) else {}
            progress_key = json.dumps(
                {
                    "status": status,
                    "sequence": progress_dict.get("sequence"),
                    "message": progress_dict.get("message"),
                    "cancel_requested": bool(response_payload.get("cancel_requested")),
                },
                sort_keys=True,
            )
            now = time.monotonic()
            if progress_key != last_progress_key or (
                not terminal
                and (now - last_heartbeat_monotonic) >= __HEARTBEAT_SECONDS__
            ):
                print(_render_progress_message(request_id, response_payload), flush=True)
                last_progress_key = progress_key
                last_heartbeat_monotonic = now
            if status == "passed":
                print(f"verification passed (request_id={request_id})", flush=True)
                return 0
            if terminal:
                print(_render_failure_message(request_id, response_payload), file=sys.stderr)
                return 1
        if time.monotonic() >= deadline:
            print(
                "verification timed out waiting for broker response "
                f"(request_id={request_id}, timeout_seconds={WAIT_TIMEOUT_SECONDS:g})",
                file=sys.stderr,
            )
            return 1
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
"""
    return (
        payload.replace("__REQUEST_TOKEN__", json.dumps(request_token))
        .replace("__REQUEST_DIR__", json.dumps(request_dir))
        .replace("__RESPONSE_DIR__", json.dumps(response_dir))
        .replace("__WAIT_TIMEOUT_SECONDS__", repr(float(wait_timeout_seconds)))
        .replace("__HEARTBEAT_SECONDS__", repr(float(_BROKER_PROGRESS_HEARTBEAT_SECONDS)))
        .replace("__ALLOWED_STATUSES__", json.dumps(list(contract["allowed_statuses"])))
        .replace(
            "__ARTIFACT_REQUIRED_STATUSES__",
            json.dumps(list(contract["artifact_required_statuses"])),
        )
        .replace(
            "__WORKSPACE_HASH_REQUIRED_STATUSES__",
            json.dumps(list(contract["workspace_hash_required_statuses"])),
        )
        .replace(
            "__REQUIRE_DEADLINE_UTC__",
            "True" if contract["require_deadline_utc"] else "False",
        )
        .replace(
            "__REQUIRE_DEADLINE_SECONDS__",
            "True" if contract["require_deadline_seconds"] else "False",
        )
    )


def _render_client_shell_wrapper(*, python_command: str) -> str:
    python_quoted = shlex.quote(python_command)
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        'SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        f'exec {python_quoted} "$SCRIPT_DIR/verify_client.py"\n'
    )


def _render_client_powershell_wrapper(*, python_command: str) -> str:
    python_quoted = _quote_powershell_path(python_command)
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"& {python_quoted} \"$PSScriptRoot\\verify_client.py\"\n"
        "exit $LASTEXITCODE\n"
    )


def _summary_has_rejected_sentinel(summary: dict[str, Any]) -> bool:
    commands = summary.get("commands")
    if not isinstance(commands, list):
        return False
    return any(
        isinstance(command, dict) and bool(command.get("rejected_sentinel"))
        for command in commands
    )


def _compute_client_wait_timeout(
    *,
    internal_deadline_seconds: float,
) -> float:
    return max(60.0, float(internal_deadline_seconds) + _BROKER_CLIENT_WAIT_GRACE_SECONDS)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    last_error: PermissionError | None = None
    for _attempt in range(10):
        try:
            tmp_path.replace(path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.05)
    if last_error is not None:
        raise last_error


def _quote_powershell_path(path: str) -> str:
    return "'" + path.replace("'", "''") + "'"
