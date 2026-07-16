#!/usr/bin/env python
"""Continuously refresh, implement, review, and merge blocker/high backlog tickets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT_FOR_IMPORTS = Path(__file__).resolve().parents[1]
_IMPLEMENT_SRC = _REPO_ROOT_FOR_IMPORTS / "apps" / "usertest_implement" / "src"
if str(_IMPLEMENT_SRC) not in sys.path:
    sys.path.insert(0, str(_IMPLEMENT_SRC))

from backlog_repo import is_generated_backlog_ticket  # noqa: E402

from usertest_implement.batch_state import latest_batch_dir, load_json  # noqa: E402
from usertest_implement.ledger import update_ledger_file  # noqa: E402

_SEVERITY_PATTERN = re.compile(r"^- Severity:\s*`?([^`\r\n]+)`?\s*$", re.MULTILINE)
_EXPORT_KIND_PATTERN = re.compile(r"^- Export kind:\s*`?([^`\r\n]+)`?\s*$", re.MULTILINE)
_FINGERPRINT_PATTERN = re.compile(r"^- Fingerprint:\s*`?([^`\r\n]+)`?\s*$", re.MULTILINE)
LATEST_CODEX_MODEL = "gpt-5.5"


@dataclass
class LoopContext:
    """Shared runtime configuration for the continuous implementation loop."""

    repo_root: Path
    owner_root: Path
    runs_dir: Path
    target: str
    repo_input: str
    settings_path: Path
    settings_profile: str
    backlog_agent: str
    backlog_model: str | None
    implementation_agent: str
    implementation_model: str | None
    review_agent: str
    review_model: str | None
    allowed_severities: set[str]
    cleanup_interval_seconds: float
    log_path: Path
    state_path: Path
    pid_path: Path
    batch_config_path: Path
    implement_python: Path
    backlog_python: Path
    backlog_research_ref: str = "origin/dev"
    backlog_breadth_profile: str = "internal_maintenance"
    gh_bin: str = "gh"
    last_cleanup_monotonic: float = field(default=0.0)
    last_refresh_monotonic: float = field(default=0.0)


def _utc_now_z() -> str:
    """Return the current UTC time formatted as an ISO 8601 Z string."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_log(ctx: LoopContext, message: str) -> None:
    """Append a single log line to the loop log and stderr."""

    line = f"{_utc_now_z()} {message}"
    ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
    with ctx.log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
    try:
        print(line, file=sys.stderr, flush=True)
    except OSError:
        # Detached/background launches can expose an invalid stderr handle on Windows.
        return


