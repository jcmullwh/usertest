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
from pathlib import Path
from typing import Any

from runner_core.pathing import agent_path_join, normalize_agent_path
from runner_core.workspace_state_hash import WorkspaceStateHash

_SHELL_PROBE_TIMEOUT_SECONDS = 2.0
_POWERSHELL_PROBE_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class VerificationBrokerRequestResult:
    request_id: str
    attempt: int
    status: str
    started_utc: str | None
    finished_utc: str | None
    workspace_hash_after_verification: str | None
    workspace_hash_mode: str | None
    artifacts_dir: str | None
    summary_path: str | None
    timed_out: bool
    failure_reason: str | None
    verification_summary: dict[str, Any] | None

    def to_artifact_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "attempt": self.attempt,
            "status": self.status,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "workspace_hash_after_verification": self.workspace_hash_after_verification,
            "workspace_hash_mode": self.workspace_hash_mode,
            "artifacts_dir": self.artifacts_dir,
            "summary_path": self.summary_path,
            "timed_out": self.timed_out,
            "failure_reason": self.failure_reason,
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


class VerificationBrokerAttempt:
    def __init__(
        self,
        *,
        run_dir: Path,
        attempt_number: int,
        client_root: Path,
        client_root_for_agent: str,
        attempt_root_for_agent: str,
        execution_shell: str,
        python_command: str,
        verification_timeout_seconds: float | None,
        verification_command_count: int,
        verifier: Callable[[int], dict[str, Any]] | None,
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
        self._stop = threading.Event()
        self._request_settle_seconds = 2.0
        self._drain_deadline_monotonic: float | None = None
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
                verification_timeout_seconds=verification_timeout_seconds,
                verification_command_count=verification_command_count,
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
        if join_timeout_seconds is None:
            self._thread.join()
            return
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
            processed = self._process_ready_requests()
            if self._stop.is_set():
                if processed or self._has_unprocessed_requests():
                    self._drain_deadline_monotonic = (
                        time.monotonic() + self._request_settle_seconds
                    )
                if (
                    not self._has_unprocessed_requests()
                    and self._drain_deadline_monotonic is not None
                    and time.monotonic() >= self._drain_deadline_monotonic
                ):
                    break
                self._stop.wait(self._poll_seconds)
                continue
            if not processed:
                self._stop.wait(self._poll_seconds)

    def _process_ready_requests(self) -> bool:
        processed_any = False
        if not self.requests_dir.exists():
            return False
        for request_path in sorted(self.requests_dir.glob("*.json")):
            request_id = request_path.stem.strip()
            if not request_id or request_id in self._processed_ids:
                continue
            self._processed_ids.add(request_id)
            processed_any = True
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

    def _handle_request(self, *, request_id: str, request_path: Path) -> None:
        response_path = self.responses_dir / f"{request_id}.json"
        started_utc = self.utc_now_fn()
        if self.verifier is None or self.workspace_hash_fn is None:
            result = VerificationBrokerRequestResult(
                request_id=request_id,
                attempt=self.attempt_number,
                status="invalid",
                started_utc=started_utc,
                finished_utc=self.utc_now_fn(),
                workspace_hash_after_verification=None,
                workspace_hash_mode=None,
                artifacts_dir=None,
                summary_path=None,
                timed_out=False,
                failure_reason="async_verifier_disabled",
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
                status="invalid",
                started_utc=started_utc,
                finished_utc=self.utc_now_fn(),
                workspace_hash_after_verification=None,
                workspace_hash_mode=None,
                artifacts_dir=None,
                summary_path=None,
                timed_out=False,
                failure_reason=f"invalid_request_json: {exc}",
                verification_summary=None,
            )
            self._record_result(result=result, response_path=response_path)
            return

        token = payload.get("request_token")
        if not isinstance(token, str) or token != self.request_token:
            result = VerificationBrokerRequestResult(
                request_id=request_id,
                attempt=self.attempt_number,
                status="invalid",
                started_utc=started_utc,
                finished_utc=self.utc_now_fn(),
                workspace_hash_after_verification=None,
                workspace_hash_mode=None,
                artifacts_dir=None,
                summary_path=None,
                timed_out=False,
                failure_reason="invalid_request_token",
                verification_summary=None,
            )
            self._record_result(result=result, response_path=response_path)
            return

        try:
            self._request_counter += 1
            summary = self.verifier(self._request_counter)
        except Exception as exc:  # noqa: BLE001
            result = VerificationBrokerRequestResult(
                request_id=request_id,
                attempt=self.attempt_number,
                status="invalid",
                started_utc=started_utc,
                finished_utc=self.utc_now_fn(),
                workspace_hash_after_verification=None,
                workspace_hash_mode=None,
                artifacts_dir=None,
                summary_path=None,
                timed_out=False,
                failure_reason=f"broker_exception: {exc}",
                verification_summary=None,
            )
            self._record_result(result=result, response_path=response_path)
            return

        artifacts_dir = summary.get("artifacts_dir")
        artifacts_dir_s = normalize_agent_path(artifacts_dir) if isinstance(artifacts_dir, str) else None
        summary_path = (
            agent_path_join(artifacts_dir_s, "verification.json") if artifacts_dir_s else None
        )
        timed_out = any(
            isinstance(command, dict) and bool(command.get("timed_out"))
            for command in summary.get("commands", [])
            if isinstance(summary.get("commands"), list)
        )
        passed = bool(summary.get("passed", False))
        workspace_hash_after_verification: str | None = None
        workspace_hash_mode: str | None = None
        failure_reason: str | None = None
        status = "passed" if passed else "failed"

        if passed:
            state_hash = self.workspace_hash_fn()
            workspace_hash_after_verification = state_hash.sha256
            workspace_hash_mode = state_hash.mode
        else:
            if timed_out:
                status = "timed_out"
                failure_reason = "timed_out"
            elif _summary_has_rejected_sentinel(summary):
                failure_reason = "rejected_sentinel"
            else:
                failure_reason = "verification_failed"

        result = VerificationBrokerRequestResult(
            request_id=request_id,
            attempt=self.attempt_number,
            status=status,
            started_utc=started_utc,
            finished_utc=self.utc_now_fn(),
            workspace_hash_after_verification=workspace_hash_after_verification,
            workspace_hash_mode=workspace_hash_mode,
            artifacts_dir=artifacts_dir_s,
            summary_path=summary_path,
            timed_out=timed_out,
            failure_reason=failure_reason,
            verification_summary=summary,
        )
        self._record_result(result=result, response_path=response_path)

    def _record_result(
        self,
        *,
        result: VerificationBrokerRequestResult,
        response_path: Path,
    ) -> None:
        with self._results_lock:
            self._results.append(result)
        _write_json_atomic(
            response_path,
            {
                "schema_version": 1,
                "request_id": result.request_id,
                "status": result.status,
                "timed_out": result.timed_out,
                "failure_reason": result.failure_reason,
                "artifacts_dir": result.artifacts_dir,
                "summary_path": result.summary_path,
                "workspace_hash_after_verification": result.workspace_hash_after_verification,
                "workspace_hash_mode": result.workspace_hash_mode,
                "started_utc": result.started_utc,
                "finished_utc": result.finished_utc,
            },
        )

    def _write_client_files(
        self,
        *,
        python_command: str,
        wait_timeout_seconds: float,
    ) -> VerificationBrokerClient:
        self.client_root.mkdir(parents=True, exist_ok=True)
        request_dir_for_agent = agent_path_join(self.attempt_root_for_agent, "requests")
        response_dir_for_agent = agent_path_join(self.attempt_root_for_agent, "responses")
        python_payload = _render_client_python(
            request_token=self.request_token,
            request_dir=request_dir_for_agent,
            response_dir=response_dir_for_agent,
            wait_timeout_seconds=wait_timeout_seconds,
        )
        self.python_script.write_text(python_payload, encoding="utf-8", newline="\n")
        self.shell_script.write_text(
            _render_client_shell_wrapper(python_command=python_command),
            encoding="utf-8",
            newline="\n",
        )
        self.powershell_script.write_text(
            _render_client_powershell_wrapper(python_command=python_command),
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
    if status.strip() not in {"passed", "failed", "timed_out", "invalid"}:
        return None, f"invalid broker response status for request_id={request_id}: {status!r}"
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
    while True:
        if response_path.exists():
            response_payload, error = _load_response(response_path, request_id)
            if error is not None:
                print(error, file=sys.stderr)
                return 1
            assert response_payload is not None
            if str(response_payload.get("status") or "").strip() == "passed":
                print(f"verification passed (request_id={request_id})", flush=True)
                return 0
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
    verification_timeout_seconds: float | None,
    verification_command_count: int,
) -> float:
    if verification_timeout_seconds is None or verification_timeout_seconds <= 0:
        return 10_800.0
    command_count = max(1, int(verification_command_count))
    return max(300.0, float(verification_timeout_seconds) * float(command_count) + 300.0)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _quote_powershell_path(path: str) -> str:
    return "'" + path.replace("'", "''") + "'"
