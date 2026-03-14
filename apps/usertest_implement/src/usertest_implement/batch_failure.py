from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FAILURE_CLASSES = {
    "ticket_regression",
    "baseline_repo_regression",
    "batch_control_plane",
    "verification_control_plane",
    "infra_transient",
    "registry_or_auth",
    "probe_false_negative",
    "success",
}


def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _read_text_if_exists(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _has_unanswered_broker_request(run_dir: Path) -> bool:
    broker_root = run_dir / "verification_broker"
    if not broker_root.exists():
        return False
    for request_path in broker_root.glob("attempt*/requests/*.json"):
        response_path = request_path.parent.parent / "responses" / request_path.name
        if not response_path.exists():
            return True
    return False


def _infer_registry_or_auth_failure(run_dir: Path) -> dict[str, Any] | None:
    log_text = _read_text_if_exists(run_dir / "bootstrap_pip.log")
    if not log_text:
        return None
    lowered = log_text.lower()
    if "gitlab" not in lowered:
        return None
    if "missing gitlab_pypi_username/gitlab_pypi_password" in lowered:
        return {
            "failure_class": "registry_or_auth",
            "retryable": False,
            "global_blocker": True,
            "summary": "Missing GitLab package index credentials during pip bootstrap.",
            "evidence": {"path": str(run_dir / "bootstrap_pip.log")},
        }
    if (
        "json.decoder.jsondecodeerror" in lowered
        or "expecting value" in lowered
        or "traceback" in lowered
    ) and "gitlab" in lowered:
        return {
            "failure_class": "registry_or_auth",
            "retryable": False,
            "global_blocker": True,
            "summary": "GitLab package index returned malformed content during pip bootstrap.",
            "evidence": {"path": str(run_dir / "bootstrap_pip.log")},
        }
    return None


def _infer_infra_transient_failure(run_dir: Path) -> dict[str, Any] | None:
    log_text = _read_text_if_exists(run_dir / "sandbox" / "maintenance_docker_build.log")
    if not log_text:
        return None
    lowered = log_text.lower()
    markers = (
        "failed to create temp dir",
        "input/output error",
        "containerd",
        "daemon is not running",
        "buildx",
    )
    if any(marker in lowered for marker in markers):
        return {
            "failure_class": "infra_transient",
            "retryable": True,
            "global_blocker": False,
            "summary": "Docker/buildx infrastructure failure during maintenance image setup.",
            "evidence": {"path": str(run_dir / "sandbox" / "maintenance_docker_build.log")},
        }
    return None


def _infer_verification_control_plane_failure(run_dir: Path) -> dict[str, Any] | None:
    if _has_unanswered_broker_request(run_dir):
        return {
            "failure_class": "verification_control_plane",
            "retryable": False,
            "global_blocker": True,
            "summary": "Verification broker request was left without a terminal response.",
            "evidence": {"verification_broker": str(run_dir / "verification_broker")},
        }
    return None


def _infer_probe_false_negative(run_dir: Path) -> dict[str, Any] | None:
    ci_gate = _read_json_if_exists(run_dir / "ci_gate.json")
    if not isinstance(ci_gate, dict):
        return None
    error = ci_gate.get("error")
    if not isinstance(error, str):
        return None
    lowered = error.lower()
    if "bash is required" in lowered and "not usable" in lowered:
        return {
            "failure_class": "probe_false_negative",
            "retryable": False,
            "global_blocker": True,
            "summary": "Environment probe declared bash unusable.",
            "evidence": {"path": str(run_dir / "ci_gate.json"), "error": error},
        }
    return None


def classify_run_outcome(
    *,
    run_dir: Path,
    handoff_summary: dict[str, Any] | None,
    timed_out: bool = False,
    missing_terminal_artifacts: bool = False,
) -> dict[str, Any]:
    if isinstance(handoff_summary, dict):
        if handoff_summary.get("final_status") == "success" and handoff_summary.get(
            "ci_status"
        ) == "success":
            return {
                "failure_class": "success",
                "retryable": False,
                "global_blocker": False,
                "summary": "PR created and CI passed.",
                "evidence": {"pr_url": handoff_summary.get("pr_url")},
            }
        if (
            handoff_summary.get("pr_created") is True
            and handoff_summary.get("ci_status") == "failure"
        ):
            return {
                "failure_class": "ticket_regression",
                "retryable": False,
                "global_blocker": True,
                "summary": "Produced PR went red in CI.",
                "evidence": {
                    "pr_url": handoff_summary.get("pr_url"),
                    "ci_run_url": handoff_summary.get("ci_run_url"),
                },
            }

    for infer in (
        _infer_infra_transient_failure,
        _infer_registry_or_auth_failure,
        _infer_verification_control_plane_failure,
        _infer_probe_false_negative,
    ):
        failure = infer(run_dir)
        if failure is not None:
            return failure

    verification = _read_json_if_exists(run_dir / "verification.json")
    if isinstance(verification, dict) and verification.get("passed") is False:
        return {
            "failure_class": "ticket_regression",
            "retryable": False,
            "global_blocker": False,
            "summary": "Local verification failed for the ticket patch.",
            "evidence": {"path": str(run_dir / "verification.json")},
        }

    if timed_out:
        failure_class = (
            "verification_control_plane"
            if _has_unanswered_broker_request(run_dir)
            else "batch_control_plane"
        )
        return {
            "failure_class": failure_class,
            "retryable": False,
            "global_blocker": True,
            "summary": "Ticket run exceeded the batch watchdog timeout.",
            "evidence": {"run_dir": str(run_dir)},
        }

    if missing_terminal_artifacts:
        return {
            "failure_class": "batch_control_plane",
            "retryable": False,
            "global_blocker": True,
            "summary": "Ticket run exited without required terminal artifacts.",
            "evidence": {"run_dir": str(run_dir)},
        }

    error_doc = _read_json_if_exists(run_dir / "error.json")
    if isinstance(error_doc, dict):
        subtype = error_doc.get("subtype")
        if subtype in {"auth_missing"}:
            return {
                "failure_class": "registry_or_auth",
                "retryable": False,
                "global_blocker": True,
                "summary": "Agent or package registry authentication is missing.",
                "evidence": error_doc,
            }
        if subtype in {"binary_missing", "binary_unusable", "policy_block"}:
            return {
                "failure_class": "batch_control_plane",
                "retryable": False,
                "global_blocker": True,
                "summary": "Batch launched with an unusable agent execution environment.",
                "evidence": error_doc,
            }

    return {
        "failure_class": "batch_control_plane",
        "retryable": False,
        "global_blocker": True,
        "summary": "Batch run failed without a more specific classification.",
        "evidence": {"run_dir": str(run_dir)},
    }


def write_batch_failure(run_dir: Path, failure: dict[str, Any]) -> Path:
    if failure.get("failure_class") not in FAILURE_CLASSES:
        raise ValueError(f"Unknown failure_class: {failure.get('failure_class')!r}")
    path = run_dir / "batch_failure.json"
    payload = {
        "schema_version": 1,
        **failure,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