def _write_state(ctx: LoopContext, **payload: Any) -> None:
    """Persist the current loop state to JSON for external monitoring."""

    ctx.state_path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "schema_version": 1,
        "updated_at_utc": _utc_now_z(),
        "pid": os.getpid(),
        **payload,
    }
    ctx.state_path.write_text(
        json.dumps(body, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _run_logged(
    ctx: LoopContext,
    argv: list[str],
    *,
    cwd: Path,
    label: str,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess while appending its stdout/stderr to the shared loop log."""

    cmd_text = subprocess.list2cmdline(argv)
    _append_log(ctx, f"RUN {label}: {cmd_text}")
    with ctx.log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{_utc_now_z()} BEGIN {label}\n")
        handle.flush()
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            stdout=handle,
            stderr=handle,
            text=True,
            check=False,
        )
        handle.write(f"{_utc_now_z()} END {label} rc={proc.returncode}\n")
    return proc


def _run_captured(
    ctx: LoopContext,
    argv: list[str],
    *,
    cwd: Path,
    label: str,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and return captured stdout/stderr while logging the command."""

    cmd_text = subprocess.list2cmdline(argv)
    _append_log(ctx, f"RUN {label}: {cmd_text}")
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.stdout.strip():
        _append_log(ctx, f"{label} stdout:\n{proc.stdout.rstrip()}")
    if proc.stderr.strip():
        _append_log(ctx, f"{label} stderr:\n{proc.stderr.rstrip()}")
    _append_log(ctx, f"{label} rc={proc.returncode}")
    return proc


def _docker_healthy(ctx: LoopContext) -> bool:
    """Return True when the local Docker daemon is responsive."""

    proc = _run_captured(
        ctx,
        ["docker", "version", "--format", "{{json .}}"],
        cwd=ctx.repo_root,
        label="docker version",
    )
    return proc.returncode == 0


def _maintenance_cleanup_due(ctx: LoopContext) -> bool:
    """Return True when the maintenance-image cleanup interval has elapsed."""

    if ctx.last_cleanup_monotonic <= 0:
        return True
    return (time.monotonic() - ctx.last_cleanup_monotonic) >= ctx.cleanup_interval_seconds


def _run_maintenance_cleanup(ctx: LoopContext) -> None:
    """Run maintenance-image cleanup best-effort and never raise on failure."""

    proc = _run_logged(
        ctx,
        [
            str(ctx.implement_python),
            "-m",
            "usertest_implement.cli",
            "--repo-root",
            str(ctx.repo_root),
            "maintenance-images",
            "cleanup",
        ],
        cwd=ctx.repo_root,
        label="maintenance-images cleanup",
    )
    ctx.last_cleanup_monotonic = time.monotonic()
    if proc.returncode != 0:
        _append_log(ctx, "WARNING maintenance image cleanup failed; continuing")


def _load_ledger(ctx: LoopContext) -> dict[str, Any]:
    """Load the implementation attempt ledger from `.agents/state`."""

    ledger_path = ctx.owner_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    if not ledger_path.exists():
        return {"actions": {}}
    raw = yaml.safe_load(ledger_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {"actions": {}}
    actions = raw.get("actions")
    if not isinstance(actions, dict):
        raw["actions"] = {}
    return raw


def _find_ticket_path(ctx: LoopContext, fingerprint: str) -> Path | None:
    """Find the current markdown ticket path for a fingerprint anywhere in `.agents/plans`."""

    plans_root = ctx.owner_root / ".agents" / "plans"
    matches = sorted(plans_root.glob(f"*/*{fingerprint}*.md"))
    return matches[0] if matches else None


def _ticket_metadata(ticket_path: Path) -> dict[str, str | None]:
    """Parse the small metadata header from an exported ticket markdown file."""

    text = ticket_path.read_text(encoding="utf-8")
    severity_match = _SEVERITY_PATTERN.search(text)
    export_kind_match = _EXPORT_KIND_PATTERN.search(text)
    fingerprint_match = _FINGERPRINT_PATTERN.search(text)
    return {
        "severity": severity_match.group(1).strip().lower() if severity_match else None,
        "export_kind": export_kind_match.group(1).strip().lower() if export_kind_match else None,
        "fingerprint": fingerprint_match.group(1).strip() if fingerprint_match else None,
    }


def _list_ready_candidates(ctx: LoopContext) -> list[Path]:
    """List ready implementation tickets restricted to blocker/high severities."""

    ready_dir = ctx.owner_root / ".agents" / "plans" / "2 - ready"
    if not ready_dir.exists():
        return []
    candidates: list[Path] = []
    for ticket_path in sorted(ready_dir.glob("*.md")):
        meta = _ticket_metadata(ticket_path)
        if meta["export_kind"] != "implementation":
            continue
        if meta["severity"] not in ctx.allowed_severities:
            continue
        candidates.append(ticket_path)
    return candidates


def _list_export_candidates(ctx: LoopContext) -> list[Path]:
    """List fresh blocker/high implementation tickets directly from the latest export file."""

    export_path = ctx.runs_dir / ctx.target / "_compiled" / f"{ctx.target}.tickets_export.json"
    if not export_path.exists():
        return []
    try:
        doc = json.loads(export_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    exports = doc.get("exports")
    if not isinstance(exports, list):
        return []
    out: list[Path] = []
    seen: set[str] = set()
    for item in exports:
        if not isinstance(item, dict):
            continue
        if str(item.get("export_kind") or "").strip().lower() != "implementation":
            continue
        source_ticket = item.get("source_ticket")
        if not isinstance(source_ticket, dict):
            continue
        if str(source_ticket.get("stage") or "").strip().lower() != "ready_for_ticket":
            continue
        severity = str(source_ticket.get("severity") or "").strip().lower()
        if severity not in ctx.allowed_severities:
            continue
        owner_repo = item.get("owner_repo")
        if not isinstance(owner_repo, dict):
            continue
        idea_path_raw = owner_repo.get("idea_path")
        if not isinstance(idea_path_raw, str) or not idea_path_raw.strip():
            continue
        idea_path = Path(idea_path_raw)
        if not idea_path.exists():
            continue
        idea_key = str(idea_path.resolve()).lower()
        if idea_key in seen:
            continue
        seen.add(idea_key)
        out.append(idea_path.resolve())
    return sorted(out)


def _gh_pr_view(ctx: LoopContext, pr_url: str) -> dict[str, Any] | None:
    """Return a small PR status payload via GitHub CLI or None on failure."""

    proc = _run_captured(
        ctx,
        [
            ctx.gh_bin,
            "pr",
            "view",
            pr_url,
            "--json",
            "url,state,mergedAt,isDraft,headRefOid,mergeable,statusCheckRollup",
        ],
        cwd=ctx.owner_root,
        label=f"gh pr view {pr_url}",
    )
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def _mark_pr_ready(ctx: LoopContext, pr_url: str) -> bool:
    """Publish an already-approved draft without invoking another model review."""

    proc = _run_captured(
        ctx,
        [ctx.gh_bin, "pr", "ready", pr_url],
        cwd=ctx.owner_root,
        label=f"gh pr ready {pr_url}",
    )
    return proc.returncode == 0


def _approved_review_matches_current_head(
    entry: object,
    *,
    pr_url: str,
    current_head_oid: str,
) -> bool:
    """Return True when causal approval is bound to the current PR commit."""

    if not isinstance(entry, dict) or not current_head_oid:
        return False
    reviewed_head_oid = str(entry.get("last_reviewed_head_oid") or "").strip()
    if (
        entry.get("last_review_pr_url") != pr_url
        or str(entry.get("last_review_decision") or "").strip().lower() != "approved"
        or reviewed_head_oid.lower() != current_head_oid.lower()
    ):
        return False
    causal_acceptance = entry.get("last_review_causal_acceptance")
    if causal_acceptance is True:
        return True
    # Reviews recorded before causal acceptance was persisted remain usable only
    # when their exact-head record had already satisfied every merge-ready gate.
    return causal_acceptance is None and entry.get("last_review_merge_ready") is True


def _pr_checks_state(pr_doc: dict[str, Any]) -> str:
    """Classify current status checks as success, pending, or failure."""

    checks_raw = pr_doc.get("statusCheckRollup")
    checks = checks_raw if isinstance(checks_raw, list) else []
    if not checks:
        return "pending"
    failure_states = {
        "ACTION_REQUIRED",
        "CANCELLED",
        "ERROR",
        "FAILURE",
        "STALE",
        "STARTUP_FAILURE",
        "TIMED_OUT",
    }
    pending_states = {
        "EXPECTED",
        "IN_PROGRESS",
        "PENDING",
        "QUEUED",
        "REQUESTED",
        "WAITING",
    }
    accepted_terminal_states = {"NEUTRAL", "SKIPPED", "SUCCESS"}
    saw_success = False
    saw_pending = False
    for check_raw in checks:
        if not isinstance(check_raw, dict):
            saw_pending = True
            continue
        conclusion = str(check_raw.get("conclusion") or "").strip().upper()
        state = str(check_raw.get("state") or "").strip().upper()
        status = str(check_raw.get("status") or "").strip().upper()
        result = conclusion or state
        if result in failure_states:
            return "failure"
        if result == "SUCCESS":
            saw_success = True
            continue
        if result in pending_states or status in pending_states:
            saw_pending = True
            continue
        if result in accepted_terminal_states:
            continue
        # A completed check without a recognized conclusion is not evidence of
        # a terminal-green gate. Unknown provider states are likewise pending.
        saw_pending = True
    if saw_pending or not saw_success:
        return "pending"
    return "success"


def _operational_blocker_evidence(
    *,
    pr_doc: dict[str, Any],
    pr_url: str,
    classification: str,
) -> dict[str, Any]:
    """Build a stable identity for one actionable live PR blocker."""

    checks_raw = pr_doc.get("statusCheckRollup")
    checks = checks_raw if isinstance(checks_raw, list) else []
    normalized_checks: list[dict[str, Any]] = []
    stable_failing_checks: list[dict[str, Any]] = []
    failure_states = {
        "ACTION_REQUIRED",
        "CANCELLED",
        "ERROR",
        "FAILURE",
        "STALE",
        "STARTUP_FAILURE",
        "TIMED_OUT",
    }
    for raw in checks:
        if not isinstance(raw, dict):
            continue
        normalized = {
            key: raw.get(key)
            for key in (
                "name",
                "context",
                "status",
                "state",
                "conclusion",
                "detailsUrl",
                "targetUrl",
                "workflowName",
            )
            if raw.get(key) is not None
        }
        if normalized:
            normalized_checks.append(normalized)
        conclusion = str(raw.get("conclusion") or "").strip().upper()
        state = str(raw.get("state") or "").strip().upper()
        result = conclusion or state
        if result in failure_states:
            stable_failing_checks.append(
                {
                    key: value
                    for key, value in {
                        "name": str(raw.get("name") or "").strip() or None,
                        "context": str(raw.get("context") or "").strip() or None,
                        "workflow_name": (
                            str(raw.get("workflowName") or "").strip() or None
                        ),
                        "result": result,
                    }.items()
                    if value is not None
                }
            )
    normalized_checks.sort(
        key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False)
    )
    stable_failing_checks.sort(
        key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False)
    )
    evidence_payload = {
        "schema_version": 1,
        "classification": classification,
        "pr_url": pr_url,
        "head_oid": str(pr_doc.get("headRefOid") or "").strip().lower(),
        "mergeable": str(pr_doc.get("mergeable") or "").strip().upper() or "UNKNOWN",
        "checks_state": _pr_checks_state(pr_doc),
        "checks": normalized_checks,
    }
    # URLs remain in evidence for the correcting author, but they are not
    # progress identity: GitHub frequently issues a new details URL for the
    # same failing check rerun. Failure membership/result and exact head are.
    identity_payload = {
        "schema_version": 1,
        "classification": classification,
        "head_oid": evidence_payload["head_oid"],
        "failing_checks": stable_failing_checks,
    }
    encoded = json.dumps(
        identity_payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **evidence_payload,
        "identity_basis": identity_payload,
        "evidence_id": "pr_operational_blocker:" + hashlib.sha256(encoded).hexdigest(),
    }


def _record_operational_correction(
    ctx: LoopContext,
    *,
    fingerprint: str,
    entry: dict[str, Any],
    evidence: dict[str, Any],
    status: str,
    reason: str | None = None,
) -> None:
    """Persist correction lineage without rewriting the accepted review record."""

    recorded_at = _utc_now_z()
    record = {
        **evidence,
        "status": status,
        "recorded_at_utc": recorded_at,
        "reason": reason,
    }
    update_ledger_file(
        ctx.owner_root / ".agents" / "state" / "backlog_implement_actions.yaml",
        fingerprint=fingerprint,
        updates={
            "last_operational_correction": record,
            "last_review_approval_invalidation": {
                "status": "invalidated_while_operationally_blocked",
                "evidence_id": evidence["evidence_id"],
                "classification": evidence["classification"],
                "pr_url": evidence["pr_url"],
                "head_oid": evidence["head_oid"],
                "prior_review_decision": entry.get("last_review_decision"),
                "prior_causal_acceptance": entry.get("last_review_causal_acceptance"),
                "prior_reviewed_head_oid": entry.get("last_reviewed_head_oid"),
                "recorded_at_utc": recorded_at,
            },
        },
    )


def _operational_correction_instruction(evidence: dict[str, Any]) -> str:
    classification = str(evidence.get("classification") or "")
    if classification == "terminal_ci_failure":
        action = (
            "The previously accepted PR head now has a terminal CI failure. Inspect the "
            "named failing checks and their logs, correct the underlying code/test/configuration "
            "on this existing PR, and rerun the relevant verification."
        )
    else:
        action = (
            "The previously accepted PR head is now definitively CONFLICTING with its target "
            "branch. Reconcile the target branch into this retained PR workspace, resolve the "
            "actual conflicts without discarding accepted work, and rerun affected verification."
        )
    return (
        f"{action}\n\nRunner-owned operational blocker evidence:\n"
        + json.dumps(evidence, indent=2, ensure_ascii=False)
    )


def _route_operational_correction(
    ctx: LoopContext,
    *,
    fingerprint: str,
    entry: dict[str, Any],
    evidence: dict[str, Any],
) -> bool:
    """Run at most one same-author correction for one exact-head blocker."""

    previous = entry.get("last_operational_correction")
    if (
        isinstance(previous, dict)
        and previous.get("evidence_id") == evidence.get("evidence_id")
        and str(previous.get("status") or "").strip().lower()
        in {"scheduled", "resume_completed", "blocked_nonprogress"}
    ):
        status = str(previous.get("status") or "").strip().lower()
        if status != "blocked_nonprogress":
            _record_operational_correction(
                ctx,
                fingerprint=fingerprint,
                entry=entry,
                evidence=evidence,
                status="blocked_nonprogress",
                reason=(
                    "The same terminal blocker remained on the same PR head after the bounded "
                    "same-author correction attempt. Automatic repetition is suppressed."
                ),
            )
        _append_log(
            ctx,
            "Operational correction made no observable progress on the same PR head; "
            f"state=blocked_nonprogress evidence={evidence['evidence_id']} "
            f"ticket={fingerprint}",
        )
        return True

    _record_operational_correction(
        ctx,
        fingerprint=fingerprint,
        entry=entry,
        evidence=evidence,
        status="scheduled",
    )
    succeeded = _resume_review_changes_requested(
        ctx,
        fingerprint,
        label=f"resume {evidence['classification']}",
        allowed_lifecycles={
            "awaiting_review",
            "ci_failed",
            "merge_ready",
            "review_changes_requested",
        },
        supervisor_instructions=[_operational_correction_instruction(evidence)],
    )
    refreshed = _load_ledger(ctx)
    actions = refreshed.get("actions")
    refreshed_entry = (
        actions.get(fingerprint)
        if isinstance(actions, dict) and isinstance(actions.get(fingerprint), dict)
        else entry
    )
    _record_operational_correction(
        ctx,
        fingerprint=fingerprint,
        entry=refreshed_entry,
        evidence=evidence,
        status="resume_completed" if succeeded else "resume_failed",
        reason=None if succeeded else "The same-author resume command returned nonzero.",
    )
    return succeeded


def _move_ticket(ctx: LoopContext, fingerprint: str, to_bucket: str) -> bool:
    """Move a ticket between plan buckets through the CLI and return success."""

    proc = _run_logged(
        ctx,
        [
            str(ctx.implement_python),
            "-m",
            "usertest_implement.cli",
            "--repo-root",
            str(ctx.repo_root),
            "tickets",
            "move",
            "--owner-root",
            str(ctx.owner_root),
            "--fingerprint",
            fingerprint,
            "--to-bucket",
            to_bucket,
        ],
        cwd=ctx.repo_root,
        label=f"tickets move {fingerprint} -> {to_bucket}",
    )
    return proc.returncode == 0


def _run_review(ctx: LoopContext, fingerprint: str) -> bool:
    """Run the explicit review stage for a PR-backed ticket."""

    if not _docker_healthy(ctx):
        _write_state(
            ctx,
            status="waiting_for_docker",
            current_action="review",
            current_ticket=fingerprint,
        )
        return False
    argv = [
        str(ctx.implement_python),
        "-m",
        "usertest_implement.cli",
        "--repo-root",
        str(ctx.repo_root),
        "review",
        "run",
        "--owner-root",
        str(ctx.owner_root),
        "--fingerprint",
        fingerprint,
        "--agent",
        ctx.review_agent,
        "--ledger",
        str(ctx.owner_root / ".agents" / "state" / "backlog_implement_actions.yaml"),
    ]
    if ctx.review_model:
        argv.extend(["--model", ctx.review_model])
    proc = _run_logged(
        ctx,
        argv,
        cwd=ctx.repo_root,
        label=f"review run {fingerprint}",
    )
    return proc.returncode == 0


def _resume_review_changes_requested(
    ctx: LoopContext,
    fingerprint: str,
    *,
    label: str = "resume review changes requested",
    allowed_lifecycles: set[str] | None = None,
    supervisor_instructions: list[str] | None = None,
) -> bool:
    """Send requested review changes through the existing durable PR resume path."""

    ledger = _load_ledger(ctx)
    actions_raw = ledger.get("actions")
    entry = actions_raw.get(fingerprint) if isinstance(actions_raw, dict) else None
    if not isinstance(entry, dict):
        return False
    run_dir_raw = entry.get("last_run_dir")
    lifecycle = str(entry.get("last_resume_lifecycle_state") or "").strip().lower()
    accepted_lifecycles = allowed_lifecycles or {"review_changes_requested"}
    if (
        not isinstance(run_dir_raw, str)
        or not run_dir_raw.strip()
        or lifecycle not in accepted_lifecycles
    ):
        return False
    argv = [
        str(ctx.implement_python),
        "-m",
        "usertest_implement.cli",
        "--repo-root",
        str(ctx.repo_root),
        "resume",
        "--run-dir",
        run_dir_raw,
        "--repo",
        ctx.repo_input,
        "--agent",
        ctx.implementation_agent,
        "--correction-origin",
        "system_self_correction",
        "--ledger",
        str(ctx.owner_root / ".agents" / "state" / "backlog_implement_actions.yaml"),
    ]
    if ctx.implementation_model:
        argv.extend(["--model", ctx.implementation_model])
    for instruction in supervisor_instructions or []:
        if instruction.strip():
            argv.extend(["--supervisor-instruction", instruction.strip()])
    proc = _run_logged(
        ctx,
        argv,
        cwd=ctx.repo_root,
        label=f"{label} {fingerprint}",
    )
    return proc.returncode == 0


def _review_changes_requested(entry: object, pr_url: str) -> bool:
    """Return True only for an unconsumed changes-requested review decision."""

    if not isinstance(entry, dict):
        return False
    return bool(
        entry.get("last_review_pr_url") == pr_url
        and str(entry.get("last_review_decision") or "").strip().lower()
        == "changes_requested"
        and entry.get("last_review_merge_ready") is not True
        and str(entry.get("last_resume_lifecycle_state") or "").strip().lower()
        == "review_changes_requested"
    )


def _merge_review(ctx: LoopContext, fingerprint: str) -> bool:
    """Attempt merge; a failed causal proof resumes the same PR instead of stalling."""

    proc = _run_logged(
        ctx,
        [
            str(ctx.implement_python),
            "-m",
            "usertest_implement.cli",
            "--repo-root",
            str(ctx.repo_root),
            "review",
            "merge",
            "--owner-root",
            str(ctx.owner_root),
            "--fingerprint",
            fingerprint,
            "--ledger",
            str(ctx.owner_root / ".agents" / "state" / "backlog_implement_actions.yaml"),
        ],
        cwd=ctx.repo_root,
        label=f"review merge {fingerprint}",
    )
    if proc.returncode == 4:
        return _resume_review_changes_requested(
            ctx,
            fingerprint,
            label="resume failed original scenario",
        )
    return proc.returncode == 0


def _reconcile_review_queue(ctx: LoopContext) -> bool:
    """Reconcile PRs while keeping incomplete outcome proof case-local."""

    def merged_outcome_pending(fingerprint: str, pr_url: str) -> bool:
        refreshed = _load_ledger(ctx)
        actions_raw = refreshed.get("actions")
        entry = actions_raw.get(fingerprint) if isinstance(actions_raw, dict) else None
        if not isinstance(entry, dict):
            return False
        state = (
            str(
                entry.get("last_outcome_state")
                or (
                    entry.get("outcome", {}).get("state")
                    if isinstance(entry.get("outcome"), dict)
                    else ""
                )
            )
            .strip()
            .lower()
        )
        return bool(
            entry.get("last_merge_pr_url") == pr_url
            and isinstance(entry.get("last_merged_at"), str)
            and str(entry.get("last_merged_at")).strip()
            and state not in {"resolved", "mitigated"}
        )

    ledger = _load_ledger(ctx)
    actions = ledger.get("actions", {})
    if not isinstance(actions, dict):
        return True
    for fingerprint, entry_raw in sorted(actions.items()):
        if not isinstance(entry_raw, dict):
            continue
        pr_url = entry_raw.get("last_pr_url") or entry_raw.get("last_review_pr_url")
        if not isinstance(pr_url, str) or not pr_url.strip():
            continue
        ticket_path = _find_ticket_path(ctx, fingerprint)
        if ticket_path is None:
            continue
        try:
            ticket_markdown = ticket_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if not is_generated_backlog_ticket(ticket_markdown):
            # This loop is the automated backlog worker.  IDEA and other
            # externally originated plans may share the same implementation
            # metadata, but their review/merge lifecycle remains separate.
            continue
        if (
            ticket_path.parent.name == "5 - complete"
            and entry_raw.get("last_merge_pr_url") == pr_url
            and isinstance(entry_raw.get("last_merged_at"), str)
            and str(entry_raw.get("last_merged_at")).strip()
            and str(
                entry_raw.get("last_outcome_state")
                or (
                    entry_raw.get("outcome", {}).get("state")
                    if isinstance(entry_raw.get("outcome"), dict)
                    else ""
                )
            )
            .strip()
            .lower()
            in {"resolved", "mitigated"}
        ):
            continue
        pr_doc = _gh_pr_view(ctx, pr_url)
        if not isinstance(pr_doc, dict):
            continue
        if pr_doc.get("mergedAt"):
            # An out-of-band/already-completed merge still needs the normal merge
            # finalizer. It writes merge provenance and drives original/live outcome
            # roles; moving the file alone would falsely treat workflow motion as proof.
            if not _merge_review(ctx, fingerprint):
                if merged_outcome_pending(fingerprint, pr_url):
                    _append_log(
                        ctx,
                        "Merged PR outcome progression remains case-locally pending; "
                        f"continuing unrelated backlog work for {fingerprint}",
                    )
                    continue
                return False
            continue
        state = str(pr_doc.get("state") or "").upper()
        if state == "CLOSED":
            if ticket_path.parent.name == "4 - for_review":
                _move_ticket(ctx, fingerprint, "2 - ready")
            continue
        if state != "OPEN":
            continue
        if ticket_path.parent.name != "4 - for_review":
            _move_ticket(ctx, fingerprint, "4 - for_review")
        current_head_oid = str(pr_doc.get("headRefOid") or "").strip()
        causal_approval_current = _approved_review_matches_current_head(
            entry_raw,
            pr_url=pr_url,
            current_head_oid=current_head_oid,
        )
        if not causal_approval_current and _review_changes_requested(entry_raw, pr_url):
            if not _resume_review_changes_requested(ctx, fingerprint):
                return False
            # One correction transition is enough for this reconciliation pass.
            # The next pass reviews the new head written by the resumed author.
            continue
        if not causal_approval_current:
            if not _run_review(ctx, fingerprint):
                continue
            refreshed_pr_doc = _gh_pr_view(ctx, pr_url)
            if isinstance(refreshed_pr_doc, dict):
                pr_doc = refreshed_pr_doc
                current_head_oid = str(pr_doc.get("headRefOid") or "").strip()
            ledger = _load_ledger(ctx)
            entry_raw = (
                ledger.get("actions", {}).get(fingerprint, {})
                if isinstance(ledger.get("actions"), dict)
                else {}
            )
            causal_approval_current = _approved_review_matches_current_head(
                entry_raw,
                pr_url=pr_url,
                current_head_oid=current_head_oid,
            )
            if not causal_approval_current and _review_changes_requested(entry_raw, pr_url):
                if not _resume_review_changes_requested(ctx, fingerprint):
                    return False
                # Do not immediately review the correction in an inner loop.
                continue
        if not causal_approval_current:
            continue
        if pr_doc.get("isDraft") is True:
            if not _mark_pr_ready(ctx, pr_url):
                _append_log(
                    ctx,
                    f"Approved draft PR could not be marked ready; will retry {fingerprint}",
                )
            continue
        checks_state = _pr_checks_state(pr_doc)
        if checks_state == "failure":
            evidence = _operational_blocker_evidence(
                pr_doc=pr_doc,
                pr_url=pr_url,
                classification="terminal_ci_failure",
            )
            if not _route_operational_correction(
                ctx,
                fingerprint=fingerprint,
                entry=entry_raw,
                evidence=evidence,
            ):
                return False
            continue
        if checks_state != "success":
            _append_log(
                ctx,
                "Approved unchanged PR head is waiting for terminal-green CI; "
                f"state={checks_state} ticket={fingerprint}",
            )
            continue
        mergeable = str(pr_doc.get("mergeable") or "").strip().upper()
        if mergeable == "CONFLICTING":
            evidence = _operational_blocker_evidence(
                pr_doc=pr_doc,
                pr_url=pr_url,
                classification="merge_conflict",
            )
            if not _route_operational_correction(
                ctx,
                fingerprint=fingerprint,
                entry=entry_raw,
                evidence=evidence,
            ):
                return False
            continue
        if mergeable != "MERGEABLE":
            _append_log(
                ctx,
                "Approved unchanged PR head has green CI but is not currently mergeable; "
                f"state={mergeable or 'UNKNOWN'} ticket={fingerprint}",
            )
            continue
        if not _merge_review(ctx, fingerprint):
            if merged_outcome_pending(fingerprint, pr_url):
                _append_log(
                    ctx,
                    "Merged PR outcome progression remains case-locally pending; "
                    f"continuing unrelated backlog work for {fingerprint}",
                )
                continue
            return False
    return True


def _run_ticket(ctx: LoopContext, ticket_path: Path) -> bool:
    """Run one implementation ticket through commit/push/PR and automatic review."""

    if not _docker_healthy(ctx):
        _write_state(
            ctx,
            status="waiting_for_docker",
            current_action="implement",
            current_ticket=str(ticket_path),
        )
        return False
    argv = [
        str(ctx.implement_python),
        "-m",
        "usertest_implement.cli",
        "--repo-root",
        str(ctx.repo_root),
        "run",
        "--settings",
        str(ctx.settings_path),
        "--settings-profile",
        ctx.settings_profile,
        "--ticket-path",
        str(ticket_path),
        "--repo",
        ctx.repo_input,
        "--ref",
        "dev",
        "--runs-dir",
        str(ctx.owner_root / "runs" / "usertest_implement"),
        "--ledger",
        str(ctx.owner_root / ".agents" / "state" / "backlog_implement_actions.yaml"),
        "--agent",
        ctx.implementation_agent,
    ]
    if ctx.implementation_model:
        argv.extend(["--model", ctx.implementation_model])
    argv.extend(["--implementation-review-agent", ctx.review_agent])
    if ctx.review_model:
        argv.extend(["--implementation-review-model", ctx.review_model])
    _write_state(
        ctx,
        status="running",
        current_action="implement",
        current_ticket=str(ticket_path),
    )
    proc = _run_logged(ctx, argv, cwd=ctx.repo_root, label=f"implement {ticket_path.name}")
    return proc.returncode == 0


def _run_batch_pass(ctx: LoopContext) -> bool:
    """Run one full maintenance batch pass through the batch engine."""

    _write_state(ctx, status="running", current_action="batch")
    proc = _run_logged(
        ctx,
        [
            str(ctx.implement_python),
            "-m",
            "usertest_implement.cli",
            "--repo-root",
            str(ctx.repo_root),
            "batch",
            "run",
            "--config",
            str(ctx.batch_config_path),
        ],
        cwd=ctx.repo_root,
        label="batch run",
    )
    return proc.returncode == 0


def _latest_passing_terminal_proof(ctx: LoopContext) -> dict[str, Any] | None:
    """Return the hash-verified final-zero proof for the latest batch, if any."""

    latest = latest_batch_dir(ctx.owner_root)
    if latest is None:
        return None
    state = load_json(latest / "batch_state.json")
    if state is None or state.get("status") != "completed":
        return None
    summary_raw = state.get("terminal_proof")
    summary = summary_raw if isinstance(summary_raw, dict) else {}
    path_raw = summary.get("path")
    if not isinstance(path_raw, str) or not path_raw.strip():
        return None
    proof_path = Path(path_raw).resolve()
    if not proof_path.is_relative_to(latest.resolve()) or not proof_path.is_file():
        return None
    payload = proof_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != summary.get("sha256"):
        return None
    try:
        proof = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(proof, dict) or proof.get("passed") is not True:
        return None
    recorded_content_hash = proof.get("proof_sha256")
    unhashed = dict(proof)
    unhashed.pop("proof_sha256", None)
    computed_content_hash = hashlib.sha256(
        json.dumps(
            unhashed,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if recorded_content_hash != computed_content_hash:
        return None
    if summary.get("proof_sha256") != recorded_content_hash:
        return None
    return proof


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the continuous implementation loop."""

    parser = argparse.ArgumentParser(
        prog="continuous_implement_loop",
        description=(
            "Continuously refresh backlog tickets, implement blocker/high items, "
            "and merge approved PRs."
        ),
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--owner-root", type=Path, default=Path.cwd())
    parser.add_argument("--runs-dir", type=Path, default=Path("runs/usertest_implement"))
    parser.add_argument("--target", default="usertest")
    parser.add_argument("--repo-input", default=None)
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path("configs/usertest_implement_settings.yaml"),
    )
    parser.add_argument("--settings-profile", default="default")
    parser.add_argument("--backlog-agent", choices=["claude", "codex", "gemini"], default="codex")
    parser.add_argument("--backlog-model", default=LATEST_CODEX_MODEL)
    parser.add_argument("--backlog-research-ref", default="origin/dev")
    parser.add_argument(
        "--backlog-breadth-profile",
        choices=["external_generalization", "internal_maintenance"],
        default="internal_maintenance",
    )
    parser.add_argument(
        "--implementation-agent", choices=["claude", "codex", "gemini"], default="codex"
    )
    parser.add_argument("--implementation-model", default=None)
    parser.add_argument("--review-agent", choices=["claude", "codex", "gemini"], default="claude")
    parser.add_argument("--review-model", default=None)
    parser.add_argument(
        "--severity",
        dest="severities",
        action="append",
        default=["blocker", "high"],
        help="Repeatable severity filter for ready-ticket selection.",
    )
    parser.add_argument("--cleanup-interval-seconds", type=float, default=21600.0)
    parser.add_argument(
        "--log-path",
        type=Path,
        default=Path("runs/_continuous_loop/continuous_loop.log"),
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path("runs/_continuous_loop/loop_state.json"),
    )
    parser.add_argument(
        "--pid-path",
        type=Path,
        default=Path("runs/_continuous_loop/loop.pid"),
    )
    parser.add_argument(
        "--batch-config",
        type=Path,
        default=Path("configs/backlog_implement_batch.yaml"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the continuous implementation loop until externally terminated."""

    parser = _build_parser()
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    owner_root = args.owner_root.resolve()
    runs_dir = (
        args.runs_dir if args.runs_dir.is_absolute() else (owner_root / args.runs_dir).resolve()
    )
    settings_path = (
        args.settings if args.settings.is_absolute() else (repo_root / args.settings).resolve()
    )
    batch_config_path = (
        args.batch_config
        if args.batch_config.is_absolute()
        else (repo_root / args.batch_config).resolve()
    )
    ctx = LoopContext(
        repo_root=repo_root,
        owner_root=owner_root,
        runs_dir=runs_dir,
        target=str(args.target),
        repo_input=(
            str(args.repo_input).strip()
            if isinstance(args.repo_input, str) and str(args.repo_input).strip()
            else str(owner_root)
        ),
        settings_path=settings_path,
        settings_profile=str(args.settings_profile),
        backlog_agent=str(args.backlog_agent),
        backlog_model=str(args.backlog_model).strip()
        if isinstance(args.backlog_model, str) and args.backlog_model.strip()
        else None,
        backlog_research_ref=str(args.backlog_research_ref).strip(),
        backlog_breadth_profile=str(args.backlog_breadth_profile).strip(),
        implementation_agent=str(args.implementation_agent),
        implementation_model=str(args.implementation_model).strip()
        if isinstance(args.implementation_model, str)
        and args.implementation_model
        and str(args.implementation_model).strip()
        else None,
        review_agent=str(args.review_agent),
        review_model=str(args.review_model).strip()
        if isinstance(args.review_model, str)
        and args.review_model
        and str(args.review_model).strip()
        else None,
        allowed_severities={
            str(value).strip().lower() for value in args.severities if str(value).strip()
        },
        cleanup_interval_seconds=float(args.cleanup_interval_seconds),
        log_path=args.log_path
        if args.log_path.is_absolute()
        else (owner_root / args.log_path).resolve(),
        state_path=args.state_path
        if args.state_path.is_absolute()
        else (owner_root / args.state_path).resolve(),
        pid_path=args.pid_path
        if args.pid_path.is_absolute()
        else (owner_root / args.pid_path).resolve(),
        batch_config_path=batch_config_path,
        implement_python=(
            repo_root / "apps" / "usertest_implement" / ".venv" / "Scripts" / "python.exe"
        ).resolve(),
        backlog_python=(
            repo_root / "apps" / "usertest_backlog" / ".venv" / "Scripts" / "python.exe"
        ).resolve(),
    )

    ctx.pid_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    _append_log(ctx, "continuous implementation loop starting")

    while True:
        try:
            _write_state(ctx, status="running", current_action="startup")
            if _maintenance_cleanup_due(ctx):
                _run_maintenance_cleanup(ctx)

            if not _reconcile_review_queue(ctx):
                _write_state(
                    ctx,
                    status="waiting_for_outcome_verification",
                    current_action="post_merge_outcome_progression",
                )
                return 2
            batch_ok = _run_batch_pass(ctx)
            if not _reconcile_review_queue(ctx):
                _write_state(
                    ctx,
                    status="waiting_for_outcome_verification",
                    current_action="post_merge_outcome_progression",
                )
                return 2
            if not batch_ok:
                _append_log(ctx, "batch pass returned non-zero; stopping continuous loop")
                _write_state(
                    ctx,
                    status="failed",
                    current_action="stopped_after_batch_failure",
                )
                return 1

            terminal_proof = _latest_passing_terminal_proof(ctx)
            if terminal_proof is not None:
                _append_log(
                    ctx,
                    "automated backlog reached hash-verified final zero; stopping loop",
                )
                _write_state(
                    ctx,
                    status="completed",
                    current_action="terminal_proof",
                    terminal_proof=terminal_proof,
                )
                return 0

            _append_log(ctx, "batch pass complete; starting next pass")
            _write_state(ctx, status="running", current_action="next_batch")
        except KeyboardInterrupt:
            _append_log(ctx, "continuous implementation loop interrupted")
            return 130
        except Exception as exc:
            _append_log(ctx, f"UNHANDLED ERROR: {exc!r}; stopping continuous loop")
            _write_state(
                ctx,
                status="error",
                current_action="exception",
                error=repr(exc),
            )
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
