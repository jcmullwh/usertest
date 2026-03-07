from __future__ import annotations

import json
import shlex
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runner_core.workspace_state_hash import WorkspaceStateHash


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
        self.client_root_for_agent = client_root_for_agent.rstrip("/\\")
        self.attempt_root_for_agent = attempt_root_for_agent.rstrip("/\\")
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
        artifacts_dir_s = artifacts_dir.strip() if isinstance(artifacts_dir, str) else None
        summary_path = (
            str(Path(artifacts_dir_s) / "verification.json") if artifacts_dir_s else None
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
                "request_id": result.request_id,
                "status": result.status,
                "timed_out": result.timed_out,
                "failure_reason": result.failure_reason,
                "artifacts_dir": result.artifacts_dir,
                "summary_path": result.summary_path,
                "workspace_hash_after_verification": result.workspace_hash_after_verification,
                "workspace_hash_mode": result.workspace_hash_mode,
            },
        )

    def _write_client_files(
        self,
        *,
        python_command: str,
        wait_timeout_seconds: float,
    ) -> VerificationBrokerClient:
        self.client_root.mkdir(parents=True, exist_ok=True)
        request_dir_for_agent = _agent_path_join(self.attempt_root_for_agent, "requests")
        python_payload = _render_client_python(
            request_token=self.request_token,
            request_dir=request_dir_for_agent,
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

        if self.execution_shell == "powershell":
            powershell_client_path = _agent_path_join(
                self.client_root_for_agent,
                "verify_client.ps1",
            )
            command = (
                "powershell -NoProfile -ExecutionPolicy Bypass -File "
                + _quote_powershell_path(powershell_client_path)
            )
            shell_script = None
            powershell_script = self.powershell_script
        else:
            shell_client_path = _agent_path_join(
                self.client_root_for_agent,
                "verify_client.sh",
            )
            command = f"sh {shlex.quote(shell_client_path)}"
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
) -> str:
    request_token_json = json.dumps(request_token)
    request_dir_json = json.dumps(request_dir)
    return f"""from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

REQUEST_TOKEN = {request_token_json}
REQUEST_DIR = Path({request_dir_json})


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False) + "\\n", encoding="utf-8")
    tmp_path.replace(path)


def main() -> int:
    request_id = "req_" + uuid.uuid4().hex
    request_path = REQUEST_DIR / f"{{request_id}}.json"
    _write_json_atomic(
        request_path,
        {{
            "schema_version": 1,
            "request_id": request_id,
            "request_token": REQUEST_TOKEN,
        }},
    )
    print(f"verification requested (request_id={{request_id}})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


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


def _agent_path_join(root: str, leaf: str) -> str:
    if root.endswith(("/", "\\")):
        return f"{root}{leaf}"
    if "\\" in root and "/" not in root:
        return f"{root}\\{leaf}"
    return f"{root}/{leaf}"


def _quote_powershell_path(path: str) -> str:
    return "'" + path.replace("'", "''") + "'"
