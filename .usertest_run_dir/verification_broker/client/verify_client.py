from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

REQUEST_TOKEN = "35e28fb143bb4c7bb3b46bea4dbda97b"
REQUEST_DIR = Path("I:/code/usertest/runs/usertest_implement/_workspaces/usertest_20260707T032118Z_claude_0/.usertest_run_dir/verification_broker/attempt1/requests")
RESPONSE_DIR = Path("I:/code/usertest/runs/usertest_implement/_workspaces/usertest_20260707T032118Z_claude_0/.usertest_run_dir/verification_broker/attempt1/responses")
WAIT_TIMEOUT_SECONDS = 21645.0
REQUIRED_TERMINAL_ARTIFACT_FIELDS = ('artifacts_dir', 'summary_path')
NO_ARTIFACT_FAILURE_REASONS = ('async_verifier_disabled', 'invalid_request_token', 'runner_shutdown')
NO_ARTIFACT_FAILURE_REASON_PREFIXES = ('invalid_request_json:', 'broker_exception:')
POLL_INTERVAL_SECONDS = 0.2
ALLOWED_STATUSES = set(["cancelled", "cancelling", "failed", "passed", "pending", "running", "timed_out"])
ARTIFACT_REQUIRED_STATUSES = set(["failed", "passed", "timed_out"])
WORKSPACE_HASH_REQUIRED_STATUSES = set(["passed"])
REQUIRE_DEADLINE_UTC = True
REQUIRE_DEADLINE_SECONDS = True


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
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
    missing_artifacts = _missing_required_terminal_artifacts(payload)
    if missing_artifacts:
        missing_list = ", ".join(missing_artifacts)
        return (
            None,
            "incomplete broker response for request_id="
            f"{request_id}: missing required artifact fields: {missing_list}",
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


def _failure_reason_allows_missing_artifacts(failure_reason: str) -> bool:
    normalized = failure_reason.strip()
    if not normalized:
        return False
    if normalized in NO_ARTIFACT_FAILURE_REASONS:
        return True
    return normalized.startswith(tuple(NO_ARTIFACT_FAILURE_REASON_PREFIXES))


def _terminal_response_requires_artifacts(payload: dict[str, object]) -> bool:
    status = str(payload.get("status") or "").strip()
    failure_reason = str(payload.get("failure_reason") or "").strip()
    if status == "passed":
        return True
    if status in {"timed_out", "cancelled", "failed"}:
        return not _failure_reason_allows_missing_artifacts(failure_reason)
    return False


def _missing_required_terminal_artifacts(payload: dict[str, object]) -> list[str]:
    if not _terminal_response_requires_artifacts(payload):
        return []
    missing: list[str] = []
    for field in REQUIRED_TERMINAL_ARTIFACT_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            missing.append(field)
    return missing


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
                and (now - last_heartbeat_monotonic) >= 5.0
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
