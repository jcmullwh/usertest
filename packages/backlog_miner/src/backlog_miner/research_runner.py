"""Stage-3 reproduce-plus-research runner.

This module implements the stage-3 orchestration described in
`.agents/ops/backlog-six-stage-pipeline/backlog-six-stage-pipeline.execplan.md`.

Stage 3 is operationally distinct from the prompt-only stages: it runs a dedicated
mission inside an isolated writable workspace (via ``runner_core.run_once``) and
extracts a strict research-dossier extension block from the resulting report.

Offline testability
-------------------
Tests must run offline. In ``dry_run`` mode, this module does not invoke any agent;
it instead writes request artifacts and returns deterministic placeholder dossiers
that satisfy the stage contract without claiming reproduction.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from backlog_core.stage_contracts import (
    RESEARCH_PROOF_SCHEMA_VERSION,
    build_stage_document,
    evidence_assignment_sha256,
    evidence_verification_sha256,
    parse_research_dossier_list,
    research_attempt_sha256,
    research_claims_sha256,
    research_dossier_output_contract_errors,
)
from runner_core import RunnerConfig, RunRequest, run_once
from runner_core.runner import validate_report
from runner_core.target_acquire import acquire_target

from backlog_miner.origin_evidence import (
    materialize_origin_attachments,
    origin_attachment_requirements,
    verify_materialized_origin_attachments,
)
from backlog_miner.research_evidence import (
    ReplayExecutor,
    _persisted_research_attempt_errors,
    _research_attempt_event_source_binding,
    verify_persisted_research_evidence,
    verify_research_evidence,
)

_LOG = logging.getLogger(__name__)

_MISSION_ID = "backlog_repro_research"
_REPAIR_MISSION_ID = "backlog_repro_research_dossier_repair"
_PERSONA_ID = "repo_backlog_investigator"
_POLICY = "write"

_STAGE = "repro_research"

_GUIDANCE_PATH = Path("configs") / "backlog_stage_guidance" / "repro_research.md"
_REPO_INTENT_PATH = Path("configs") / "repo_intent.md"

_EXTENSION_KEY = "backlog_repro_research"
_REPEATED_CORRECTION_STATE_RESTART_COUNT = 3

_RUNNER_OWNED_DOSSIER_FIELDS: frozenset[str] = frozenset(
    {
        "research_schema_version",
        "repo_revision",
        "diff_classification",
        "evidence_assignment",
        "evidence_verification",
        "repo_workspace",
        "run_dir",
        "runner_exit_code",
        "runner_report_validation_errors",
        "diff_suspicious_reasons",
        "artifacts",
        "post_research_same_mechanism_bundle",
        "research_attempts",
    }
)

# Stage-3 Codex may inspect repository state and explore candidate replays. This is
# a coarse in-agent capability policy, not an evidence authorization contract:
# broad interpreter/shell prefixes and POSIX project rules remain residual policy
# debt. Accepted experiments are independently re-executed and property-gated by
# research_evidence (shell-free argv, confinement, immutable source or tracked
# repository bindings, isolation, and runner attestation). Do not add ecosystem
# names here as a substitute for that generic verifier.
_CODEX_RESEARCH_EXEC_ALLOW_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("git", "status"),
    ("git", "diff"),
    ("git", "show"),
    ("git", "log"),
    ("git", "rev-parse"),
    ("git", "branch"),
    ("git", "blame"),
    ("git", "ls-files"),
    ("git", "ls-tree"),
    ("git", "cat-file"),
    ("git", "grep"),
    ("pytest",),
    ("python", "-m", "pytest"),
    ("python",),
    ("powershell.exe",),
    ("powershell",),
    ("pwsh",),
    ("node",),
    ("ruby",),
    ("perl",),
    ("php",),
    ("java",),
    ("deno",),
    ("bun",),
    ("bash",),
    ("sh",),
    ("pdm", "run", "pytest"),
    ("pdm", "run", "python", "-m", "pytest"),
    ("npm", "test"),
    ("npm", "run", "test"),
    ("pnpm", "test"),
    ("pnpm", "run", "test"),
    ("yarn", "test"),
    ("yarn", "run", "test"),
    ("cargo", "test"),
    ("cargo", "run"),
    ("go", "test"),
    ("go", "run"),
    ("dotnet", "test"),
    ("dotnet", "run"),
    ("mvn",),
    ("gradle",),
    ("gradlew",),
    ("docker", "version"),
    ("docker", "info"),
    ("Get-FileHash",),
    ("Get-Command",),
    ("Test-Path",),
)

_VERIFIED_MECHANISM_PROJECTION_FIELDS: tuple[str, ...] = (
    "verified_mechanism",
    "verified_mechanism_sha256",
    "verified_mechanism_provenance",
    "verified_mechanism_provenance_sha256",
)


def _has_origin_attachment_refs(atoms: Sequence[dict[str, Any]]) -> bool:
    return any(
        isinstance(attachment, dict) and isinstance(attachment.get("artifact_ref"), dict)
        for atom in atoms
        for attachment in (
            atom.get("attachments") if isinstance(atom.get("attachments"), list) else []
        )
    )


def _prepare_origin_evidence_workspace(
    *,
    repo_input: str,
    repo_ref: str,
    preferred_workspace_dir: Path,
    evidence_atoms: Sequence[dict[str, Any]],
    source_root: Path,
) -> tuple[Path, dict[str, Any]]:
    acquired = acquire_target(
        repo=repo_input,
        dest_dir=preferred_workspace_dir,
        ref=repo_ref,
    )
    manifest = materialize_origin_attachments(
        atoms=evidence_atoms,
        workspace_dir=acquired.workspace_dir,
        source_root=source_root,
        relative_root=Path(".usertest_research") / "origin_evidence",
    )
    return acquired.workspace_dir, manifest


def _origin_attachment_read_receipts(
    *,
    run_dir: Path,
    workspace_dir: Path,
    manifest: dict[str, Any],
    evidence_attempts: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    errors = verify_materialized_origin_attachments(
        workspace_dir=workspace_dir,
        manifest=manifest,
    )
    events: list[dict[str, Any]] = []
    current_run_dir = run_dir.resolve()
    ordered_run_dirs: list[Path] = []
    seen_run_dirs: set[Path] = set()
    for attempt in evidence_attempts:
        raw_run_dir = _coerce_str(attempt.get("run_dir"))
        if raw_run_dir is None:
            continue
        source_run_dir = Path(raw_run_dir).resolve()
        if source_run_dir in seen_run_dirs:
            continue
        seen_run_dirs.add(source_run_dir)
        ordered_run_dirs.append(source_run_dir)
    if current_run_dir not in seen_run_dirs:
        ordered_run_dirs.append(current_run_dir)
    for source_index, source_run_dir in enumerate(ordered_run_dirs):
        events_path = source_run_dir / "normalized_events.jsonl"
        if not events_path.is_file():
            return [], [
                *errors,
                f"origin_attachment_normalized_events_missing:{source_index}",
            ]
        try:
            for line_number, line in enumerate(
                events_path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if not line.strip():
                    continue
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError(f"event {line_number} is not an object")
                events.append(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return [], [
                *errors,
                f"origin_attachment_normalized_events_unreadable:{source_index}",
            ]

    receipts: list[dict[str, Any]] = []
    observed: set[str] = set()
    for requirement in origin_attachment_requirements(manifest):
        rel_path = str(requirement["file"])
        expected_sha = str(requirement["sha256"])
        path = (workspace_dir / Path(rel_path)).resolve()
        try:
            path.relative_to(workspace_dir.resolve())
        except ValueError:
            errors.append(f"origin_attachment_read_outside_workspace:{rel_path}")
            continue
        for event_index, event in reversed(list(enumerate(events))):
            if event.get("type") != "read_file":
                continue
            data_raw = event.get("data")
            data = data_raw if isinstance(data_raw, dict) else {}
            event_path = _coerce_str(data.get("path"))
            normalized_event_path = (event_path or "").replace("\\", "/").casefold()
            normalized_rel = rel_path.replace("\\", "/").casefold()
            if not (
                normalized_event_path == normalized_rel
                or normalized_event_path.endswith("/" + normalized_rel)
            ):
                continue
            if (
                not path.is_file()
                or sha256(path.read_bytes()).hexdigest() != expected_sha
                or path.stat().st_size != requirement.get("size_bytes")
                or data.get("content_observed") is not True
                or data.get("whole_file_observed") is not True
                or data.get("source_exit_code") != 0
                or data.get("file_sha256") != expected_sha
                or data.get("file_size_bytes") != requirement.get("size_bytes")
            ):
                continue
            receipts.append(
                {
                    "artifact_sha256": requirement["artifact_sha256"],
                    "file": rel_path,
                    "file_sha256": expected_sha,
                    "file_size_bytes": requirement.get("size_bytes"),
                    "read_event_index": event_index,
                    "read_event_sha256": _canonical_json_sha256(event),
                    "observed_content_sha256": data.get("observed_content_sha256"),
                    "observed_bytes": data.get("observed_bytes"),
                }
            )
            observed.add(rel_path)
            break
    for requirement in origin_attachment_requirements(manifest):
        rel_path = str(requirement["file"])
        if rel_path not in observed:
            errors.append(f"origin_attachment_chunk_not_read_in_full:{rel_path}")
    return receipts, list(dict.fromkeys(errors))


def _fail_evidence_verification(
    verification: dict[str, Any],
    *,
    errors: Sequence[str],
) -> None:
    """Atomically invalidate a receipt and remove its stale success projection."""

    existing_raw = verification.get("errors")
    existing = existing_raw if isinstance(existing_raw, list) else []
    verification["errors"] = list(
        dict.fromkeys(
            [
                *[value for value in existing if isinstance(value, str) and value],
                *[value for value in errors if isinstance(value, str) and value],
            ]
        )
    )
    verification["status"] = "failed"
    for field in _VERIFIED_MECHANISM_PROJECTION_FIELDS:
        verification[field] = None
    verification["outcome_oracles"] = []


def _coerce_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _report_status_blocking_reason(
    report_status: str | None,
    research_status: str | None,
) -> str | None:
    """Treat the extension as outcome authority while rejecting contradictory success."""
    if report_status in {"partial", "failure"} and research_status == "evidence_sufficient":
        return f"runner_report_status:{report_status}"
    return None


def _stable_seed(problem_id: str) -> int:
    """Derive a stable integer seed for a problem ID."""
    digest = sha256(problem_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _load_json_object(path: Path) -> dict[str, Any]:
    """Load JSON from *path* and require it is an object."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected JSON object in {path}, got {type(raw).__name__}")
    return raw


def _model_report_schema_errors(
    *,
    run_dir: Path,
    report: dict[str, Any],
) -> list[str]:
    """Recompute only model-authored JSON-schema errors from runner artifacts."""
    schema_path = run_dir / "report.schema.json"
    if not schema_path.is_file():
        return []
    try:
        schema = _load_json_object(schema_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return []
    return validate_report(report, schema)


def _canonical_json_sha256(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _model_owned_dossier_projection(dossier: Mapping[str, Any]) -> dict[str, Any]:
    """Project one dossier to the exact fields authored by the researcher.

    ``artifact_refs`` is a mixed field: the researcher authors evidence references and the
    runner later appends its own ``runner:*`` receipts. Remove only that reserved namespace
    when matching a persisted dossier back to the immutable model-output attempt.
    """
    projection: dict[str, Any] = {}
    for key, value in dossier.items():
        if key in _RUNNER_OWNED_DOSSIER_FIELDS:
            continue
        projected_value = value
        if key == "artifact_refs" and isinstance(value, list):
            projected_value = [
                item
                for item in value
                if not (
                    isinstance(item, Mapping)
                    and isinstance(item.get("artifact_id"), str)
                    and str(item["artifact_id"]).startswith("runner:")
                )
            ]
        projection[key] = json.loads(json.dumps(projected_value, ensure_ascii=False))
    return projection


def _continuation_source_attempt(
    *,
    dossier: Mapping[str, Any],
    attempts: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Select the retained frontier's author attempt, not merely the latest attempt.

    A correction cycle can end with a regression while retaining an earlier objective-best
    dossier.  The immutable attempt ledger remains chronological, so the last entry is not
    necessarily the dossier the caller is asking the author to continue.  Match the retained
    model-owned projection back to the most recent attempt that produced it.
    """
    retained_sha256 = _canonical_json_sha256(_model_owned_dossier_projection(dossier))
    for attempt in reversed(attempts):
        attempted_raw = attempt.get("attempted_dossier")
        if not isinstance(attempted_raw, Mapping):
            continue
        if (
            _canonical_json_sha256(_model_owned_dossier_projection(attempted_raw))
            == retained_sha256
        ):
            return attempt
    return attempts[-1]


def _write_evidence_assignment_sidecar(
    run_dir: Path,
    *,
    evidence_assignment: dict[str, Any],
) -> Path:
    """Persist runner-owned parent lineage before downstream report processing."""

    target_ref_path = run_dir / "target_ref.json"
    target_ref = _load_json_object(target_ref_path) if target_ref_path.is_file() else {}
    payload: dict[str, Any] = {
        "schema_version": 1,
        "producer": "backlog_miner.research_runner",
        "target_ref_sha256": _canonical_json_sha256(target_ref),
        "evidence_assignment": evidence_assignment,
    }
    payload["sidecar_sha256"] = _canonical_json_sha256(payload)
    path = run_dir / "evidence_assignment.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _load_diff_numstat(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Expected JSON list in {path}, got {type(raw).__name__}")
    return [item for item in raw if isinstance(item, dict)]


def _string_list(value: Any) -> list[str]:
    """Return non-empty string members from a JSON-like list."""
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _runner_artifact_refs(run_dir: Path) -> list[dict[str, str]]:
    """Return canonical references to the evidence retained by ``runner_core``."""
    refs: list[dict[str, str]] = []
    for kind, filename, description in (
        ("report_json", "report.json", "Validated stage-3 research report"),
        ("report_markdown", "report.md", "Human-readable research report"),
        ("patch", "patch.diff", "Research-only workspace diff"),
        (
            "evidence_assignment",
            "evidence_assignment.json",
            "Runner-owned canonical-case evidence assignment",
        ),
        ("diff_numstat", "diff_numstat.json", "Machine-readable changed-path summary"),
        ("normalized_events", "normalized_events.jsonl", "Normalized command and tool events"),
        ("target_ref", "target_ref.json", "Runner-owned acquired revision record"),
        ("workspace_ref", "workspace_ref.json", "Runner-owned workspace record"),
        (
            "codex_subscription_auth",
            "codex_execpolicy_overlay.json",
            "Verified host ChatGPT subscription and controlled-policy receipt",
        ),
        ("agent_stderr", "agent_stderr.txt", "Agent stderr captured by the runner"),
    ):
        path = run_dir / filename
        if path.exists():
            if kind == "agent_stderr":
                try:
                    if path.stat().st_size == 0:
                        continue
                except OSError:
                    continue
            refs.append(
                {
                    "artifact_id": f"runner:{kind}",
                    "kind": kind,
                    "path": str(path),
                    "description": description,
                }
            )
    return refs


def _canonical_repo_revision(run_dir: Path) -> str | None:
    """Read the acquired repository revision recorded by the runner."""
    target_ref_path = run_dir / "target_ref.json"
    if not target_ref_path.exists():
        return None
    target_ref = _load_json_object(target_ref_path)
    return _coerce_str(target_ref.get("commit_sha"))


def _resolve_repo_ref(repo_input: str, requested_ref: str) -> str:
    """Resolve a local source-of-truth ref before the target workspace is acquired."""
    try:
        repo_path = Path(repo_input).expanduser()
        is_local = repo_path.is_dir()
    except OSError:
        is_local = False
    if not is_local:
        return requested_ref
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--verify", f"{requested_ref}^{{commit}}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        detail = result.stderr.strip() or result.stdout.strip() or "ref not found"
        raise ValueError(
            f"run_repro_research_stage: cannot resolve source ref {requested_ref!r} "
            f"in {repo_input!r}: {detail}"
        )
    return result.stdout.strip()


def _classify_diff(
    modified_paths: Sequence[str],
    *,
    writes_purpose: Sequence[str] | None = None,
) -> tuple[str, list[str]]:
    """Classify a stage-3 diff as allowed research edits vs suspicious implementation.

    The classification is intentionally conservative: only the dedicated
    ``.usertest_research/`` overlay is research-only. Test, script, tool, and
    configuration edits can change repository behavior and therefore remain
    suspicious until implementation is explicitly authorized.
    """
    normalized = [p.replace("\\", "/") for p in modified_paths if isinstance(p, str) and p.strip()]
    if not normalized:
        return "no_changes", []

    suspicious: list[str] = []
    for path in normalized:
        if path.startswith(".usertest_research/"):
            continue
        suspicious.append(path)

    if not suspicious:
        return "allowed_research_edits", []

    reasons = [f"suspicious_path: {p}" for p in suspicious]
    if writes_purpose:
        reasons.append("writes_purpose_claimed: " + ", ".join(sorted(set(writes_purpose))))
    return "suspicious_implementation", reasons


def _blocked_research_placeholder(
    *,
    case_id: str,
    problem_id: str,
    evidence_assignment: dict[str, Any],
    evidence_atom_ids: Sequence[str],
    requested_repo_ref: str | None,
    resolved_repo_ref: str | None,
    reason: str,
    unknown: str,
    evidence_needed: str,
) -> dict[str, Any]:
    """Build an explicit non-advancing proof when research cannot execute."""
    dossier: dict[str, Any] = {
        "research_schema_version": RESEARCH_PROOF_SCHEMA_VERSION,
        "case_id": case_id,
        "problem_id": problem_id,
        "repo_revision": "unavailable:not_executed",
        "research_method": "reproduction",
        "reproduction_status": "blocked",
        "research_status": "blocked",
        "writes_used": False,
        "writes_purpose": ["none"],
        "implementation_performed": False,
        "diff_classification": "no_changes",
        "artifact_refs": [],
        "experiments": [],
        "inspected_files": [],
        "inspected_symbols": [],
        "root_cause_hypotheses": [],
        "root_cause_confidence": 0.0,
        "broader_class_assessment": "unknown",
        "material_unknowns": [
            {
                "unknown": unknown,
                "affects": ["root_cause", "interface", "change_surface"],
                "evidence_needed": evidence_needed,
            }
        ],
        "blocking_reasons": [reason],
        "evidence_boundaries": [
            "No mechanism claim was accepted because the assigned evidence was incomplete"
        ],
        "evidence_assignment": evidence_assignment,
        "repo_workspace": None,
        "evidence_verification": {
            "verification_method": "runner_artifact_binding_v1",
            "status": "failed",
            "case_id": case_id,
            "problem_id": problem_id,
            "repo_revision": "unavailable:not_executed",
            "requested_repo_ref": requested_repo_ref,
            "resolved_repo_ref": resolved_repo_ref,
            "workspace_dir": None,
            "workspace_head": None,
            "workspace_overlay": {},
            "replay_isolation": {
                "executor": "blocked",
                "os_sandbox": False,
                "network": "unavailable",
                "filesystem_isolation": "unavailable",
                "trust_decision": "denied",
                "trust_reason": reason,
                "source_workspace": None,
                "sanitized_environment_keys": [],
            },
            "planning_workspace_dir": None,
            "planning_workspace_head": None,
            "planning_workspace_clean": None,
            "run_dir": None,
            "origin_atom_ids": list(evidence_atom_ids),
            "normalized_events_sha256": None,
            "run_report_sha256": None,
            "assignment_sha256": evidence_assignment.get("assignment_sha256"),
            "claims_sha256": None,
            "artifacts": [],
            "experiments": [],
            "inspected_files": [],
            "inspected_symbols": [],
            "hypothesis_refs": [],
            "causal_links": [],
            "mechanism_evidence": [],
            "verified_mechanism": None,
            "verified_mechanism_sha256": None,
            "verified_mechanism_provenance": None,
            "verified_mechanism_provenance_sha256": None,
            "test_selections": [],
            "control_verifications": [],
            "falsification_interventions": [],
            "deterministic_mechanism_closures": [],
            "failure_paths": [],
            "atom_bindings": [],
            "errors": [reason],
        },
        "run_dir": None,
    }
    dossier["evidence_verification"]["claims_sha256"] = research_claims_sha256(dossier)
    dossier["evidence_verification"]["receipt_sha256"] = evidence_verification_sha256(
        dossier["evidence_verification"]
    )
    return dossier


def _blocked_research_after_run_failure(
    *,
    case_id: str,
    problem_id: str,
    evidence_assignment: dict[str, Any],
    evidence_atom_ids: Sequence[str],
    requested_repo_ref: str | None,
    resolved_repo_ref: str | None,
    run_dir: Path | None,
    reason: str,
    unknown: str,
    evidence_needed: str,
) -> dict[str, Any]:
    """Retain a case-local failed run without aborting unrelated research cases."""

    dossier = _blocked_research_placeholder(
        case_id=case_id,
        problem_id=problem_id,
        evidence_assignment=evidence_assignment,
        evidence_atom_ids=evidence_atom_ids,
        requested_repo_ref=requested_repo_ref,
        resolved_repo_ref=resolved_repo_ref,
        reason=reason,
        unknown=unknown,
        evidence_needed=evidence_needed,
    )
    if run_dir is not None and run_dir.is_dir():
        dossier["run_dir"] = str(run_dir.resolve())
        dossier["artifact_refs"] = _runner_artifact_refs(run_dir)
        verification = dossier["evidence_verification"]
        verification["run_dir"] = str(run_dir.resolve())
        verification["artifacts"] = list(dossier["artifact_refs"])
        repo_revision = _canonical_repo_revision(run_dir)
        if repo_revision is not None:
            dossier["repo_revision"] = repo_revision
            verification["repo_revision"] = repo_revision
        verification["claims_sha256"] = research_claims_sha256(dossier)
        verification["receipt_sha256"] = evidence_verification_sha256(verification)
    return dossier


def _runner_external_wait(run_dir: Path) -> dict[str, Any] | None:
    """Read one runner-attested, resumable ChatGPT subscription wait."""
    error_path = run_dir / "error.json"
    if not error_path.is_file():
        return None
    try:
        error = _load_json_object(error_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    wait_raw = error.get("external_wait")
    wait = wait_raw if isinstance(wait_raw, dict) else {}
    if (
        error.get("type") != "AgentExternalWait"
        or error.get("code") != "codex_chatgpt_subscription_usage_limit"
        or error.get("provider") != "codex"
        or error.get("route") != "chatgpt_subscription"
        or error.get("api_fallback_allowed") is not False
        or wait.get("state") != "parked"
        or wait.get("retry_mode") != "resume_same_session"
        or wait.get("route") != "chatgpt_subscription"
        or wait.get("api_fallback_allowed") is not False
    ):
        return None
    return {
        "code": "codex_chatgpt_subscription_usage_limit",
        "provider": "codex",
        "phase": _coerce_str(error.get("phase")) or "agent_execution",
        "route": "chatgpt_subscription",
        "api_fallback_allowed": False,
        "state": "parked",
        "retry_mode": "resume_same_session",
        "retry_disposition": wait.get("retry_disposition"),
        "resume_after": json.loads(json.dumps(wait.get("resume_after"), ensure_ascii=False)),
        "run_dir": str(run_dir.resolve()),
        "error_artifact": str(error_path.resolve()),
        "error_artifact_sha256": sha256(error_path.read_bytes()).hexdigest(),
        "error_artifact_size_bytes": error_path.stat().st_size,
    }


def _stage_external_wait_checkpoint(
    *,
    external_wait: dict[str, Any],
    case_id: str,
    problem_id: str,
    expected_session_id: str | None,
    observed_session_id: str | None,
) -> dict[str, Any]:
    """Bind one provider-global wait to the Stage-3 frontier that encountered it."""
    checkpoint: dict[str, Any] = {
        "schema_version": 1,
        "status": "parked_external_wait",
        "scope": "repro_research_stage",
        "reason": "codex_chatgpt_subscription_usage_limit",
        "trigger_case_id": case_id,
        "trigger_problem_id": problem_id,
        "expected_session_id": expected_session_id,
        "observed_session_id": observed_session_id,
        "authored_work_disposition": "retained",
        "resume_status": "checkpoint_persisted_same_author_resume_supported",
        "next_action": "resume_same_author_from_checkpoint_after_provider_reset",
        "route": "chatgpt_subscription",
        "api_fallback_allowed": False,
        "external_wait": json.loads(json.dumps(external_wait, ensure_ascii=False)),
    }
    checkpoint["checkpoint_sha256"] = _canonical_json_sha256(checkpoint)
    return checkpoint


def _parked_before_dispatch_dossier(
    *,
    case_id: str,
    problem_id: str,
    evidence_assignment: dict[str, Any],
    evidence_atom_ids: Sequence[str],
    requested_repo_ref: str | None,
    resolved_repo_ref: str | None,
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    """Represent selected work intentionally left undispatched by a provider-global wait."""
    checkpoint_sha256 = str(checkpoint["checkpoint_sha256"])
    return _blocked_research_placeholder(
        case_id=case_id,
        problem_id=problem_id,
        evidence_assignment=evidence_assignment,
        evidence_atom_ids=evidence_atom_ids,
        requested_repo_ref=requested_repo_ref,
        resolved_repo_ref=resolved_repo_ref,
        reason=(f"research_external_wait_stage_parked_before_dispatch:{checkpoint_sha256}"),
        unknown="Research did not start because the signed-in Codex subscription is parked",
        evidence_needed=(
            "Resume this selected case after the provider reset recorded by the Stage-3 "
            "external-wait checkpoint"
        ),
    )


def _research_attempt_record(
    *,
    attempt_number: int,
    outcome: str,
    run_dir: Path,
    report_path: Path,
    validation_errors: Sequence[str],
    attempted_dossier: dict[str, Any],
    attempt_kind: str = "full_research",
    source_attempt_sha256: str | None = None,
    authorized_paths: Sequence[str] = (),
    baseline_dossier_sha256: str | None = None,
    baseline_projection_sha256: str | None = None,
    repair_contract_sha256: str | None = None,
    validation_errors_before: Sequence[str] = (),
    agent_session_id: str | None = None,
    observed_agent_session_id: str | None = None,
    resumed_from_session_id: str | None = None,
    attempt_wall_seconds: float | None = None,
    repair_progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Retain one immutable model-output attempt for later audit."""
    attempted_dossier_copy = json.loads(json.dumps(attempted_dossier, ensure_ascii=False))
    normalized_errors = _dedupe_validation_errors(validation_errors)
    normalized_before = _dedupe_validation_errors(validation_errors_before)

    def artifact_receipt(kind: str, path: Path) -> dict[str, Any]:
        exists = path.is_file()
        return {
            "kind": kind,
            "path": str(path.resolve()),
            "exists": exists,
            "sha256": sha256(path.read_bytes()).hexdigest() if exists else None,
            "size_bytes": path.stat().st_size if exists else None,
        }

    attempt = {
        "attempt_number": attempt_number,
        "attempt_kind": attempt_kind,
        "outcome": outcome,
        "run_dir": str(run_dir.resolve()),
        "report_path": str(report_path.resolve()),
        "validation_errors": normalized_errors,
        "validation_errors_before": normalized_before,
        "validation_errors_after": normalized_errors,
        # A JSON round-trip prevents later runner augmentation from mutating the
        # exact extension object the model originally emitted.
        "attempted_dossier": attempted_dossier_copy,
        "attempted_dossier_sha256": _canonical_json_sha256(attempted_dossier_copy),
        "source_attempt_sha256": source_attempt_sha256,
        "authorized_paths": list(authorized_paths),
        "baseline_dossier_sha256": baseline_dossier_sha256,
        "baseline_projection_sha256": baseline_projection_sha256,
        "repair_contract_sha256": repair_contract_sha256,
        "agent_session_id": agent_session_id,
        "observed_agent_session_id": observed_agent_session_id,
        "resumed_from_session_id": resumed_from_session_id,
        "attempt_wall_seconds": attempt_wall_seconds,
        "repair_progress": (
            json.loads(json.dumps(repair_progress, ensure_ascii=False))
            if isinstance(repair_progress, dict)
            else None
        ),
        "attempt_artifacts": [
            artifact_receipt("report", report_path),
            artifact_receipt("workspace_ref", run_dir / "workspace_ref.json"),
            artifact_receipt("target_ref", run_dir / "target_ref.json"),
            artifact_receipt("normalized_events", run_dir / "normalized_events.jsonl"),
            artifact_receipt(
                "codex_subscription_auth",
                run_dir / "codex_execpolicy_overlay.json",
            ),
        ],
    }
    attempt["attempt_sha256"] = research_attempt_sha256(attempt)
    return attempt


def _run_wall_seconds(run_dir: Path) -> float | None:
    try:
        meta = _load_json_object(run_dir / "run_meta.json")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    value = meta.get("run_wall_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) < 0.0:
        return None
    return float(value)


def _research_invocation_failure_record(
    *,
    attempt_number: int,
    validation_errors: Sequence[str],
    attempt_kind: str = "full_research",
    source_attempt_sha256: str | None = None,
    authorized_paths: Sequence[str] = (),
    baseline_dossier_sha256: str | None = None,
    baseline_projection_sha256: str | None = None,
    repair_contract_sha256: str | None = None,
    validation_errors_before: Sequence[str] = (),
    agent_session_id: str | None = None,
    observed_agent_session_id: str | None = None,
    resumed_from_session_id: str | None = None,
    attempt_wall_seconds: float | None = None,
    repair_progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a retry invocation that never produced a run without inventing paths."""
    attempted_dossier: dict[str, Any] = {}
    normalized_errors = _dedupe_validation_errors(validation_errors)
    normalized_before = _dedupe_validation_errors(validation_errors_before)
    attempt = {
        "attempt_number": attempt_number,
        "attempt_kind": attempt_kind,
        "outcome": "invocation_failed",
        "run_dir": None,
        "report_path": None,
        "validation_errors": normalized_errors,
        "validation_errors_before": normalized_before,
        "validation_errors_after": normalized_errors,
        "attempted_dossier": attempted_dossier,
        "attempted_dossier_sha256": _canonical_json_sha256(attempted_dossier),
        "source_attempt_sha256": source_attempt_sha256,
        "authorized_paths": list(authorized_paths),
        "baseline_dossier_sha256": baseline_dossier_sha256,
        "baseline_projection_sha256": baseline_projection_sha256,
        "repair_contract_sha256": repair_contract_sha256,
        "agent_session_id": agent_session_id,
        "observed_agent_session_id": observed_agent_session_id,
        "resumed_from_session_id": resumed_from_session_id,
        "attempt_wall_seconds": attempt_wall_seconds,
        "repair_progress": (
            json.loads(json.dumps(repair_progress, ensure_ascii=False))
            if isinstance(repair_progress, dict)
            else None
        ),
        "attempt_artifacts": [],
    }
    attempt["attempt_sha256"] = research_attempt_sha256(attempt)
    return attempt


def _set_research_attempts(
    dossier: dict[str, Any],
    attempts: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Attach content-bound attempt history and refresh runner receipt hashes."""
    dossier["research_attempts"] = [dict(attempt) for attempt in attempts]
    verification_raw = dossier.get("evidence_verification")
    if isinstance(verification_raw, dict):
        verification_raw["claims_sha256"] = research_claims_sha256(dossier)
        verification_raw["receipt_sha256"] = evidence_verification_sha256(verification_raw)
    return dossier


def _research_attempt_request_summary(attempt: dict[str, Any]) -> dict[str, Any]:
    """Project attempt provenance into the compact request ledger."""
    return {
        key: attempt.get(key)
        for key in (
            "attempt_number",
            "attempt_kind",
            "outcome",
            "run_dir",
            "report_path",
            "validation_errors",
            "validation_errors_before",
            "validation_errors_after",
            "agent_session_id",
            "observed_agent_session_id",
            "resumed_from_session_id",
            "attempt_wall_seconds",
            "repair_progress",
        )
    }


def _record_terminal_continuation_unavailable(
    attempt: dict[str, Any],
    *,
    repair_result: dict[str, Any],
    assessment: dict[str, Any],
) -> None:
    """Content-bind why a full author cycle could not continue in its original session."""
    terminal = {
        "status": repair_result.get("status"),
        "continuation_failure": repair_result.get("continuation_failure"),
        "expected_session_id": repair_result.get("expected_session_id"),
        "observed_session_id": repair_result.get("observed_session_id"),
        "decision": assessment.get("decision"),
        "reason": assessment.get("reason"),
    }
    progress_raw = attempt.get("repair_progress")
    progress = dict(progress_raw) if isinstance(progress_raw, dict) else {}
    progress.pop("provenance_sha256", None)
    progress["terminal_continuation"] = terminal
    if attempt.get("attempt_kind") == "fresh_research_retry":
        progress["provenance_sha256"] = _canonical_json_sha256(progress)
    attempt["repair_progress"] = progress
    attempt["attempt_sha256"] = research_attempt_sha256(attempt)


def _research_retry_prior_attempt_projection(attempt: dict[str, Any]) -> dict[str, Any]:
    """Return a content-addressed model-output projection for one bounded retry.

    The projection deliberately excludes workspace paths and runner-owned receipts.  It gives
    the fresh research run enough context to preserve strong model-authored investigation while
    still requiring the runner to reacquire the repository and replay every claimed experiment.
    """

    dossier_raw = attempt.get("attempted_dossier")
    dossier = dossier_raw if isinstance(dossier_raw, dict) else {}
    dossier_copy = json.loads(json.dumps(dossier, ensure_ascii=False))
    projection: dict[str, Any] = {
        "attempt_number": attempt.get("attempt_number"),
        "outcome": attempt.get("outcome"),
        "validation_errors": _string_list(attempt.get("validation_errors")),
        "attempted_dossier": dossier_copy,
        "attempted_dossier_sha256": _canonical_json_sha256(dossier_copy),
    }
    projection["projection_sha256"] = _canonical_json_sha256(projection)
    return projection


def _research_correction_frontiers(
    *,
    repair_status: str,
    latest_safe_attempt: dict[str, Any],
    best_count_attempt: dict[str, Any],
    attempt_history: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Content-address both the forward frontier and objective best before a fresh restart."""
    latest_projection = _research_retry_prior_attempt_projection(latest_safe_attempt)
    best_projection = _research_retry_prior_attempt_projection(best_count_attempt)
    frontiers: dict[str, Any] = {
        "repair_status": repair_status,
        "latest_safe_projection": latest_projection,
        "latest_safe_projection_sha256": latest_projection["projection_sha256"],
        "best_count_projection": best_projection,
        "best_count_projection_sha256": best_projection["projection_sha256"],
        "attempt_history": [
            {
                "attempt_number": attempt.get("attempt_number"),
                "attempt_kind": attempt.get("attempt_kind"),
                "outcome": attempt.get("outcome"),
                "attempt_sha256": attempt.get("attempt_sha256"),
                "validation_errors": _string_list(attempt.get("validation_errors")),
            }
            for attempt in attempt_history
        ],
    }
    frontiers["frontiers_sha256"] = _canonical_json_sha256(frontiers)
    return frontiers


def _safe_model_output_attempt(attempt: dict[str, Any]) -> bool:
    return attempt.get("outcome") in {
        "output_contract_valid",
        "output_contract_invalid",
        "repair_contract_valid",
        "repair_contract_invalid",
    }


def _attempt_correction_state(attempt: dict[str, Any]) -> str:
    return _canonical_json_sha256(
        {
            "attempted_dossier_sha256": attempt.get("attempted_dossier_sha256"),
            "error_identities": sorted(
                _validation_error_identity(error)
                for error in _string_list(attempt.get("validation_errors"))
            ),
        }
    )


def _clean_investigation_estimate_seconds(
    attempts: Sequence[dict[str, Any]],
) -> float | None:
    """Estimate clean-investigation cost from full authoring turns in the ledger."""
    full_attempt_seconds = [
        float(attempt.get("attempt_wall_seconds"))
        for attempt in attempts
        if attempt.get("attempt_kind") in {"full_research", "fresh_research_retry"}
        and isinstance(attempt.get("attempt_wall_seconds"), (int, float))
        and not isinstance(attempt.get("attempt_wall_seconds"), bool)
        and float(attempt.get("attempt_wall_seconds")) > 0.0
    ]
    return sum(full_attempt_seconds) / len(full_attempt_seconds) if full_attempt_seconds else None


def _fresh_restart_progress_assessment(
    *,
    full_attempt_kind: str,
    prior_attempts: Sequence[dict[str, Any]],
    current_cycle_attempts: Sequence[dict[str, Any]],
    current_best_attempt: dict[str, Any],
    repair_status: str,
) -> dict[str, Any]:
    """Decide whether a completed author cycle warrants another fresh investigation."""
    prior_safe = [attempt for attempt in prior_attempts if _safe_model_output_attempt(attempt)]
    prior_best_count = (
        min(len(_string_list(attempt.get("validation_errors"))) for attempt in prior_safe)
        if prior_safe
        else None
    )
    current_best_count = len(_string_list(current_best_attempt.get("validation_errors")))
    current_state = _attempt_correction_state(current_best_attempt)
    repeated_equivalent = any(
        _attempt_correction_state(attempt) == current_state for attempt in prior_safe
    )
    objective_progress = prior_best_count is not None and current_best_count < prior_best_count
    cycle_wall_seconds = sum(
        float(attempt.get("attempt_wall_seconds") or 0.0)
        for attempt in current_cycle_attempts
        if isinstance(attempt.get("attempt_wall_seconds"), (int, float))
        and not isinstance(attempt.get("attempt_wall_seconds"), bool)
    )
    clean_investigation_estimate = _clean_investigation_estimate_seconds(
        [*prior_attempts, *current_cycle_attempts]
    )
    assessment: dict[str, Any] = {
        "decision": "repairable_paused",
        "reason": "fresh_cycle_no_objective_progress",
        "full_attempt_kind": full_attempt_kind,
        "restart_trigger": repair_status,
        "prior_best_error_count": prior_best_count,
        "current_best_error_count": current_best_count,
        "objective_progress": objective_progress,
        "repeated_equivalent_state": repeated_equivalent,
        "current_best_state_sha256": current_state,
        "cycle_wall_seconds": cycle_wall_seconds,
        "clean_investigation_estimate_seconds": clean_investigation_estimate,
    }
    if full_attempt_kind == "full_research":
        # This is not a free retry: the original author already exhausted useful correction and
        # explicitly demonstrated that a new investigation is now the cheaper path.
        assessment.update(
            decision="restart",
            reason="initial_author_cycle_demonstrated_restart_need",
        )
    elif repeated_equivalent:
        assessment.update(
            decision="repairable_paused",
            reason="fresh_cycle_repeated_equivalent_state",
        )
    elif objective_progress:
        assessment.update(
            decision="restart",
            reason="fresh_cycle_net_error_reduction",
        )
    return assessment


def _restart_cycle_metrics(attempts: Sequence[dict[str, Any]]) -> dict[str, float | int]:
    """Summarize behavior-gated fresh cycles from retained attempt history."""
    full_indexes = [
        index
        for index, attempt in enumerate(attempts)
        if attempt.get("attempt_kind") in {"full_research", "fresh_research_retry"}
    ]
    metrics: dict[str, float | int] = {
        "fresh_restart_cycle_count": 0,
        "fresh_restart_objective_progress_cycle_count": 0,
        "fresh_restart_nonprogress_cycle_count": 0,
        "fresh_restart_equivalent_cycle_count": 0,
        "fresh_restart_cycle_wall_seconds": 0.0,
    }
    for position, start in enumerate(full_indexes):
        if attempts[start].get("attempt_kind") != "fresh_research_retry":
            continue
        end = full_indexes[position + 1] if position + 1 < len(full_indexes) else len(attempts)
        prior_safe = [
            attempt for attempt in attempts[:start] if _safe_model_output_attempt(attempt)
        ]
        cycle_safe = [
            attempt for attempt in attempts[start:end] if _safe_model_output_attempt(attempt)
        ]
        if not cycle_safe:
            continue
        current_best = min(
            cycle_safe,
            key=lambda attempt: len(_string_list(attempt.get("validation_errors"))),
        )
        prior_best_count = min(
            (len(_string_list(attempt.get("validation_errors"))) for attempt in prior_safe),
            default=None,
        )
        current_best_count = len(_string_list(current_best.get("validation_errors")))
        equivalent = any(
            _attempt_correction_state(attempt) == _attempt_correction_state(current_best)
            for attempt in prior_safe
        )
        objective_progress = prior_best_count is not None and current_best_count < prior_best_count
        metrics["fresh_restart_cycle_count"] += 1
        metrics["fresh_restart_cycle_wall_seconds"] += sum(
            float(attempt.get("attempt_wall_seconds") or 0.0)
            for attempt in attempts[start:end]
            if isinstance(attempt.get("attempt_wall_seconds"), (int, float))
            and not isinstance(attempt.get("attempt_wall_seconds"), bool)
        )
        if objective_progress:
            metrics["fresh_restart_objective_progress_cycle_count"] += 1
        else:
            metrics["fresh_restart_nonprogress_cycle_count"] += 1
        if equivalent:
            metrics["fresh_restart_equivalent_cycle_count"] += 1
    return metrics


def _research_retry_remediation_hints(
    validation_errors: Sequence[str],
) -> list[dict[str, Any]]:
    """Translate deterministic contract errors into stable, field-specific retry hints."""

    hints: list[dict[str, Any]] = []
    for validation_error in validation_errors:
        error = str(validation_error)
        code = error.partition(":")[0].strip()
        target_fields: list[str]
        required_change: str
        if code == "research_dossier_positive_outcome_contract_invalid":
            target_fields = [
                "experiments[].positive_outcome_contract",
                "experiments[].origin_evidence_bindings",
            ]
            required_change = (
                "Use exactly one documented positive-outcome shape. For "
                "origin_atom_exact_value, atom_id and field_path must match an "
                "expected_behavior binding, and postcondition value/equals must equal the "
                "immutable source-atom scalar without coercion. artifact_json_value requires "
                "path, json_pointer, and equals; config_state_equals requires "
                "mechanism_symbol, exists, and equals."
            )
        elif code == "research_dossier_unresolved_hypothesis_evidence_ref":
            target_fields = [
                "root_cause_hypotheses[].supporting_evidence",
                "root_cause_hypotheses[].counterevidence",
                "root_cause_hypotheses[].disposition_evidence",
            ]
            required_change = (
                "Keep only exact experiment_id or artifact_id values declared in this dossier. "
                "Atom IDs, attempt IDs, prose labels, and undeclared diagnostics are not "
                "hypothesis evidence references."
            )
        elif code == "research_dossier_hypothesis_control_unbound":
            target_fields = [
                "root_cause_hypotheses[].counterevidence",
                "experiments[].control_relationship",
            ]
            required_change = (
                "Reference a control only when it is a genuine paired intervention with its "
                "supporting experiment: same atoms and mechanism-source evidence, a matching "
                "intervened mechanism-symbol subset, distinct shell-free command, one changed "
                "causal condition, and complementary observable results. Otherwise remove the "
                "control reference and report the diagnostic outside experiments."
            )
        elif code == "research_dossier_falsification_result_mismatch":
            target_fields = [
                "root_cause_hypotheses[].falsification_attempts[].disproof_condition",
                "experiments[].observable_assertion",
            ]
            required_change = (
                "Match the declared falsification outcome, not its prose gloss. For disproved, "
                "the challenge assertion exactly equals the disproof condition. For survived, "
                "contains is complemented by not_contains (and vice versa) with the same source "
                "and expected scalar; equals uses the same source with a different observed "
                "expected scalar. Do not make a survived assertion match its disproof condition."
            )
        elif code == "research_dossier_unknown_fields":
            target_fields = ["extensions.backlog_repro_research"]
            required_change = (
                "Remove only the unsupported top-level fields named by the validation error. "
                "Do not relocate them into evidence-bearing structures or change the retained "
                "experiments, hypotheses, status, unknowns, or blockers."
            )
        elif code == "research_dossier_missing_required_field":
            missing_field = error.rpartition(":")[2].strip() or "<field named by error>"
            target_fields = [f"extensions.backlog_repro_research.{missing_field}"]
            required_change = (
                "Emit the named required model-owned field with the exact documented JSON type; "
                "do not invent runner-owned fields."
            )
        elif code in {
            "research_dossier_problem_id_mismatch",
            "research_dossier_case_id_mismatch",
        }:
            identity_field = "problem_id" if "problem_id" in code else "case_id"
            target_fields = [f"extensions.backlog_repro_research.{identity_field}"]
            required_change = (
                f"Copy assigned {identity_field} exactly, as scalar string equality; do not "
                "derive, normalize, or rename it."
            )
        elif code == "research_extension_missing":
            target_fields = ["extensions.backlog_repro_research"]
            required_change = "Emit the complete required research extension object."
        elif code == "research_dossier_invalid_experiment_command":
            target_fields = ["experiments[].command"]
            required_change = (
                "Use one non-empty shell-free command string, not an argv array or object. "
                "Preserve the retained executable and arguments exactly, quoting tokens with "
                "spaces inside the string; do not run a new command or invent evidence."
            )
        elif code == "research_dossier_invalid_experiment_outcome":
            target_fields = ["experiments[].outcome"]
            required_change = (
                "Use exactly one scalar outcome: supports, refutes, or inconclusive. Put any "
                "explanation in result; reproduced/not_reproduced and structured objects are "
                "not experiment outcomes."
            )
        elif code in {
            "research_dossier_invalid_experiment_observable_assertion",
            "research_dossier_invalid_assertion_source",
            "research_dossier_invalid_assertion_operator",
            "research_dossier_invalid_exit_code_assertion",
            "research_dossier_invalid_text_assertion_expected",
        }:
            target_fields = ["experiments[].observable_assertion"]
            required_change = (
                "Use an object with source equal to exit_code, stdout, stderr, or combined; "
                "operator equal to equals, contains, or not_contains; and expected equal to "
                "the observed scalar. For exit_code, use operator=equals and an integer "
                "expected value. Artifact IDs and JSON pointers belong in artifact_refs/result, "
                "not in assertion.source."
            )
        elif code in {
            "research_dossier_invalid_hypothesis_disposition",
            "research_dossier_primary_hypothesis_disposition_invalid",
        }:
            target_fields = ["root_cause_hypotheses[].disposition"]
            required_change = (
                "Use exactly primary, refuted, plausible, or unresolved. The first hypothesis "
                "must be primary even when research_status is insufficient_evidence; primary "
                "only identifies the leading mechanism and does not claim resolution or "
                "evidence sufficiency."
            )
        elif code == "inspected_file_not_observed":
            target_fields = ["inspected_files", "inspected_symbols"]
            required_change = (
                "If the claim is still needed, actually reread the named repository file in "
                "this research turn using one standalone attested command: Get-Content -Raw "
                "-Encoding UTF8 -LiteralPath <path> for a small file, or Get-Content -Encoding "
                "UTF8 -LiteralPath <path> | Select-Object -Skip <N> -First <M> for an exact "
                "bounded range. Search/Select-String is discovery only, and the attested read "
                "must not be chained with markers or other commands. Otherwise remove the "
                "unsupported inspected-file/symbol claim and preserve the resulting unknown."
            )
        elif code == "inspected_symbol_unresolved":
            target_fields = ["inspected_symbols", "inspected_files"]
            required_change = (
                "After locating the symbol, perform a standalone attested whole-file or exact "
                "bounded Get-Content read containing its complete definition. Do not use "
                "Select-String output as proof. If the definition was not observed, remove the "
                "symbol claim and report the mechanism/change-surface boundary honestly."
            )
        elif code == "inspected_file_unresolved":
            target_fields = ["inspected_files", "artifact_refs"]
            required_change = (
                "Only tracked files from the pinned planning revision belong in "
                "inspected_files. A .usertest_research overlay or generated run artifact must "
                "remain an artifact_ref/experiment artifact, not masquerade as a repository "
                "implementation file."
            )
        elif code == "experiment_replay_workspace_mutated":
            target_fields = ["experiments[].replay_setup.disposable_state_paths"]
            required_change = (
                "If the replay intentionally creates case-local state, declare only those exact "
                "relative paths in replay_setup.disposable_state_paths; otherwise change the "
                "research harness to avoid the mutation. Never declare tracked product paths or "
                "unrelated workspace state as disposable."
            )
        elif code.startswith("research_report_"):
            target_fields = ["report.json"]
            required_change = (
                "Emit one complete troubleshoot_v1 JSON report with the required research "
                "extension; rerun the research rather than repairing JSON in isolation."
            )
        elif "falsification_attempt" in code:
            target_fields = ["root_cause_hypotheses[].falsification_attempts[]"]
            required_change = (
                "Use an attempt only for a genuine paired causal intervention. Copy the exact "
                "hypothesis ID and statement, bind distinct declared baseline/challenge "
                "experiments, and state a machine-checkable disproof condition. An empty list is "
                "correct when no honest counterfactual exists."
            )
        elif "falsification_" in code:
            target_fields = [
                "root_cause_hypotheses[].falsification_attempts[]",
                "experiments[]",
                "experiments[].control_relationship",
            ]
            required_change = (
                "Bind the declared baseline and challenge as a genuine paired intervention: "
                "resolved distinct experiment IDs and shell-free commands, the required "
                "supporting/refuting outcomes, identical addressed source atoms, shared "
                "mechanism-source evidence, and a challenge result matching the predeclared "
                "disproof condition. Remove the optional attempt when no such intervention was "
                "actually run."
            )
        elif "control_relationship" in code or "control" in code:
            target_fields = ["experiments[].control_relationship"]
            required_change = (
                "A control must name its declared supporting experiment and a single real causal "
                "condition changed by a distinct shell-free replayable command. Do not classify "
                "an ancillary diagnostic as a control."
            )
        elif "artifact_ref" in code:
            target_fields = ["artifact_refs", "experiments[].artifact_refs"]
            required_change = (
                "Declare each artifact once with a unique artifact_id and reference only those "
                "exact IDs from experiments and hypotheses."
            )
        elif "hypothesis" in code:
            target_fields = ["root_cause_hypotheses[]"]
            required_change = (
                "Make the first hypothesis the concrete failure-producing mechanism, not a fix "
                "bundle. Keep alternatives only when evidence makes them genuinely plausible, "
                "and use only declared experiment/artifact IDs as evidence references."
            )
        elif "experiment" in code or "static_trace" in code or "fidelity_mapping" in code:
            target_fields = ["experiments[]"]
            required_change = (
                "Keep only shell-free, runner-replayable causal experiments with the exact "
                "scenario shape, command, result, assertion, exit code, and declared evidence "
                "IDs. Move ancillary non-causal diagnostics to the outer report, but retain a "
                "genuine replayable causal experiment whose honest outcome is inconclusive and "
                "preserve its resulting unknown."
            )
        elif "material_unknown" in code:
            target_fields = ["material_unknowns[]"]
            required_change = (
                "Represent each material unknown with its hypothesis_id when applicable, causal "
                "effect fields, and the specific evidence needed; do not hide it to preserve an "
                "evidence_sufficient status."
            )
        else:
            target_fields = ["extensions.backlog_repro_research"]
            required_change = (
                "Correct the exact deterministic validation error while preserving honest status "
                "and all stronger research that can be reverified in the fresh workspace."
            )
        hints.append(
            {
                "validation_error": error,
                "error_code": code,
                "target_fields": target_fields,
                "required_change": required_change,
            }
        )
    return hints


_IMMUTABLE_RESEARCH_EVIDENCE_PATHS: tuple[str, ...] = (
    "artifact_refs",
    "experiments[].command",
    "experiments[].result",
    "experiments[].exit_code",
    "experiments[].artifact_refs",
    "inspected_files",
    "inspected_symbols",
)


def _validation_error_identity(error: str) -> str:
    """Normalize presentation whitespace without weakening one error's identity."""
    return " ".join(str(error).split())


def _dedupe_validation_errors(errors: Sequence[str]) -> list[str]:
    """Use one normalized entry per validator identity for feedback and progress."""

    return list(dict.fromkeys(_validation_error_identity(str(error)) for error in errors))


def _repair_error_requires_new_investigation(error: str) -> bool:
    """Identify integrity/evidence failures that a dossier-only correction cannot create."""
    code = str(error).partition(":")[0].strip().casefold()
    # Ordinary report/runner/verification diagnostics remain feedback: the same session may fix
    # structure, prune unsupported interpretation, downgrade honestly, or recover a transient.
    # Only independently observed integrity loss is immediately outside dossier repair.
    return code.startswith(
        (
            "suspicious_implementation_diff",
            "research_dossier_repair_session_continuity_failed",
        )
    )


def _verifier_feedback_requires_research_tools(errors: Sequence[str]) -> bool:
    """Give every post-verifier correction the original research capabilities.

    Output-contract structure is repaired before verification. Once the runner verifier
    reports a gap, unforeseen diagnostics must not be guessed into a no-tool category by
    their wording. The same author may choose not to use the available tools.
    """

    return bool(errors)


def _narrow_repair_path(
    path: str,
    *,
    error: str,
    dossier: dict[str, Any],
) -> str:
    normalized = path.removeprefix("extensions.backlog_repro_research.")
    if "[]" not in normalized:
        return normalized

    index_match = re.search(r"\bindex=(\d+)\b", error)
    if normalized.startswith("experiments[]") and index_match is not None:
        return normalized.replace("experiments[]", f"experiments[{index_match.group(1)}]", 1)

    collection_name = (
        "experiments" if normalized.startswith("experiments[]") else "root_cause_hypotheses"
    )
    id_field = "experiment_id" if collection_name == "experiments" else "hypothesis_id"
    values_raw = dossier.get(collection_name)
    values = values_raw if isinstance(values_raw, list) else []
    matching_indexes = [
        index
        for index, value in enumerate(values)
        if isinstance(value, dict)
        and isinstance(value.get(id_field), str)
        and re.search(
            rf"(?<![A-Za-z0-9_.:-]){re.escape(value[id_field])}(?![A-Za-z0-9_.:-])",
            error,
        )
    ]
    if len(matching_indexes) == 1:
        return normalized.replace(
            f"{collection_name}[]",
            f"{collection_name}[{matching_indexes[0]}]",
            1,
        )
    return normalized


def _targeted_repair_authorized_paths(
    validation_errors: Sequence[str],
    *,
    dossier: dict[str, Any],
) -> list[str] | None:
    """Return the only model-owned paths a cheap correction may change."""
    errors = [str(error) for error in validation_errors]
    if not errors:
        return None
    paths: list[str] = []
    for error, hint in zip(errors, _research_retry_remediation_hints(errors), strict=True):
        for path in hint["target_fields"]:
            narrowed = _narrow_repair_path(str(path), error=error, dossier=dossier)
            if narrowed and narrowed not in paths:
                paths.append(narrowed)
    # Unknown validators are deliberately repairable. Their verbatim error and generic extension
    # target let the same author session self-correct without waiting for a prompt-code release.
    return paths or ["*"]


def _json_changed_paths(before: Any, after: Any, *, path: str = "") -> list[str]:
    """Return stable model-owned paths whose values differ."""
    if type(before) is not type(after):  # noqa: E721 - bool/int must remain distinct
        return [path or "$"]
    if isinstance(before, dict):
        changed: list[str] = []
        for key in sorted(set(before) | set(after)):
            child_path = f"{path}.{key}" if path else str(key)
            if key not in before or key not in after:
                changed.append(child_path)
            else:
                changed.extend(_json_changed_paths(before[key], after[key], path=child_path))
        return changed
    if isinstance(before, list):
        if len(before) != len(after):
            return [path or "$"]
        changed = []
        for index, (before_item, after_item) in enumerate(zip(before, after, strict=True)):
            changed.extend(
                _json_changed_paths(
                    before_item,
                    after_item,
                    path=f"{path}[{index}]" if path else f"[{index}]",
                )
            )
        return changed
    return [] if before == after else [path or "$"]


def _path_is_repair_authorized(path: str, authorized_paths: Sequence[str]) -> bool:
    for pattern in authorized_paths:
        normalized = str(pattern)
        if normalized in {"*", "extensions.backlog_repro_research"}:
            return True
        # A trailing [] authorizes changing the optional list itself as well as its members.
        parent_pattern = normalized[:-2] if normalized.endswith("[]") else normalized
        regex = re.escape(parent_pattern).replace(r"\[\]", r"\[\d+\]")
        if re.fullmatch(rf"{regex}(?:\..+|\[\d+\].*)?", path):
            return True
    return False


def _path_matches_pattern(path: str, pattern: str) -> bool:
    normalized = str(pattern)
    if normalized.endswith("[]") and path == normalized[:-2]:
        return True
    regex = re.escape(normalized).replace(r"\[\]", r"\[\d+\]")
    return re.fullmatch(rf"{regex}(?:\..+|\[\d+\].*)?", path) is not None


def _fundamental_evidence_changes(
    changed_paths: Sequence[str],
    *,
    explicitly_authorized_paths: Sequence[str],
    before_dossier: dict[str, Any],
    after_dossier: dict[str, Any],
) -> list[str]:
    """Protect retained observations while allowing generic interpretation repair."""
    del explicitly_authorized_paths
    protected_changes = [
        path
        for path in changed_paths
        for protected in _IMMUTABLE_RESEARCH_EVIDENCE_PATHS
        if _path_matches_pattern(path, protected)
    ]
    before_experiments_raw = before_dossier.get("experiments")
    after_experiments_raw = after_dossier.get("experiments")
    before_experiments = before_experiments_raw if isinstance(before_experiments_raw, list) else []
    after_experiments = after_experiments_raw if isinstance(after_experiments_raw, list) else []
    before_ids = {
        str(experiment.get("experiment_id"))
        for experiment in before_experiments
        if isinstance(experiment, dict) and _coerce_str(experiment.get("experiment_id")) is not None
    }
    after_ids = {
        str(experiment.get("experiment_id"))
        for experiment in after_experiments
        if isinstance(experiment, dict) and _coerce_str(experiment.get("experiment_id")) is not None
    }
    protected_changes.extend(
        f"experiments[added:{experiment_id}]" for experiment_id in sorted(after_ids - before_ids)
    )
    before_by_id = {
        str(experiment["experiment_id"]): experiment
        for experiment in before_experiments
        if isinstance(experiment, dict) and _coerce_str(experiment.get("experiment_id")) is not None
    }
    after_by_id = {
        str(experiment["experiment_id"]): experiment
        for experiment in after_experiments
        if isinstance(experiment, dict) and _coerce_str(experiment.get("experiment_id")) is not None
    }
    for experiment_id in sorted(set(before_by_id) & set(after_by_id)):
        before_experiment = before_by_id[experiment_id]
        after_experiment = after_by_id[experiment_id]
        for field in ("command", "result", "exit_code", "artifact_refs"):
            if before_experiment.get(field) != after_experiment.get(field):
                protected_changes.append(f"experiments[{experiment_id}].{field}")

    before_status = _coerce_str(before_dossier.get("research_status"))
    after_status = _coerce_str(after_dossier.get("research_status"))
    if before_status in {"blocked", "insufficient_evidence"} and after_status == (
        "evidence_sufficient"
    ):
        protected_changes.append("research_status[unsupported_upgrade]")
    return list(dict.fromkeys(protected_changes))


def _correction_progress_assessment(
    *,
    before_errors: Sequence[str],
    after_errors: Sequence[str],
    before_dossier_sha256: str,
    after_dossier_sha256: str,
    repeated_state_count: int,
    fundamental_changes: Sequence[str],
    cumulative_correction_seconds: float,
    total_correction_seconds: float,
    original_investigation_seconds: float | None,
    best_error_count: int,
) -> dict[str, Any]:
    """Choose correction, acceptance, or restart from observed progress and cost."""
    before_ids = {_validation_error_identity(error) for error in before_errors}
    after_ids = {_validation_error_identity(error) for error in after_errors}
    resolved = sorted(before_ids - after_ids)
    introduced = sorted(after_ids - before_ids)
    objective_progress = len(after_ids) < best_error_count
    forward_frontier_advanced = (
        before_dossier_sha256 != after_dossier_sha256 or before_ids != after_ids
    )
    progress: dict[str, Any] = {
        "before_error_count": len(before_ids),
        "after_error_count": len(after_ids),
        "resolved_error_identities": resolved,
        "introduced_error_identities": introduced,
        "dossier_changed": before_dossier_sha256 != after_dossier_sha256,
        "repeated_state_count": repeated_state_count,
        "correction_seconds_since_best_progress": cumulative_correction_seconds,
        "total_correction_seconds": total_correction_seconds,
        "original_investigation_seconds": original_investigation_seconds,
        # A safe changed candidate is worth retaining as the next correction frontier, but only
        # objective progress resets the cost-since-best clock. This prevents 1-for-1 diagnostic
        # churn from running forever while preserving useful parent-to-child corrections.
        "forward_frontier_advanced": forward_frontier_advanced,
        "objective_progress": objective_progress,
        "cost_clock_reset": objective_progress,
    }
    if fundamental_changes:
        if (
            original_investigation_seconds is not None
            and original_investigation_seconds > 0.0
            and cumulative_correction_seconds >= original_investigation_seconds
        ):
            progress.update(
                decision="restart",
                reason="retained_evidence_rework_reached_investigation_cost",
                fundamental_change_paths=list(fundamental_changes),
            )
        elif repeated_state_count >= _REPEATED_CORRECTION_STATE_RESTART_COUNT:
            progress.update(
                decision="restart",
                reason="retained_evidence_change_repeated_after_feedback",
                fundamental_change_paths=list(fundamental_changes),
            )
        else:
            progress.update(
                decision="continue",
                reason="revert_accidental_retained_evidence_change",
                fundamental_change_paths=list(fundamental_changes),
            )
    elif not after_ids:
        progress.update(decision="accepted", reason="model_output_contract_satisfied")
    elif any(_repair_error_requires_new_investigation(error) for error in after_errors):
        progress.update(decision="restart", reason="integrity_or_new_investigation_required")
    elif objective_progress:
        # This remains progress even when the remaining error is new and resets the cost clock.
        progress.update(decision="continue", reason="best_error_count_decreased")
    elif repeated_state_count >= _REPEATED_CORRECTION_STATE_RESTART_COUNT:
        progress.update(decision="restart", reason="exact_state_repeated_after_feedback")
    elif (
        original_investigation_seconds is not None
        and original_investigation_seconds > 0.0
        and cumulative_correction_seconds >= original_investigation_seconds
    ):
        progress.update(decision="restart", reason="correction_cost_reached_investigation_cost")
    elif resolved:
        progress.update(decision="continue", reason="prior_errors_reworked_without_new_best")
    elif len(after_ids) < len(before_ids):
        progress.update(decision="continue", reason="local_error_count_decreased_without_new_best")
    elif forward_frontier_advanced:
        progress.update(decision="continue", reason="correction_state_changed")
    else:
        # One no-op is feedback, not proof of incapacity. Only the repeated state above stalls.
        progress.update(decision="continue", reason="first_materially_unchanged_correction")
    return progress


def _repair_contract(
    *,
    case_id: str,
    problem_id: str,
    source_attempt: dict[str, Any],
    validation_errors: Sequence[str],
    authorized_paths: Sequence[str],
    previous_correction_feedback: dict[str, Any] | None = None,
    research_capabilities: bool = False,
    independent_feedback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    projection = _research_retry_prior_attempt_projection(source_attempt)
    contract: dict[str, Any] = {
        "schema_version": 1,
        "mode": "targeted_model_output_repair",
        "case_id": case_id,
        "problem_id": problem_id,
        "source_attempt_sha256": source_attempt["attempt_sha256"],
        "baseline_dossier": projection["attempted_dossier"],
        "baseline_dossier_sha256": projection["attempted_dossier_sha256"],
        "baseline_projection_sha256": projection["projection_sha256"],
        "validation_errors": list(validation_errors),
        "normalized_error_identities": [
            _validation_error_identity(error) for error in validation_errors
        ],
        "remediation_hints": _research_retry_remediation_hints(validation_errors),
        "authorized_paths": list(authorized_paths),
        "immutable_evidence_paths": (
            list(_IMMUTABLE_RESEARCH_EVIDENCE_PATHS) if research_capabilities else []
        ),
        "previous_correction_feedback": (
            json.loads(json.dumps(previous_correction_feedback, ensure_ascii=False))
            if isinstance(previous_correction_feedback, dict)
            else None
        ),
        "research_capabilities": research_capabilities,
        "immutable_rule": (
            "Return the complete dossier. The verifier found that the retained proof is not yet "
            "sufficient. Continue the same investigation in this exact workspace. You may add or "
            "change evidence only by actually running and retaining the corresponding experiment, "
            "read, or artifact in this turn. Rerun every claimed experiment needed by the complete "
            "dossier. Do not implement a product fix or fabricate a result."
            if research_capabilities
            else (
                "Return the complete dossier. The baseline dossier is an unverified draft, and the "
                "validation hints identify likely fields rather than a closed edit whitelist. You "
                "may make correlated corrections to any model-authored draft claim. Preserve the "
                "retained run's actual observations: do not run tools, invent observations, or "
                "repeat research. The runner will verify every corrected evidence claim against "
                "the retained run before the dossier can advance."
            )
        ),
    }
    if isinstance(independent_feedback, Mapping):
        immutable_feedback = json.loads(json.dumps(dict(independent_feedback), ensure_ascii=False))
        contract["independent_feedback"] = immutable_feedback
        contract["independent_feedback_sha256"] = _canonical_json_sha256(immutable_feedback)
    contract["repair_contract_sha256"] = _canonical_json_sha256(contract)
    return contract


def _append_prompt_for_targeted_repair(contract: dict[str, Any]) -> str:
    payload = json.dumps(contract, ensure_ascii=False, indent=2)
    if contract.get("research_capabilities") is True:
        return (
            "# Backlog research evidence: same-session continuation\n\n"
            "The runner verifier found correctable gaps after your dossier was parsed. Continue "
            "the original investigation in this exact author session and workspace. Use research "
            "tools where the diagnostics require new evidence; actually execute and retain every "
            "new or changed experiment. Return one complete troubleshoot_v1 report with the full "
            "backlog_repro_research extension. Do not implement the product change. If the "
            "required "
            "evidence cannot be established, report insufficient_evidence honestly.\n\n"
            "## Verifier feedback payload (JSON)\n"
            f"{payload}\n"
        )
    return (
        "# Backlog research dossier: bounded correction\n\n"
        "This is a correction turn, not a new investigation. Do not inspect the repository, "
        "run commands, edit files, add evidence, or change an honest research status. Emit one "
        "complete troubleshoot_v1 report whose backlog_repro_research extension is the complete "
        "baseline dossier with the listed errors corrected. Field hints are guidance rather than "
        "a closed whitelist: correlated model-owned structure and interpretation corrections are "
        "allowed. The baseline's model-authored evidence fields are unverified draft claims, not "
        "immutable observations: correct malformed or unsupported claims while preserving what "
        "the retained run actually established. An honest downgrade to insufficient_evidence is "
        "allowed; never upgrade or fabricate support.\n\n"
        "## Dossier repair payload (JSON)\n"
        f"{payload}\n"
    )


def _research_attempt_workspace(attempt: dict[str, Any]) -> str | None:
    """Return the hash-recorded workspace path for a retained attempt, if available."""
    artifacts_raw = attempt.get("attempt_artifacts")
    artifacts = artifacts_raw if isinstance(artifacts_raw, list) else []
    workspace_ref = next(
        (
            artifact
            for artifact in artifacts
            if isinstance(artifact, dict)
            and artifact.get("kind") == "workspace_ref"
            and artifact.get("exists") is True
        ),
        None,
    )
    if not isinstance(workspace_ref, dict):
        return None
    path_raw = _coerce_str(workspace_ref.get("path"))
    if path_raw is None:
        return None
    try:
        obj = _load_json_object(Path(path_raw))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    workspace_raw = _coerce_str(obj.get("workspace_dir"))
    if workspace_raw is None:
        return None
    return str(Path(workspace_raw).resolve()).replace("\\", "/").casefold()


def _research_attempt_workspace_path(attempt: dict[str, Any]) -> Path | None:
    """Return the hash-bound runner workspace for one retained attempt.

    A dossier's ``repo_workspace``/``workspace_dir`` text is not provenance.  Only the
    runner-owned ``workspace_ref.json`` recorded in an immutable attempt may authorize a
    same-author continuation.  Rehash the attempt and its exact artifact before trusting the
    path so correction cannot silently move to a caller-supplied or subsequently edited
    workspace.
    """
    if attempt.get("attempt_sha256") != research_attempt_sha256(attempt):
        return None
    run_dir_raw = _coerce_str(attempt.get("run_dir"))
    if run_dir_raw is None:
        return None
    run_dir = Path(run_dir_raw).resolve()
    artifacts_raw = attempt.get("attempt_artifacts")
    artifacts = artifacts_raw if isinstance(artifacts_raw, list) else []
    workspace_ref = next(
        (
            artifact
            for artifact in artifacts
            if isinstance(artifact, dict)
            and artifact.get("kind") == "workspace_ref"
            and artifact.get("exists") is True
        ),
        None,
    )
    if not isinstance(workspace_ref, dict):
        return None
    path_raw = _coerce_str(workspace_ref.get("path"))
    if path_raw is None:
        return None
    workspace_ref_path = Path(path_raw).resolve()
    if workspace_ref_path != (run_dir / "workspace_ref.json").resolve():
        return None
    if not workspace_ref_path.is_file():
        return None
    try:
        workspace_ref_bytes = workspace_ref_path.read_bytes()
        workspace_ref_size = workspace_ref_path.stat().st_size
    except OSError:
        return None
    if sha256(workspace_ref_bytes).hexdigest() != workspace_ref.get(
        "sha256"
    ) or workspace_ref_size != workspace_ref.get("size_bytes"):
        return None
    try:
        obj = _load_json_object(workspace_ref_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    workspace_raw = _coerce_str(obj.get("workspace_dir"))
    return Path(workspace_raw).resolve() if workspace_raw is not None else None


def _research_continuation_workspace_path(
    *,
    source_attempt: dict[str, Any],
    continuation_run_dir: Path,
) -> Path | None:
    """Bind a continuation run to its retained author's exact workspace.

    The retained attempt supplies the content-addressed authority.  The new runner-owned
    workspace record must name that same path; a dossier field or a continuation record that
    points elsewhere is never accepted.  The continuation record is captured in the new
    attempt's artifact receipts immediately after candidate validation.
    """
    retained_workspace = _research_attempt_workspace_path(source_attempt)
    if retained_workspace is None:
        return None
    workspace_ref_path = continuation_run_dir.resolve() / "workspace_ref.json"
    try:
        workspace_ref = _load_json_object(workspace_ref_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    continuation_workspace_raw = _coerce_str(workspace_ref.get("workspace_dir"))
    if continuation_workspace_raw is None:
        return None
    continuation_workspace = Path(continuation_workspace_raw).resolve()
    if continuation_workspace != retained_workspace:
        return None
    return retained_workspace


def _research_attempt_revision(attempt: dict[str, Any]) -> str | None:
    """Return the commit recorded by the hash-bound target-ref receipt."""
    artifacts_raw = attempt.get("attempt_artifacts")
    artifacts = artifacts_raw if isinstance(artifacts_raw, list) else []
    target_ref = next(
        (
            artifact
            for artifact in artifacts
            if isinstance(artifact, dict)
            and artifact.get("kind") == "target_ref"
            and artifact.get("exists") is True
        ),
        None,
    )
    if not isinstance(target_ref, dict):
        return None
    path_raw = _coerce_str(target_ref.get("path"))
    if path_raw is None:
        return None
    try:
        obj = _load_json_object(Path(path_raw))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    revision = _coerce_str(obj.get("commit_sha"))
    return revision.casefold() if revision is not None else None


def _compatible_research_evidence_attempts(
    attempts: Sequence[dict[str, Any]],
    *,
    case_id: str,
    problem_id: str,
    repo_revision: str,
    agent_session_id: str,
    workspace: Path,
    current_run_dir: Path,
) -> list[dict[str, Any]]:
    """Return authenticated prior attempts from the same retained Codex researcher.

    A correction turn is not a new investigation.  Commands and file reads observed in an
    earlier turn remain evidence when the correction keeps the same case, revision, workspace,
    signed-in Codex route, and author session.  This selector deliberately admits only
    runner-recorded attempts whose complete artifact contract still verifies; the current run
    remains separate and authoritative for the report, workspace diff, and changed claims.
    """

    current = current_run_dir.resolve()
    compatible_by_run: dict[Path, dict[str, Any]] = {}
    for attempt in attempts:
        binding_errors: list[str] = []
        binding = _research_attempt_event_source_binding(
            attempt,
            expected_case_id=case_id,
            expected_problem_id=problem_id,
            expected_repo_revision=repo_revision,
            expected_workspace=workspace,
            expected_agent_session_id=agent_session_id,
            errors=binding_errors,
        )
        if binding is None:
            continue
        source_run = Path(str(binding["run_dir"])).resolve()
        if source_run == current:
            continue
        # A verifier-source record and its completed attempt can legitimately name
        # the same immutable run. Keep one source boundary and bind it to the latest
        # retained attempt record for that run.
        compatible_by_run[source_run] = json.loads(json.dumps(attempt, ensure_ascii=False))
    return list(compatible_by_run.values())


def _repair_candidate_from_run(
    *,
    result: Any,
    case_id: str,
    problem_id: str,
    evidence_assignment: dict[str, Any],
    allow_research_workspace_changes: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """Load and deterministically validate one correction-turn report."""
    run_dir = result.run_dir
    report_path = run_dir / "report.json"
    candidate: dict[str, Any] = {}
    errors: list[str] = []
    if result.exit_code != 0:
        errors.append(f"runner_exit_code:{result.exit_code}")
    errors.extend(
        f"runner_report_validation_error:{error}" for error in result.report_validation_errors
    )
    if not report_path.is_file():
        errors.append("research_report_missing")
        return candidate, errors
    try:
        report = _load_json_object(report_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"research_report_malformed:{type(exc).__name__}:{exc}")
        return candidate, errors
    errors.extend(
        f"research_report_schema_invalid:{error}"
        for error in _model_report_schema_errors(run_dir=run_dir, report=report)
    )
    extensions_raw = report.get("extensions")
    extensions = extensions_raw if isinstance(extensions_raw, dict) else {}
    candidate_raw = extensions.get(_EXTENSION_KEY)
    if not isinstance(candidate_raw, dict):
        errors.append(f"research_extension_missing:{_EXTENSION_KEY}")
        return candidate, errors
    candidate = dict(candidate_raw)
    candidate_pid = _coerce_str(candidate.get("problem_id"))
    candidate_case_id = _coerce_str(candidate.get("case_id"))
    if candidate_pid != problem_id:
        errors.append(
            f"research_dossier_problem_id_mismatch:expected={problem_id}:actual={candidate_pid}"
        )
    if candidate_case_id != case_id:
        errors.append(
            f"research_dossier_case_id_mismatch:expected={case_id}:actual={candidate_case_id}"
        )
    if candidate.get("implementation_performed") is True:
        errors.append("research_implementation_performed_forbidden")
    else:
        errors.extend(
            research_dossier_output_contract_errors(
                {
                    key: value
                    for key, value in candidate.items()
                    if key not in _RUNNER_OWNED_DOSSIER_FIELDS
                },
                evidence_assignment=evidence_assignment,
            )
        )
    diff_numstat = _load_diff_numstat(run_dir / "diff_numstat.json")
    modified_paths = [
        path
        for entry in diff_numstat
        for path in [_coerce_str(entry.get("path"))]
        if path is not None
    ]
    diff_class, _ = _classify_diff(
        modified_paths,
        writes_purpose=_string_list(candidate.get("writes_purpose")),
    )
    if modified_paths and (
        not allow_research_workspace_changes or diff_class == "suspicious_implementation"
    ):
        errors.append(
            "research_dossier_repair_workspace_changed:" + ",".join(sorted(modified_paths))
        )
    return candidate, list(dict.fromkeys(errors))


def _run_targeted_dossier_repairs(
    *,
    repo_input: str,
    repo_revision: str,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    case_id: str,
    problem_id: str,
    evidence_assignment: dict[str, Any],
    source_attempt: dict[str, Any],
    validation_errors: Sequence[str],
    first_attempt_number: int,
    candidate_validator: Callable[[dict[str, Any], Any], Sequence[str]] | None = None,
    research_capabilities: bool = False,
    attempt_kind: str = "model_output_repair",
    independent_feedback: Mapping[str, Any] | None = None,
    original_investigation_seconds: float | None = None,
    source_baseline_is_unverified_draft: bool | None = None,
    evidence_attempt_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Continue the authoring Codex session until corrected or demonstrably worth restarting."""
    workspace = _research_attempt_workspace_path(source_attempt)
    baseline_raw = source_attempt.get("attempted_dossier")
    baseline = (
        json.loads(json.dumps(baseline_raw, ensure_ascii=False))
        if isinstance(baseline_raw, dict)
        else {}
    )
    current_errors = _dedupe_validation_errors(validation_errors)
    baseline_is_unverified_draft = (
        source_baseline_is_unverified_draft
        if isinstance(source_baseline_is_unverified_draft, bool)
        else source_attempt.get("outcome") == "output_contract_invalid"
    )
    accepted_source = source_attempt
    best_dossier = json.loads(json.dumps(baseline, ensure_ascii=False))
    best_errors = list(current_errors)
    best_source = source_attempt
    attempts: list[dict[str, Any]] = []
    repair_runs: list[str] = []
    session_id = _coerce_str(source_attempt.get("agent_session_id"))
    if agent != "codex" or session_id is None:
        return {
            "dossier": baseline,
            "validation_errors": current_errors,
            "source_attempt_sha256": source_attempt.get("attempt_sha256"),
            "best_dossier": baseline,
            "best_validation_errors": current_errors,
            "best_source_attempt_sha256": source_attempt.get("attempt_sha256"),
            "attempts": attempts,
            "repair_run_dirs": repair_runs,
            "status": "same_session_continuation_unavailable",
            "expected_session_id": session_id,
            "observed_session_id": None,
            "continuation_failure": (
                "agent_is_not_codex" if agent != "codex" else "author_session_id_missing"
            ),
        }
    if workspace is None or not workspace.is_dir():
        return {
            "dossier": baseline,
            "validation_errors": current_errors,
            "source_attempt_sha256": source_attempt.get("attempt_sha256"),
            "best_dossier": baseline,
            "best_validation_errors": current_errors,
            "best_source_attempt_sha256": source_attempt.get("attempt_sha256"),
            "attempts": attempts,
            "repair_run_dirs": repair_runs,
            "status": "workspace_unavailable",
            "expected_session_id": session_id,
            "observed_session_id": None,
            "continuation_failure": "retained_author_workspace_missing",
        }
    original_seconds_raw = (
        original_investigation_seconds
        if original_investigation_seconds is not None
        else source_attempt.get("attempt_wall_seconds")
    )
    original_seconds = (
        float(original_seconds_raw)
        if isinstance(original_seconds_raw, (int, float))
        and not isinstance(original_seconds_raw, bool)
        and float(original_seconds_raw) > 0.0
        else None
    )
    correction_seconds_total = 0.0
    correction_seconds_since_best = 0.0
    state_counts: dict[str, int] = {}
    initial_state_key = _canonical_json_sha256(
        {
            "dossier_sha256": _canonical_json_sha256(baseline),
            "error_identities": sorted(
                _validation_error_identity(error) for error in current_errors
            ),
        }
    )
    invocation_failure_counts: dict[str, int] = {}
    previous_correction_feedback: dict[str, Any] | None = None
    repair_index = 0

    while True:
        authorized_paths = _targeted_repair_authorized_paths(
            current_errors,
            dossier=baseline,
        )
        if authorized_paths is None:
            break
        contract = _repair_contract(
            case_id=case_id,
            problem_id=problem_id,
            source_attempt=accepted_source,
            validation_errors=current_errors,
            authorized_paths=authorized_paths,
            previous_correction_feedback=previous_correction_feedback,
            research_capabilities=research_capabilities,
            independent_feedback=independent_feedback,
        )
        attempt_number = first_attempt_number + repair_index
        request = RunRequest(
            repo=repo_input,
            ref=repo_revision,
            agent=agent,
            policy=_POLICY,
            persona_id=_PERSONA_ID,
            mission_id=_MISSION_ID if research_capabilities else _REPAIR_MISSION_ID,
            evidence_role="research",
            origin_stage=(
                "repro_research_verifier_continuation"
                if research_capabilities
                else "repro_research_dossier_repair"
            ),
            parent_case_id=case_id,
            seed=_stable_seed(
                f"{problem_id}:dossier_repair:{attempt_number}:{contract['repair_contract_sha256']}"
            ),
            model=model,
            agent_append_system_prompt=_append_prompt_for_targeted_repair(contract),
            keep_workspace=True,
            resume_workspace_dir=workspace,
            codex_resume_session_id=session_id,
            # A repair turn has the same controlled subscription route as research but needs no
            # shell/tool authorization: all permissible input is in the content-bound prompt.
            codex_execpolicy_allow_prefixes=(
                _CODEX_RESEARCH_EXEC_ALLOW_PREFIXES if research_capabilities else ()
            ),
        )
        invocation_started = time.monotonic()
        try:
            result = run_once(config=cfg, request=request)
        except Exception as exc:  # noqa: BLE001
            failure_seconds = max(0.0, time.monotonic() - invocation_started)
            correction_seconds_total += failure_seconds
            correction_seconds_since_best += failure_seconds
            failure = f"research_dossier_repair_runner_exception:{type(exc).__name__}:{exc}"
            invocation_failure_counts[failure] = invocation_failure_counts.get(failure, 0) + 1
            failure_repeated = invocation_failure_counts[failure]
            economic_pause = bool(
                original_seconds is not None and correction_seconds_since_best >= original_seconds
            )
            failure_reason = (
                "correction_cost_reached_investigation_cost"
                if economic_pause
                else "same_session_invocation_failed_repeatedly"
                if failure_repeated >= 2
                else "retry_same_session_after_first_invocation_failure"
            )
            failure_progress = {
                "decision": ("restart" if economic_pause or failure_repeated >= 2 else "continue"),
                "reason": failure_reason,
                "before_error_count": len(current_errors),
                "after_error_count": 1,
                "resolved_error_identities": [],
                "introduced_error_identities": [failure],
                "dossier_changed": False,
                "repeated_state_count": failure_repeated,
                "correction_seconds_since_best_progress": correction_seconds_since_best,
                "total_correction_seconds": correction_seconds_total,
                "original_investigation_seconds": original_seconds,
                "authored_work_disposition": "retained",
                "retained_frontier": {
                    "latest_safe_dossier_sha256": _canonical_json_sha256(baseline),
                    "objective_best_dossier_sha256": _canonical_json_sha256(best_dossier),
                    "next_action": (
                        "fresh_restart_evaluation"
                        if economic_pause or failure_repeated >= 2
                        else "same_session_retry"
                    ),
                },
            }
            attempts.append(
                _research_invocation_failure_record(
                    attempt_number=attempt_number,
                    validation_errors=[failure],
                    attempt_kind=attempt_kind,
                    source_attempt_sha256=accepted_source["attempt_sha256"],
                    authorized_paths=authorized_paths,
                    baseline_dossier_sha256=_canonical_json_sha256(baseline),
                    baseline_projection_sha256=contract["baseline_projection_sha256"],
                    repair_contract_sha256=contract["repair_contract_sha256"],
                    validation_errors_before=current_errors,
                    agent_session_id=session_id,
                    resumed_from_session_id=session_id,
                    attempt_wall_seconds=failure_seconds,
                    repair_progress=failure_progress,
                )
            )
            previous_correction_feedback = {
                "source_attempt_sha256": attempts[-1]["attempt_sha256"],
                "assessment_reason": failure_progress["reason"],
                "validation_errors": [failure],
                "instruction": "Retry the correction in this same session; do not restart.",
            }
            if economic_pause or failure_repeated >= 2:
                return {
                    "dossier": best_dossier,
                    "validation_errors": best_errors,
                    "source_attempt_sha256": best_source.get("attempt_sha256"),
                    "best_dossier": best_dossier,
                    "best_validation_errors": best_errors,
                    "best_source_attempt_sha256": best_source.get("attempt_sha256"),
                    "attempts": attempts,
                    "repair_run_dirs": repair_runs,
                    "status": "restart:" + failure_reason,
                }
            repair_index += 1
            continue

        repair_runs.append(str(result.run_dir.resolve()))
        repair_seconds = _run_wall_seconds(result.run_dir) or 0.0
        correction_seconds_total += repair_seconds
        correction_seconds_since_best += repair_seconds
        external_wait = _runner_external_wait(result.run_dir)
        if external_wait is not None:
            wait_progress = {
                "decision": "parked",
                "reason": "codex_chatgpt_subscription_usage_limit",
                "before_error_count": len(current_errors),
                "after_error_count": len(current_errors),
                "resolved_error_identities": [],
                "introduced_error_identities": [],
                "dossier_changed": False,
                "repeated_state_count": 0,
                "correction_seconds_since_best_progress": correction_seconds_since_best,
                "total_correction_seconds": correction_seconds_total,
                "original_investigation_seconds": original_seconds,
                "authored_work_disposition": "retained",
                "external_wait": external_wait,
                "retained_frontier": {
                    "latest_safe_dossier_sha256": _canonical_json_sha256(baseline),
                    "objective_best_dossier_sha256": _canonical_json_sha256(best_dossier),
                    "candidate_disposition": "no_candidate_provider_wait",
                    "next_action": "resume_same_session_after_provider_reset",
                },
            }
            wait_attempt = _research_attempt_record(
                attempt_number=attempt_number,
                outcome="external_wait",
                run_dir=result.run_dir,
                report_path=result.run_dir / "report.json",
                validation_errors=current_errors,
                attempted_dossier=baseline,
                attempt_kind=attempt_kind,
                source_attempt_sha256=accepted_source["attempt_sha256"],
                authorized_paths=authorized_paths,
                baseline_dossier_sha256=_canonical_json_sha256(baseline),
                baseline_projection_sha256=contract["baseline_projection_sha256"],
                repair_contract_sha256=contract["repair_contract_sha256"],
                validation_errors_before=current_errors,
                agent_session_id=session_id,
                observed_agent_session_id=result.agent_session_id,
                resumed_from_session_id=session_id,
                attempt_wall_seconds=repair_seconds,
                repair_progress=wait_progress,
            )
            attempts.append(wait_attempt)
            return {
                "dossier": baseline,
                "validation_errors": current_errors,
                "source_attempt_sha256": accepted_source.get("attempt_sha256"),
                "best_dossier": best_dossier,
                "best_validation_errors": best_errors,
                "best_source_attempt_sha256": best_source.get("attempt_sha256"),
                "attempts": attempts,
                "repair_run_dirs": repair_runs,
                "status": "parked_external_wait",
                "external_wait": external_wait,
                "expected_session_id": session_id,
                "observed_session_id": result.agent_session_id,
                "continuation_failure": None,
            }
        candidate, candidate_errors = _repair_candidate_from_run(
            result=result,
            case_id=case_id,
            problem_id=problem_id,
            evidence_assignment=evidence_assignment,
            allow_research_workspace_changes=research_capabilities,
        )
        if candidate and not candidate_errors and candidate_validator is not None:
            try:
                candidate_errors.extend(
                    str(error) for error in candidate_validator(candidate, result)
                )
            except Exception as exc:  # noqa: BLE001
                candidate_errors.append(
                    f"research_evidence_candidate_verifier_exception:{type(exc).__name__}:{exc}"
                )
        if result.agent_session_id != session_id:
            candidate_errors.append(
                "research_dossier_repair_session_continuity_failed:"
                f"expected={session_id}:actual={result.agent_session_id}"
            )
        candidate_errors = _dedupe_validation_errors(candidate_errors)
        changed_paths = _json_changed_paths(baseline, candidate)
        fundamental_changes = (
            []
            if not baseline or research_capabilities or baseline_is_unverified_draft
            else _fundamental_evidence_changes(
                changed_paths,
                explicitly_authorized_paths=authorized_paths,
                before_dossier=baseline,
                after_dossier=candidate,
            )
        )
        state_key = _canonical_json_sha256(
            {
                "dossier_sha256": _canonical_json_sha256(candidate),
                "error_identities": sorted(
                    _validation_error_identity(error) for error in candidate_errors
                ),
            }
        )
        repeated_state_count = state_counts.get(state_key, 0) + 1
        state_counts[state_key] = repeated_state_count
        if state_key == initial_state_key and repair_index > 0:
            repeated_state_count = max(repeated_state_count, 2)
        progress = _correction_progress_assessment(
            before_errors=current_errors,
            after_errors=candidate_errors,
            before_dossier_sha256=_canonical_json_sha256(baseline),
            after_dossier_sha256=_canonical_json_sha256(candidate),
            repeated_state_count=repeated_state_count,
            fundamental_changes=fundamental_changes,
            cumulative_correction_seconds=correction_seconds_since_best,
            total_correction_seconds=correction_seconds_total,
            original_investigation_seconds=original_seconds,
            best_error_count=len(best_errors),
        )
        if result.agent_session_id != session_id:
            progress["decision"] = "restart"
            progress["reason"] = "same_session_continuity_failed"
            progress["observed_session_id"] = result.agent_session_id
        decision = str(progress["decision"])
        if decision == "restart":
            latest_safe = not fundamental_changes and result.agent_session_id == session_id
            progress["authored_work_disposition"] = "retained"
            progress["retained_frontier"] = {
                "latest_safe_dossier_sha256": _canonical_json_sha256(
                    candidate if latest_safe else baseline
                ),
                "objective_best_dossier_sha256": _canonical_json_sha256(best_dossier),
                "candidate_disposition": (
                    "retained_as_latest_safe"
                    if latest_safe
                    else "quarantined_while_prior_safe_frontier_is_retained"
                ),
                "next_action": "fresh_restart_or_pause_evaluation",
            }
        outcome = (
            "repair_contract_valid"
            if decision == "accepted"
            else "repair_contract_invalid"
            if decision == "continue" and not fundamental_changes
            else "repair_scope_rejected"
        )
        repair_attempt = _research_attempt_record(
            attempt_number=attempt_number,
            outcome=outcome,
            run_dir=result.run_dir,
            report_path=result.run_dir / "report.json",
            validation_errors=candidate_errors,
            attempted_dossier=candidate,
            attempt_kind=attempt_kind,
            source_attempt_sha256=accepted_source["attempt_sha256"],
            authorized_paths=authorized_paths,
            baseline_dossier_sha256=_canonical_json_sha256(baseline),
            baseline_projection_sha256=contract["baseline_projection_sha256"],
            repair_contract_sha256=contract["repair_contract_sha256"],
            validation_errors_before=current_errors,
            agent_session_id=session_id,
            observed_agent_session_id=result.agent_session_id,
            resumed_from_session_id=session_id,
            attempt_wall_seconds=repair_seconds,
            repair_progress=progress,
        )
        attempts.append(repair_attempt)
        if evidence_attempt_history is not None:
            evidence_attempt_history.append(dict(repair_attempt))
        previous_correction_feedback = {
            "source_attempt_sha256": repair_attempt["attempt_sha256"],
            "candidate_dossier_sha256": repair_attempt["attempted_dossier_sha256"],
            "assessment_reason": progress["reason"],
            "validation_errors": list(candidate_errors),
            "fundamental_change_paths": list(fundamental_changes),
            "instruction": (
                "Restore every retained-evidence path from baseline before correcting the "
                "model-owned structure."
                if fundamental_changes
                else "Use this assessment as feedback for the next correction."
            ),
        }
        if decision == "restart":
            latest_is_safe = not fundamental_changes and result.agent_session_id == session_id
            return {
                "dossier": candidate if latest_is_safe else baseline,
                "validation_errors": candidate_errors if latest_is_safe else current_errors,
                "source_attempt_sha256": (
                    repair_attempt.get("attempt_sha256")
                    if latest_is_safe
                    else accepted_source.get("attempt_sha256")
                ),
                "best_dossier": best_dossier,
                "best_validation_errors": best_errors,
                "best_source_attempt_sha256": best_source.get("attempt_sha256"),
                "attempts": attempts,
                "repair_run_dirs": repair_runs,
                "status": "restart:" + str(progress["reason"]),
                "expected_session_id": session_id,
                "observed_session_id": result.agent_session_id,
                "continuation_failure": (
                    "observed_session_changed"
                    if progress.get("reason") == "same_session_continuity_failed"
                    else None
                ),
            }
        if fundamental_changes:
            # The attempted correction is retained, but never becomes the next baseline. Give
            # the same session the exact original dossier plus feedback to revert the evidence
            # edit; only persistence or cost can justify restart.
            repair_index += 1
            continue
        baseline = candidate
        current_errors = candidate_errors
        accepted_source = repair_attempt
        if len(current_errors) <= len(best_errors):
            best_dossier = json.loads(json.dumps(baseline, ensure_ascii=False))
            best_errors = list(current_errors)
            best_source = repair_attempt
            if progress.get("cost_clock_reset") is True:
                correction_seconds_since_best = 0.0
        if decision == "accepted":
            return {
                "dossier": baseline,
                "validation_errors": [],
                "source_attempt_sha256": accepted_source.get("attempt_sha256"),
                "best_dossier": baseline,
                "best_validation_errors": [],
                "best_source_attempt_sha256": accepted_source.get("attempt_sha256"),
                "attempts": attempts,
                "repair_run_dirs": repair_runs,
                "status": "corrected",
            }
        repair_index += 1

    raise AssertionError("unreachable adaptive correction state")


def continue_research_dossier_from_independent_feedback(
    *,
    dossier: dict[str, Any],
    validation_errors: Sequence[str],
    repo_input: str,
    requested_repo_ref: str | None,
    resolved_repo_ref: str | None,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    replay_timeout_seconds: float | None,
    replay_executor: ReplayExecutor | None,
    artifacts_dir: Path,
    independent_feedback: Mapping[str, Any] | None = None,
    continuation_attempt_kind: str = "independent_qualification_research_continuation",
    original_investigation_seconds: float | None = None,
) -> dict[str, Any]:
    """Resume one retained researcher with external semantic feedback and reverify it.

    This is the evidence-capable qualification adapter.  It deliberately reuses the
    normal targeted-repair loop, original signed-in Codex session, retained workspace,
    source assignment, and full evidence verifier.  A locally valid rewrite is not
    accepted unless the new runner result independently re-establishes the research
    proof receipt.
    """

    case_id = _coerce_str(dossier.get("case_id"))
    problem_id = _coerce_str(dossier.get("problem_id"))
    repo_revision = _coerce_str(dossier.get("repo_revision"))
    assignment_raw = dossier.get("evidence_assignment")
    evidence_assignment = dict(assignment_raw) if isinstance(assignment_raw, dict) else {}
    evidence_atom_ids = _string_list(evidence_assignment.get("expected_atom_ids"))
    attempts_raw = dossier.get("research_attempts")
    attempts = (
        [dict(item) for item in attempts_raw if isinstance(item, dict)]
        if isinstance(attempts_raw, list)
        else []
    )
    errors = [str(error) for error in validation_errors if str(error).strip()]
    missing: list[str] = []
    if case_id is None:
        missing.append("case_id")
    if problem_id is None:
        missing.append("problem_id")
    if repo_revision is None:
        missing.append("repo_revision")
    if not evidence_assignment:
        missing.append("evidence_assignment")
    if not evidence_atom_ids:
        missing.append("evidence_atom_ids")
    if not attempts:
        missing.append("research_attempts")
    if not errors:
        missing.append("validation_errors")
    if missing:
        return {
            "status": "repairable_paused:research_feedback_context_missing",
            "dossier": dict(dossier),
            "validation_errors": ["research_feedback_context_missing:" + ",".join(missing)],
            "attempts": [],
            "authored_work_disposition": "retained",
        }
    assert case_id is not None
    assert problem_id is not None
    assert repo_revision is not None
    assert evidence_atom_ids

    source_attempt = _continuation_source_attempt(
        dossier=dossier,
        attempts=attempts,
    )
    explicit_original_seconds = (
        float(original_investigation_seconds)
        if isinstance(original_investigation_seconds, (int, float))
        and not isinstance(original_investigation_seconds, bool)
        and float(original_investigation_seconds) > 0.0
        else None
    )
    effective_original_seconds = (
        explicit_original_seconds
        if explicit_original_seconds is not None
        else _clean_investigation_estimate_seconds(attempts)
    )
    verified_candidates: dict[str, tuple[dict[str, Any], dict[str, Any], Path]] = {}
    origin_receipt_raw = dossier.get("evidence_verification")
    origin_receipt = origin_receipt_raw if isinstance(origin_receipt_raw, dict) else {}
    origin_attachment_raw = origin_receipt.get("origin_attachment_evidence")
    origin_attachment = (
        dict(origin_attachment_raw) if isinstance(origin_attachment_raw, dict) else {}
    )
    source_baseline_is_unverified_draft = bool(
        origin_receipt.get("status") != "verified"
        or source_attempt.get("outcome")
        in {
            "output_contract_invalid",
            "repair_contract_invalid",
            "repair_scope_rejected",
            "invocation_failed",
        }
    )
    evidence_attempt_history = [dict(attempt) for attempt in attempts]

    def validate_candidate(candidate: dict[str, Any], correction_result: Any) -> Sequence[str]:
        verification_run_dir = Path(correction_result.run_dir).resolve()
        prepared = {
            key: value
            for key, value in candidate.items()
            if key not in _RUNNER_OWNED_DOSSIER_FIELDS
        }
        prepared["research_schema_version"] = RESEARCH_PROOF_SCHEMA_VERSION
        prepared["evidence_assignment"] = evidence_assignment
        prepared["repo_revision"] = _canonical_repo_revision(verification_run_dir) or repo_revision
        diff_paths = [
            path
            for entry in _load_diff_numstat(verification_run_dir / "diff_numstat.json")
            for path in [_coerce_str(entry.get("path"))]
            if path is not None
        ]
        diff_class, diff_reasons = _classify_diff(
            diff_paths,
            writes_purpose=_string_list(prepared.get("writes_purpose")),
        )
        prepared["diff_classification"] = diff_class
        if diff_reasons:
            prepared["diff_suspicious_reasons"] = diff_reasons
        prepared["run_dir"] = str(verification_run_dir)
        prepared["runner_exit_code"] = int(correction_result.exit_code)
        prepared["runner_report_validation_errors"] = list(
            correction_result.report_validation_errors
        )
        refs_raw = prepared.get("artifact_refs")
        refs = list(refs_raw) if isinstance(refs_raw, list) else []
        known_paths = {
            str(ref.get("path"))
            for ref in refs
            if isinstance(ref, dict) and _coerce_str(ref.get("path")) is not None
        }
        for ref in _runner_artifact_refs(verification_run_dir):
            if ref["path"] not in known_paths:
                refs.append(ref)
        prepared["artifact_refs"] = refs
        retained_workspace = _research_attempt_workspace_path(source_attempt)
        retained_session_id = _coerce_str(source_attempt.get("agent_session_id"))
        evidence_attempts = (
            _compatible_research_evidence_attempts(
                evidence_attempt_history,
                case_id=case_id,
                problem_id=problem_id,
                repo_revision=prepared["repo_revision"],
                agent_session_id=retained_session_id,
                workspace=retained_workspace,
                current_run_dir=verification_run_dir,
            )
            if retained_workspace is not None and retained_session_id is not None
            else []
        )
        receipt = verify_research_evidence(
            prepared,
            run_dir=verification_run_dir,
            evidence_attempts=evidence_attempts,
            evidence_agent_session_id=retained_session_id,
            repo_revision=prepared["repo_revision"],
            case_id=case_id,
            problem_id=problem_id,
            expected_case_id=_coerce_str(prepared.get("case_id")),
            expected_problem_id=_coerce_str(prepared.get("problem_id")),
            evidence_assignment=evidence_assignment,
            evidence_atom_ids=list(evidence_atom_ids),
            revision_view_destination=(
                artifacts_dir
                / "revision_views"
                / sha256(
                    (f"{repo_input}\0{prepared['repo_revision']}\0{verification_run_dir}").encode()
                ).hexdigest()[:16]
            ),
            replay_timeout_seconds=replay_timeout_seconds,
            requested_repo_ref=requested_repo_ref,
            resolved_repo_ref=resolved_repo_ref,
            replay_executor=replay_executor,
        )
        if origin_attachment:
            workspace = _research_continuation_workspace_path(
                source_attempt=source_attempt,
                continuation_run_dir=verification_run_dir,
            )
            attachment_reads, attachment_errors = (
                _origin_attachment_read_receipts(
                    run_dir=verification_run_dir,
                    workspace_dir=workspace,
                    manifest=origin_attachment,
                    evidence_attempts=evidence_attempts,
                )
                if workspace is not None
                else ([], ["origin_attachment_workspace_unavailable"])
            )
            receipt["origin_attachment_evidence"] = origin_attachment
            receipt["origin_attachment_read_attestations"] = attachment_reads
            if attachment_errors:
                _fail_evidence_verification(receipt, errors=attachment_errors)
        verified_candidates[_canonical_json_sha256(candidate)] = (
            prepared,
            receipt,
            verification_run_dir,
        )
        return (
            []
            if receipt.get("status") == "verified"
            else _string_list(receipt.get("errors"))
            or ["research_evidence_verification_failed_without_diagnostic"]
        )

    repair = _run_targeted_dossier_repairs(
        repo_input=repo_input,
        repo_revision=repo_revision,
        agent=agent,
        model=model,
        cfg=cfg,
        case_id=case_id,
        problem_id=problem_id,
        evidence_assignment=evidence_assignment,
        source_attempt=source_attempt,
        validation_errors=errors,
        first_attempt_number=len(attempts) + 1,
        candidate_validator=validate_candidate,
        research_capabilities=True,
        attempt_kind=continuation_attempt_kind,
        independent_feedback=independent_feedback,
        original_investigation_seconds=effective_original_seconds,
        source_baseline_is_unverified_draft=source_baseline_is_unverified_draft,
        evidence_attempt_history=evidence_attempt_history,
    )
    repaired_raw = repair.get("dossier")
    repaired = dict(repaired_raw) if isinstance(repaired_raw, dict) else dict(dossier)
    accepted = verified_candidates.get(_canonical_json_sha256(repaired))
    new_attempts = [dict(item) for item in repair.get("attempts", []) if isinstance(item, dict)]
    if repair.get("status") == "corrected" and accepted is not None:
        prepared, receipt, verification_run_dir = accepted
        prepared["evidence_verification"] = receipt
        prepared["run_dir"] = str(verification_run_dir)
        workspace_dir = receipt.get("planning_workspace_dir")
        prepared["repo_workspace"] = workspace_dir if isinstance(workspace_dir, str) else None
        _set_research_attempts(prepared, [*attempts, *new_attempts])
        return {
            "status": "corrected",
            "dossier": prepared,
            "validation_errors": [],
            "attempts": new_attempts,
            "repair_run_dirs": list(repair.get("repair_run_dirs") or []),
            "expected_session_id": repair.get("expected_session_id"),
            "observed_session_id": repair.get("observed_session_id"),
            "authored_work_disposition": "retained",
        }
    best_raw = repair.get("best_dossier")
    best = dict(best_raw) if isinstance(best_raw, dict) else repaired
    best_errors_raw = repair.get("best_validation_errors")
    best_errors = (
        _string_list(best_errors_raw)
        if isinstance(best_errors_raw, list)
        else _string_list(repair.get("validation_errors")) or errors
    )
    best_verified = verified_candidates.get(_canonical_json_sha256(best))
    if best_verified is not None:
        retained, retained_receipt, retained_run_dir = best_verified
        retained["evidence_verification"] = retained_receipt
        retained["run_dir"] = str(retained_run_dir)
        retained_workspace = retained_receipt.get("planning_workspace_dir")
        retained["repo_workspace"] = (
            retained_workspace if isinstance(retained_workspace, str) else None
        )
    else:
        retained = json.loads(json.dumps(dossier, ensure_ascii=False))
        for key, value in best.items():
            if key not in _RUNNER_OWNED_DOSSIER_FIELDS:
                retained[key] = json.loads(json.dumps(value, ensure_ascii=False))
    _set_research_attempts(retained, [*attempts, *new_attempts])
    return {
        "status": str(repair.get("status") or "repairable_paused:research_correction_failed"),
        "dossier": retained,
        "validation_errors": best_errors,
        "best_source_attempt_sha256": repair.get("best_source_attempt_sha256"),
        "attempts": new_attempts,
        "repair_run_dirs": list(repair.get("repair_run_dirs") or []),
        "expected_session_id": repair.get("expected_session_id"),
        "observed_session_id": repair.get("observed_session_id"),
        "external_wait": repair.get("external_wait"),
        "continuation_failure": repair.get("continuation_failure"),
        "authored_work_disposition": "retained",
    }


def _resume_checkpoint_from_stage_document(
    stage_document: Mapping[str, Any] | None,
    *,
    selected_problem_ids: Sequence[str] = (),
    selected_case_ids: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    """Validate and project one persisted Stage-3 provider-wait checkpoint."""
    if not isinstance(stage_document, Mapping):
        return None, {}
    meta_raw = stage_document.get("input_meta")
    meta = meta_raw if isinstance(meta_raw, Mapping) else {}
    if meta.get("stage_status") in {"checkpointed_progress", "completed"}:
        return None, {}
    checkpoint_raw = meta.get("external_wait")
    checkpoint = dict(checkpoint_raw) if isinstance(checkpoint_raw, Mapping) else {}
    wait_raw = checkpoint.get("external_wait")
    wait = wait_raw if isinstance(wait_raw, Mapping) else {}
    checkpoint_without_hash = {
        key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"
    }
    if (
        meta.get("stage_status") != "parked_external_wait"
        or checkpoint.get("status") != "parked_external_wait"
        or checkpoint.get("scope") != "repro_research_stage"
        or checkpoint.get("reason") != "codex_chatgpt_subscription_usage_limit"
        or checkpoint.get("resume_status") != "checkpoint_persisted_same_author_resume_supported"
        or checkpoint.get("route") != "chatgpt_subscription"
        or checkpoint.get("api_fallback_allowed") is not False
        or wait.get("code") != "codex_chatgpt_subscription_usage_limit"
        or wait.get("provider") != "codex"
        or wait.get("state") != "parked"
        or wait.get("route") != "chatgpt_subscription"
        or wait.get("api_fallback_allowed") is not False
        or checkpoint.get("checkpoint_sha256") != _canonical_json_sha256(checkpoint_without_hash)
    ):
        raise ValueError("research_external_wait_resume_checkpoint_invalid")
    items_raw = stage_document.get("items")
    items = items_raw if isinstance(items_raw, list) else []
    item_problem_ids: list[str] = []
    by_problem_id: dict[str, dict[str, Any]] = {}
    for item_index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"research_external_wait_resume_dossier_invalid:{item_index}")
        problem_id = _coerce_str(item.get("problem_id"))
        if problem_id is None:
            raise ValueError(
                f"research_external_wait_resume_dossier_problem_id_missing:{item_index}"
            )
        if problem_id in by_problem_id:
            raise ValueError(f"research_external_wait_resume_dossier_duplicate:{problem_id}")
        item_problem_ids.append(problem_id)
        by_problem_id[problem_id] = dict(item)
    if list(selected_problem_ids) != item_problem_ids:
        raise ValueError("research_external_wait_resume_dossier_selection_changed")
    trigger_problem_id = _coerce_str(checkpoint.get("trigger_problem_id"))
    if trigger_problem_id is None or trigger_problem_id not in by_problem_id:
        raise ValueError("research_external_wait_resume_trigger_dossier_missing")
    trigger_case_id = _coerce_str(checkpoint.get("trigger_case_id"))
    trigger_dossier_case_id = _coerce_str(by_problem_id[trigger_problem_id].get("case_id"))
    current_trigger_case_id = (
        selected_case_ids.get(trigger_problem_id)
        if isinstance(selected_case_ids, Mapping)
        else None
    )
    if (
        trigger_case_id is None
        or trigger_dossier_case_id != trigger_case_id
        or current_trigger_case_id != trigger_case_id
    ):
        raise ValueError("research_external_wait_resume_trigger_case_changed")
    trigger_index = item_problem_ids.index(trigger_problem_id)
    checkpoint_sha256 = str(checkpoint["checkpoint_sha256"])
    expected_parked_reason = (
        "research_external_wait_stage_parked_before_dispatch:" + checkpoint_sha256
    )
    for item_index, problem_id in enumerate(item_problem_ids):
        blockers = _string_list(by_problem_id[problem_id].get("blocking_reasons"))
        is_parked_placeholder = expected_parked_reason in blockers
        if item_index < trigger_index and is_parked_placeholder:
            raise ValueError(
                f"research_external_wait_resume_pretrigger_placeholder_invalid:{problem_id}"
            )
        if item_index > trigger_index and blockers != [expected_parked_reason]:
            raise ValueError(
                f"research_external_wait_resume_parked_placeholder_invalid:{problem_id}"
            )
    return checkpoint, by_problem_id


_STAGE3_SEMANTIC_PROOF_CONTRACT_VERSION = "root_cause_research_proof_v2_cumulative_evidence"
_STAGE3_ORCHESTRATION_LINEAGE_FIELDS = frozenset(
    {"canonical_problem_id", "case_member_problem_ids", "same_cause_group_id"}
)


def stage3_research_dossier_resume_sha256(dossier: Mapping[str, Any]) -> str:
    """Hash the authored proof while ignoring derived orchestration annotations.

    Canonical case-lineage annotations are reapplied from the hash-bound upstream
    case registry after resume.  They must not make an otherwise identical research
    proof unusable merely because Stage 3 completed before orchestration attached
    those derived fields.
    """

    projection = {
        key: value
        for key, value in dossier.items()
        if key not in _STAGE3_ORCHESTRATION_LINEAGE_FIELDS
    }
    return _canonical_json_sha256(projection)


def stage3_research_compatibility_contract(*, agent: str) -> dict[str, Any]:
    """Return the material Stage-3 resume contract.

    Model selection and prompt prose are intentionally absent: changing either is
    compatible with reusing a proof that still satisfies the current persisted
    evidence contract.  Agent identity, the Codex subscription route, and an
    explicit semantic proof-contract version are material.
    """

    normalized_agent = str(agent).strip().casefold()
    if not normalized_agent:
        raise ValueError("stage3_research_compatibility_agent_missing")
    codex_subscription = normalized_agent == "codex"
    contract: dict[str, Any] = {
        "schema_version": 1,
        "contract_kind": "stage3_research_resume_compatibility",
        "semantic_proof_contract_version": _STAGE3_SEMANTIC_PROOF_CONTRACT_VERSION,
        "agent": normalized_agent,
        "execution_route": {
            "route": ("chatgpt_subscription" if codex_subscription else "configured_agent_backend"),
            "host_codex_data_required": codex_subscription,
            "api_fallback_allowed": False if codex_subscription else None,
        },
        "declared_compatible_changes": ["model_selection", "prompt_prose"],
    }
    contract["contract_sha256"] = _canonical_json_sha256(contract)
    return contract


def _valid_stage3_research_compatibility_contract(
    value: Any,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    contract = dict(value)
    without_hash = {key: item for key, item in contract.items() if key != "contract_sha256"}
    route_raw = contract.get("execution_route")
    route = route_raw if isinstance(route_raw, Mapping) else {}
    agent = _coerce_str(contract.get("agent"))
    codex_subscription = agent == "codex"
    if (
        set(contract)
        != {
            "schema_version",
            "contract_kind",
            "semantic_proof_contract_version",
            "agent",
            "execution_route",
            "declared_compatible_changes",
            "contract_sha256",
        }
        or contract.get("schema_version") != 1
        or contract.get("contract_kind") != "stage3_research_resume_compatibility"
        or not isinstance(contract.get("semantic_proof_contract_version"), str)
        or agent is None
        or set(route) != {"route", "host_codex_data_required", "api_fallback_allowed"}
        or route.get("route")
        != ("chatgpt_subscription" if codex_subscription else "configured_agent_backend")
        or route.get("host_codex_data_required") is not codex_subscription
        or route.get("api_fallback_allowed") != (False if codex_subscription else None)
        or contract.get("declared_compatible_changes") != ["model_selection", "prompt_prose"]
        or contract.get("contract_sha256") != _canonical_json_sha256(without_hash)
    ):
        return None
    return json.loads(json.dumps(contract, ensure_ascii=False))


def _completed_prefix_checkpoint(
    *,
    selected_problems: Sequence[Mapping[str, Any]],
    completed_dossiers: Sequence[Mapping[str, Any]],
    resolved_repo_ref: str | None,
    compatibility_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind a completed Stage-3 prefix so a process crash does not repeat it."""
    selected = [
        {
            "problem_id": _coerce_str(problem.get("problem_id")),
            "case_id": _coerce_str(problem.get("case_id")) or "case:unassigned",
            "assignment_sha256": (
                problem.get("evidence_assignment", {}).get("assignment_sha256")
                if isinstance(problem.get("evidence_assignment"), Mapping)
                else None
            ),
        }
        for problem in selected_problems
    ]
    completed = [
        {
            "problem_id": _coerce_str(dossier.get("problem_id")),
            "case_id": _coerce_str(dossier.get("case_id")),
            "dossier_sha256": stage3_research_dossier_resume_sha256(dossier),
        }
        for dossier in completed_dossiers
    ]
    checkpoint: dict[str, Any] = {
        "schema_version": 1,
        "status": "checkpointed_progress",
        "scope": "repro_research_stage",
        "resolved_repo_ref": resolved_repo_ref,
        "research_compatibility": json.loads(
            json.dumps(compatibility_contract, ensure_ascii=False)
        ),
        "selected": selected,
        "completed_prefix": completed,
    }
    checkpoint["checkpoint_sha256"] = _canonical_json_sha256(checkpoint)
    return checkpoint


def completed_stage3_checkpoint(
    *,
    dossiers: Sequence[Mapping[str, Any]],
    fresh_research_dossier_count: int,
    retained_research_reused_count: int,
    compatibility_contract: Mapping[str, Any],
    progress_checkpoint: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind every final Stage-3 item while retaining the fresh/reused boundary."""

    compatibility = _valid_stage3_research_compatibility_contract(compatibility_contract)
    if compatibility is None:
        raise ValueError("research_completed_compatibility_contract_invalid")
    if (
        isinstance(fresh_research_dossier_count, bool)
        or not isinstance(fresh_research_dossier_count, int)
        or fresh_research_dossier_count < 0
        or isinstance(retained_research_reused_count, bool)
        or not isinstance(retained_research_reused_count, int)
        or retained_research_reused_count < 0
        or fresh_research_dossier_count + retained_research_reused_count != len(dossiers)
    ):
        raise ValueError("research_completed_dossier_counts_invalid")
    progress = dict(progress_checkpoint)
    progress_without_hash = {
        key: item for key, item in progress.items() if key != "checkpoint_sha256"
    }
    if progress.get("checkpoint_sha256") != _canonical_json_sha256(progress_without_hash):
        raise ValueError("research_completed_progress_checkpoint_invalid")
    completed_items = [
        {
            "problem_id": _coerce_str(dossier.get("problem_id")),
            "case_id": _coerce_str(dossier.get("case_id")),
            "dossier_sha256": stage3_research_dossier_resume_sha256(dossier),
        }
        for dossier in dossiers
    ]
    checkpoint: dict[str, Any] = {
        "schema_version": 1,
        "status": "completed",
        "scope": "repro_research_stage",
        "fresh_research_dossier_count": fresh_research_dossier_count,
        "retained_research_reused_count": retained_research_reused_count,
        "research_compatibility_sha256": compatibility["contract_sha256"],
        "progress_checkpoint_sha256": progress["checkpoint_sha256"],
        "completed_items": completed_items,
    }
    checkpoint["checkpoint_sha256"] = _canonical_json_sha256(checkpoint)
    return checkpoint


def _validated_completed_stage3_checkpoint(
    stage_document: Mapping[str, Any],
    *,
    expected_compatibility_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return one intact final checkpoint, optionally bound to current semantics."""

    meta_raw = stage_document.get("input_meta")
    meta = meta_raw if isinstance(meta_raw, Mapping) else {}
    checkpoint_raw = meta.get("completed_stage_checkpoint")
    checkpoint = dict(checkpoint_raw) if isinstance(checkpoint_raw, Mapping) else {}
    without_hash = {key: item for key, item in checkpoint.items() if key != "checkpoint_sha256"}
    compatibility = _valid_stage3_research_compatibility_contract(
        meta.get("research_compatibility")
    )
    progress_raw = meta.get("progress_checkpoint")
    progress = dict(progress_raw) if isinstance(progress_raw, Mapping) else {}
    progress_without_hash = {
        key: item for key, item in progress.items() if key != "checkpoint_sha256"
    }
    items_raw = stage_document.get("items")
    items = items_raw if isinstance(items_raw, list) else []
    fresh_count = checkpoint.get("fresh_research_dossier_count")
    reused_count = checkpoint.get("retained_research_reused_count")
    summaries_raw = checkpoint.get("completed_items")
    summaries = summaries_raw if isinstance(summaries_raw, list) else []
    expected_compatibility = (
        dict(expected_compatibility_contract)
        if isinstance(expected_compatibility_contract, Mapping)
        else None
    )
    if (
        stage_document.get("stage") != _STAGE
        or meta.get("stage_status") != "completed"
        or stage_document.get("item_count") != len(items)
        or checkpoint.get("schema_version") != 1
        or checkpoint.get("status") != "completed"
        or checkpoint.get("scope") != "repro_research_stage"
        or checkpoint.get("checkpoint_sha256") != _canonical_json_sha256(without_hash)
        or compatibility is None
        or (expected_compatibility is not None and compatibility != expected_compatibility)
        or checkpoint.get("research_compatibility_sha256") != compatibility.get("contract_sha256")
        or progress.get("checkpoint_sha256") != _canonical_json_sha256(progress_without_hash)
        or checkpoint.get("progress_checkpoint_sha256") != progress.get("checkpoint_sha256")
        or isinstance(fresh_count, bool)
        or not isinstance(fresh_count, int)
        or fresh_count < 0
        or isinstance(reused_count, bool)
        or not isinstance(reused_count, int)
        or reused_count < 0
        or fresh_count + reused_count != len(items)
        or len(summaries) != len(items)
    ):
        return None
    progress_compatibility = _valid_stage3_research_compatibility_contract(
        progress.get("research_compatibility")
    )
    progress_completed_raw = progress.get("completed_prefix")
    progress_completed = progress_completed_raw if isinstance(progress_completed_raw, list) else []
    progress_selected_raw = progress.get("selected")
    progress_selected = progress_selected_raw if isinstance(progress_selected_raw, list) else []
    if (
        progress.get("schema_version") != 1
        or progress.get("status") != "checkpointed_progress"
        or progress.get("scope") != "repro_research_stage"
        or progress_compatibility != compatibility
        or len(progress_completed) != fresh_count
        or len(progress_selected) != fresh_count
    ):
        return None
    for index, (summary, item) in enumerate(zip(summaries, items, strict=True)):
        if (
            not isinstance(summary, Mapping)
            or not isinstance(item, Mapping)
            or summary.get("problem_id") != item.get("problem_id")
            or summary.get("case_id") != item.get("case_id")
            or summary.get("dossier_sha256") != stage3_research_dossier_resume_sha256(item)
        ):
            return None
        if index < fresh_count:
            fresh_summary = progress_completed[index]
            if (
                not isinstance(fresh_summary, Mapping)
                or fresh_summary.get("problem_id") != summary.get("problem_id")
                or fresh_summary.get("case_id") != summary.get("case_id")
                or fresh_summary.get("dossier_sha256") != summary.get("dossier_sha256")
            ):
                return None
    return json.loads(json.dumps(checkpoint, ensure_ascii=False))


def _resume_completed_prefix_from_stage_document(
    stage_document: Mapping[str, Any] | None,
    *,
    selected_problems: Sequence[Mapping[str, Any]],
    resolved_repo_ref: str | None,
    expected_compatibility_contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Validate and return only a sequential, fully committed Stage-3 prefix."""
    if not isinstance(stage_document, Mapping):
        return []
    meta_raw = stage_document.get("input_meta")
    meta = meta_raw if isinstance(meta_raw, Mapping) else {}
    stage_status = meta.get("stage_status")
    if stage_status not in {"checkpointed_progress", "completed"}:
        return []
    checkpoint_raw = meta.get("progress_checkpoint")
    checkpoint = dict(checkpoint_raw) if isinstance(checkpoint_raw, Mapping) else {}
    checkpoint_without_hash = {
        key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"
    }
    expected_selected = [
        {
            "problem_id": _coerce_str(problem.get("problem_id")),
            "case_id": _coerce_str(problem.get("case_id")) or "case:unassigned",
            "assignment_sha256": (
                problem.get("evidence_assignment", {}).get("assignment_sha256")
                if isinstance(problem.get("evidence_assignment"), Mapping)
                else None
            ),
        }
        for problem in selected_problems
    ]
    completed_raw = checkpoint.get("completed_prefix")
    completed = completed_raw if isinstance(completed_raw, list) else []
    items_raw = stage_document.get("items")
    all_items = items_raw if isinstance(items_raw, list) else []
    if stage_status == "completed":
        completion = _validated_completed_stage3_checkpoint(
            stage_document,
            expected_compatibility_contract=expected_compatibility_contract,
        )
        if completion is None:
            raise ValueError("research_completed_resume_checkpoint_invalid")
        fresh_count = completion["fresh_research_dossier_count"]
        items = all_items[:fresh_count]
    else:
        items = all_items
    persisted_compatibility = _valid_stage3_research_compatibility_contract(
        checkpoint.get("research_compatibility")
    )
    if persisted_compatibility is None:
        raise ValueError("research_progress_resume_compatibility_invalid")
    if persisted_compatibility != dict(expected_compatibility_contract):
        raise ValueError("research_progress_resume_compatibility_changed")
    if (
        checkpoint.get("schema_version") != 1
        or checkpoint.get("status") != "checkpointed_progress"
        or checkpoint.get("scope") != "repro_research_stage"
        or checkpoint.get("resolved_repo_ref") != resolved_repo_ref
        or checkpoint.get("selected") != expected_selected
        or checkpoint.get("checkpoint_sha256") != _canonical_json_sha256(checkpoint_without_hash)
        or len(completed) != len(items)
        or len(items) > len(expected_selected)
    ):
        raise ValueError("research_progress_resume_checkpoint_invalid")
    retained: list[dict[str, Any]] = []
    for index, (summary, item) in enumerate(zip(completed, items, strict=True)):
        if not isinstance(summary, Mapping) or not isinstance(item, dict):
            raise ValueError(f"research_progress_resume_dossier_invalid:{index}")
        expected = expected_selected[index]
        if (
            summary.get("problem_id") != expected["problem_id"]
            or summary.get("case_id") != expected["case_id"]
            or _coerce_str(item.get("problem_id")) != expected["problem_id"]
            or _coerce_str(item.get("case_id")) != expected["case_id"]
            or summary.get("dossier_sha256") != stage3_research_dossier_resume_sha256(item)
        ):
            raise ValueError(f"research_progress_resume_prefix_changed:{index}")
        proof_item = {
            key: value
            for key, value in item.items()
            if key not in _STAGE3_ORCHESTRATION_LINEAGE_FIELDS
        }
        validated, _ = parse_research_dossier_list(json.dumps([proof_item]))
        persisted = validated[0]
        attempt_errors = _persisted_research_attempt_errors(persisted)
        if attempt_errors:
            raise ValueError(
                "research_progress_resume_attempt_changed:"
                + str(expected["problem_id"])
                + ":"
                + ",".join(attempt_errors)
            )
        verification_raw = persisted.get("evidence_verification")
        verification = verification_raw if isinstance(verification_raw, dict) else {}
        if verification.get("status") == "verified":
            evidence_valid, evidence_errors = verify_persisted_research_evidence(persisted)
            if not evidence_valid or evidence_errors:
                raise ValueError(
                    "research_progress_resume_evidence_changed:"
                    + str(expected["problem_id"])
                    + ":"
                    + ",".join(evidence_errors or ["persisted_verification_failed"])
                )
        retained.append(persisted)
    return retained


def resume_research_dossier_from_external_wait(
    *,
    dossier: dict[str, Any],
    checkpoint: Mapping[str, Any],
    repo_input: str,
    requested_repo_ref: str | None,
    resolved_repo_ref: str | None,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    replay_timeout_seconds: float | None,
    replay_executor: ReplayExecutor | None,
    artifacts_dir: Path,
) -> dict[str, Any]:
    """Rehydrate one parked author frontier and continue it in the exact Codex session."""
    validated, _ = parse_research_dossier_list(json.dumps([dossier]))
    retained = validated[0]
    attempts_raw = retained.get("research_attempts")
    attempts = (
        [dict(item) for item in attempts_raw if isinstance(item, dict)]
        if isinstance(attempts_raw, list)
        else []
    )
    if not attempts or attempts[-1].get("outcome") != "external_wait":
        raise ValueError("research_external_wait_resume_attempt_missing")
    wait_attempt = attempts[-1]
    progress_raw = wait_attempt.get("repair_progress")
    progress = progress_raw if isinstance(progress_raw, dict) else {}
    wait_raw = progress.get("external_wait")
    wait = wait_raw if isinstance(wait_raw, dict) else {}
    checkpoint_wait_raw = checkpoint.get("external_wait")
    checkpoint_wait = checkpoint_wait_raw if isinstance(checkpoint_wait_raw, Mapping) else {}
    run_dir_raw = _coerce_str(wait_attempt.get("run_dir"))
    if (
        progress.get("decision") != "parked"
        or wait.get("code") != "codex_chatgpt_subscription_usage_limit"
        or wait.get("route") != "chatgpt_subscription"
        or wait.get("api_fallback_allowed") is not False
        or wait.get("error_artifact_sha256") != checkpoint_wait.get("error_artifact_sha256")
        or run_dir_raw is None
    ):
        raise ValueError("research_external_wait_resume_attempt_invalid")
    live_wait = _runner_external_wait(Path(run_dir_raw).resolve())
    if (
        live_wait is None
        or live_wait.get("error_artifact_sha256") != wait.get("error_artifact_sha256")
        or live_wait.get("error_artifact_size_bytes") != wait.get("error_artifact_size_bytes")
    ):
        raise ValueError("research_external_wait_resume_artifact_changed")
    if wait_attempt.get("attempt_sha256") != research_attempt_sha256(wait_attempt):
        raise ValueError("research_external_wait_resume_attempt_hash_changed")
    wait_attempt_errors = _persisted_research_attempt_errors({"research_attempts": [wait_attempt]})
    if wait_attempt_errors:
        raise ValueError(
            "research_external_wait_resume_wait_attempt_invalid:" + ",".join(wait_attempt_errors)
        )
    checkpoint_expected_session = _coerce_str(checkpoint.get("expected_session_id"))
    checkpoint_observed_session = _coerce_str(checkpoint.get("observed_session_id"))
    attempt_expected_session = _coerce_str(wait_attempt.get("agent_session_id"))
    attempt_observed_session = _coerce_str(wait_attempt.get("observed_agent_session_id"))
    if (
        checkpoint_expected_session is None
        or checkpoint_observed_session is None
        or attempt_expected_session != checkpoint_expected_session
        or attempt_observed_session != checkpoint_observed_session
    ):
        raise ValueError("research_external_wait_resume_session_provenance_changed")
    wait_revision = _research_attempt_revision(wait_attempt)
    retained_revision = _coerce_str(retained.get("repo_revision"))
    if (
        wait_revision is None
        or retained_revision is None
        or wait_revision != retained_revision.casefold()
    ):
        raise ValueError("research_external_wait_resume_target_revision_changed")
    prior = dict(retained)
    prior["research_attempts"] = attempts[:-1]
    prior_attempt_errors = _persisted_research_attempt_errors(prior)
    if prior_attempt_errors:
        raise ValueError(
            "research_external_wait_resume_prior_attempt_invalid:" + ",".join(prior_attempt_errors)
        )
    validation_errors = _string_list(wait_attempt.get("validation_errors"))
    if not validation_errors:
        raise ValueError("research_external_wait_resume_validation_errors_missing")
    return continue_research_dossier_from_independent_feedback(
        dossier=retained,
        validation_errors=validation_errors,
        repo_input=repo_input,
        requested_repo_ref=requested_repo_ref,
        resolved_repo_ref=resolved_repo_ref,
        agent=agent,
        model=model,
        cfg=cfg,
        replay_timeout_seconds=replay_timeout_seconds,
        replay_executor=replay_executor,
        artifacts_dir=artifacts_dir,
        independent_feedback={
            "kind": "provider_external_wait_resume",
            "checkpoint_sha256": checkpoint.get("checkpoint_sha256"),
            "instruction": (
                "The provider reset has cleared. Continue the retained investigation in this "
                "same author session and correct the still-recorded validation errors."
            ),
        },
        continuation_attempt_kind="evidence_verification_research_continuation",
    )


def _append_prompt_for_problem(
    *,
    repo_root: Path,
    problem_payload: dict[str, Any],
) -> str:
    """Build a system-prompt append string containing stage guidance + problem context."""
    guidance_path = repo_root / _GUIDANCE_PATH
    if not guidance_path.exists():
        raise FileNotFoundError(f"Missing stage guidance: {guidance_path}")
    guidance_text = guidance_path.read_text(encoding="utf-8")

    repo_intent_path = repo_root / _REPO_INTENT_PATH
    repo_intent_text = (
        repo_intent_path.read_text(encoding="utf-8") if repo_intent_path.exists() else ""
    )

    prompt_payload = dict(problem_payload)
    assignment_raw = prompt_payload.get("evidence_assignment")
    assignment = dict(assignment_raw) if isinstance(assignment_raw, dict) else {}
    origin_raw = assignment.get("origin_attachment_evidence")
    origin = origin_raw if isinstance(origin_raw, dict) else {}
    if origin:
        artifacts_raw = origin.get("artifacts")
        artifacts = artifacts_raw if isinstance(artifacts_raw, list) else []
        compact_origin = {
            "schema_version": origin.get("schema_version"),
            "format": origin.get("format"),
            "manifest_file": origin.get("manifest_file"),
            "manifest_file_sha256": origin.get("manifest_file_sha256"),
            "materialization_sha256": origin.get("materialization_sha256"),
            "atom_refs": origin.get("atom_refs", []),
            "errors": origin.get("errors", []),
            "artifacts": [
                {
                    "artifact_sha256": artifact.get("artifact_sha256"),
                    "size_bytes": artifact.get("size_bytes"),
                    "manifest_file": artifact.get("manifest_file"),
                    "manifest_file_sha256": artifact.get("manifest_file_sha256"),
                    "chunk_count": artifact.get("chunk_count"),
                }
                for artifact in artifacts
                if isinstance(artifact, dict)
            ],
        }
        assignment["origin_attachment_evidence"] = compact_origin
        prompt_payload["evidence_assignment"] = assignment

    payload = json.dumps(prompt_payload, ensure_ascii=False, indent=2)

    parts: list[str] = []
    parts.append("# Backlog reproduce-plus-research: context")
    parts.append("")
    parts.append("## Stage guidance (repo-owned)")
    parts.append(guidance_text.strip())
    parts.append("")
    if repo_intent_text.strip():
        parts.append("## Repo intent (repo-owned)")
        parts.append(repo_intent_text.strip())
        parts.append("")
    parts.append("## Assigned problem payload (JSON)")
    parts.append(payload)
    parts.append("")
    origin_raw = assignment.get("origin_attachment_evidence")
    origin = origin_raw if isinstance(origin_raw, dict) else {}
    if origin.get("atom_refs"):
        parts.append("## Required origin-attachment reads")
        parts.append(
            "The host artifact_ref paths are provenance only and may be invisible here. "
            "Use the hash-verified workspace paths in evidence_assignment."
        )
        parts.append(
            "Before making a mechanism claim, read each artifact manifest_file and every "
            "complete bounded chunk file declared by that manifest. The runner retains and "
            "revalidates those read events; the large chunk list is intentionally not inlined."
        )
        parts.append("")
    parts.append(
        "Reminder: stage-3 success is reproduction/bounding with evidence, NOT implementation."
    )
    return "\n".join(parts).strip() + "\n"


def run_repro_research_stage(
    *,
    repo_root: Path,
    repo_input: str | None,
    repo_ref: str | None,
    target_slug: str | None,
    selected_problems: Sequence[dict[str, Any]],
    artifacts_dir: Path,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    dry_run: bool,
    replay_timeout_seconds: float | None = 300.0,
    replay_executor: ReplayExecutor | None = None,
    replay_executor_metadata: dict[str, Any] | None = None,
    resume_stage_document: Mapping[str, Any] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    _attempt_number: int = 1,
    _full_attempt_kind: str = "full_research",
    _full_source_attempt_sha256: str | None = None,
    _full_baseline_dossier_sha256: str | None = None,
    _full_baseline_projection_sha256: str | None = None,
    _full_validation_errors_before: Sequence[str] = (),
    _full_restart_provenance: dict[str, Any] | None = None,
    _prior_attempts: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Run stage 3 repro-plus-research for selected problems.

    Parameters
    ----------
    repo_root:
        Runner repo root (the monorepo that contains `.usertest/`).
    repo_input:
        Target repo input for acquisition (path, git URL, pip:/pdm: spec, etc.).
    target_slug:
        Optional runs target slug (for metadata only).
    selected_problems:
        Selected problems from stage 2. Each entry must contain ``problem_id``.
        The dict is passed through to the mission via an appended system prompt.
    artifacts_dir:
        Base compiled artifact directory (``*.backlog_artifacts``). Stage 3 writes
        request metadata under ``artifacts_dir / "repro_research"``.
    agent:
        Agent backend to run (``codex``, ``claude``, ``gemini``).
    model:
        Optional model override.
    cfg:
        Runner configuration.
    dry_run:
        When ``True``, do not invoke any agent; return deterministic placeholder dossiers.

    Returns
    -------
    dict[str, Any]
        Stage document dict (see ``backlog_core.stage_contracts.build_stage_document``).

    Case-local runner, report, extension, and dossier failures are returned as explicit
    blocked research proofs so unrelated cases continue. A repair-authorizable model-output
    failure receives exact validator feedback in the original Codex author session. Correction
    continues while it is improving the best known dossier or remains cheaper than repeating the
    original investigation; replacing two errors with one different error is still progress.
    A fresh, complete case-local investigation is reserved for demonstrated correction
    nonprogress, session/workspace integrity failures, or correction cost approaching the original
    investigation. Evidence-assignment and verification failures never use either path. Global
    configuration failures (for example a missing repo reference or stage guidance) still raise
    because no case can be researched correctly under that configuration. A runner-attested
    ChatGPT subscription usage limit is provider-global: it retains the triggering frontier and
    emits parked placeholders for every remaining selected case without further model dispatch.
    """
    if _attempt_number < 1:
        raise ValueError("_attempt_number must be positive")
    if _attempt_number != len(_prior_attempts) + 1:
        raise ValueError("_attempt_number must follow retained prior attempts")
    if _full_attempt_kind not in {"full_research", "fresh_research_retry"}:
        raise ValueError("_full_attempt_kind must identify a full research invocation")
    if _full_attempt_kind == "full_research" and any(
        value is not None
        for value in (
            _full_source_attempt_sha256,
            _full_baseline_dossier_sha256,
            _full_baseline_projection_sha256,
            _full_restart_provenance,
        )
    ):
        raise ValueError("initial full research cannot carry retry provenance")
    if _full_attempt_kind == "fresh_research_retry" and not isinstance(
        _full_restart_provenance, dict
    ):
        raise ValueError("fresh research retry requires restart provenance")
    stage_artifacts_dir = artifacts_dir / _STAGE
    stage_artifacts_dir.mkdir(parents=True, exist_ok=True)

    requests: list[dict[str, Any]] = []
    dossiers: list[dict[str, Any]] = []
    replay_metadata = dict(replay_executor_metadata or {"executor": "blocked"})
    stage_external_wait: dict[str, Any] | None = None
    selected_problem_ids_ordered: list[str] = []
    selected_case_ids: dict[str, str] = {}
    for problem_index, problem in enumerate(selected_problems, start=1):
        if not isinstance(problem, dict):
            raise ValueError(
                f"run_repro_research_stage: selected_problems[{problem_index}] invalid"
            )
        problem_id = _coerce_str(problem.get("problem_id"))
        if problem_id is None:
            raise ValueError(
                f"run_repro_research_stage: selected_problems[{problem_index}] missing problem_id"
            )
        if problem_id in selected_case_ids:
            raise ValueError(
                f"run_repro_research_stage: duplicate selected problem_id:{problem_id}"
            )
        selected_problem_ids_ordered.append(problem_id)
        selected_case_ids[problem_id] = _coerce_str(problem.get("case_id")) or "case:unassigned"
    resume_checkpoint, resume_items_by_problem_id = _resume_checkpoint_from_stage_document(
        resume_stage_document,
        selected_problem_ids=selected_problem_ids_ordered,
        selected_case_ids=selected_case_ids,
    )
    resume_trigger_problem_id = (
        _coerce_str(resume_checkpoint.get("trigger_problem_id"))
        if resume_checkpoint is not None
        else None
    )
    resume_trigger_cleared = resume_checkpoint is None
    selected_problem_ids = set(selected_problem_ids_ordered)
    if resume_checkpoint is not None and resume_trigger_problem_id not in selected_problem_ids:
        raise ValueError("research_external_wait_resume_trigger_not_selected")
    if resume_checkpoint is not None:
        # Validate the entire retained frontier before resuming its trigger.  Checking
        # assignments only as the dispatch loop reaches each case could resume the model before
        # discovering that a later parked case or earlier completed case had changed.
        for problem in selected_problems:
            problem_id = str(problem["problem_id"])
            persisted_dossier = resume_items_by_problem_id[problem_id]
            persisted_assignment_raw = persisted_dossier.get("evidence_assignment")
            persisted_assignment = (
                persisted_assignment_raw if isinstance(persisted_assignment_raw, dict) else {}
            )
            current_assignment_raw = problem.get("evidence_assignment")
            current_assignment = (
                current_assignment_raw if isinstance(current_assignment_raw, dict) else {}
            )
            if persisted_assignment.get("assignment_sha256") != current_assignment.get(
                "assignment_sha256"
            ):
                raise ValueError(
                    "research_external_wait_resume_evidence_assignment_changed:" + problem_id
                )
            if _coerce_str(persisted_dossier.get("case_id")) != selected_case_ids[problem_id]:
                raise ValueError(
                    "research_external_wait_resume_case_assignment_changed:" + problem_id
                )

    if not dry_run and selected_problems and (repo_input is None or not str(repo_input).strip()):
        raise ValueError(
            "run_repro_research_stage: repo_input is required when dry_run=false. "
            "Provide --repo-input or ensure the caller inferred a single repo_input."
        )
    requested_repo_ref = _coerce_str(repo_ref)
    if not dry_run and selected_problems and requested_repo_ref is None:
        raise ValueError(
            "run_repro_research_stage: repo_ref is required when dry_run=false; "
            "configure backlog_research.source_ref or pass --research-ref"
        )
    resolved_repo_ref = (
        _resolve_repo_ref(str(repo_input), requested_repo_ref)
        if not dry_run
        and selected_problems
        and repo_input is not None
        and requested_repo_ref is not None
        else requested_repo_ref
    )
    compatibility_contract = stage3_research_compatibility_contract(agent=agent)
    completed_prefix = _resume_completed_prefix_from_stage_document(
        resume_stage_document,
        selected_problems=selected_problems,
        resolved_repo_ref=resolved_repo_ref,
        expected_compatibility_contract=compatibility_contract,
    )
    if completed_prefix and resume_checkpoint is not None:
        raise ValueError("research_resume_checkpoint_modes_conflict")
    dossiers.extend(completed_prefix)
    resumed_progress_checkpoint_sha256 = None
    if completed_prefix and isinstance(resume_stage_document, Mapping):
        resume_meta_raw = resume_stage_document.get("input_meta")
        resume_meta = resume_meta_raw if isinstance(resume_meta_raw, Mapping) else {}
        progress_raw = resume_meta.get("progress_checkpoint")
        progress = progress_raw if isinstance(progress_raw, Mapping) else {}
        resumed_progress_checkpoint_sha256 = _coerce_str(progress.get("checkpoint_sha256"))

    def commit_dossier(dossier: dict[str, Any]) -> None:
        index = len(dossiers)
        if index >= len(selected_problem_ids_ordered):
            raise ValueError("research_progress_commit_exceeds_selection")
        expected_problem_id = selected_problem_ids_ordered[index]
        if (
            _coerce_str(dossier.get("problem_id")) != expected_problem_id
            or _coerce_str(dossier.get("case_id")) != selected_case_ids[expected_problem_id]
        ):
            raise ValueError("research_progress_commit_identity_mismatch:" + expected_problem_id)
        dossiers.append(dossier)
        if progress_callback is None or stage_external_wait is not None:
            return
        checkpoint = _completed_prefix_checkpoint(
            selected_problems=selected_problems,
            completed_dossiers=dossiers,
            resolved_repo_ref=resolved_repo_ref,
            compatibility_contract=compatibility_contract,
        )
        progress_callback(
            build_stage_document(
                _STAGE,
                dossiers,
                input_meta={
                    "selected_problem_count": len(selected_problems),
                    "stage_status": "checkpointed_progress",
                    "progress_checkpoint": checkpoint,
                    "research_compatibility": compatibility_contract,
                    "resumed_progress_checkpoint_sha256": (resumed_progress_checkpoint_sha256),
                    "repo_input": repo_input,
                    "target_slug": target_slug,
                    "agent": agent,
                    "model": model,
                },
                artifacts={},
            )
        )

    for idx, problem in enumerate(selected_problems, start=1):
        pid = _coerce_str(problem.get("problem_id"))
        if pid is None:
            raise ValueError(
                f"run_repro_research_stage: selected_problems[{idx}] missing problem_id"
            )
        case_id = _coerce_str(problem.get("case_id")) or "case:unassigned"
        assignment_raw = problem.get("evidence_assignment")
        evidence_assignment = (
            dict(assignment_raw)
            if isinstance(assignment_raw, dict)
            else {
                "status": "incomplete",
                "errors": ["origin_evidence_assignment_missing"],
                "case_id": case_id,
                "problem_id": pid,
                "expected_atom_ids": [],
                "atom_receipts": [],
            }
        )

        seed = (
            _stable_seed(pid)
            if _attempt_number == 1
            else _stable_seed(f"{pid}:research_attempt:{_attempt_number}")
        )
        evidence_atoms_raw = problem.get("evidence_atoms")
        evidence_atoms = evidence_atoms_raw if isinstance(evidence_atoms_raw, list) else []
        evidence_atom_ids = [
            atom_id
            for atom in evidence_atoms
            if isinstance(atom, dict)
            for atom_id in [_coerce_str(atom.get("atom_id"))]
            if atom_id is not None
        ]
        if not isinstance(assignment_raw, dict):
            evidence_assignment["expected_atom_ids"] = list(evidence_atom_ids)
            evidence_assignment["assignment_sha256"] = evidence_assignment_sha256(
                evidence_assignment
            )
        req_meta = {
            "problem_id": pid,
            "agent": agent,
            "model": model,
            "policy": _POLICY,
            "persona_id": _PERSONA_ID,
            "mission_id": _MISSION_ID,
            "seed": seed,
            "repo_input": repo_input,
            "requested_repo_ref": requested_repo_ref,
            "resolved_repo_ref": resolved_repo_ref,
            "evidence_atom_count": len(evidence_atoms),
            "evidence_atom_ids": evidence_atom_ids,
        }
        requests.append(req_meta)

        if idx <= len(completed_prefix):
            req_meta.update(
                {
                    "dispatch_status": "reused_completed_prefix",
                    "progress_checkpoint_sha256": resumed_progress_checkpoint_sha256,
                }
            )
            continue

        if stage_external_wait is not None:
            checkpoint_sha256 = str(stage_external_wait["checkpoint_sha256"])
            req_meta.update(
                {
                    "dispatch_status": "parked_not_started",
                    "external_wait_checkpoint_sha256": checkpoint_sha256,
                    "blocked_by_problem_id": stage_external_wait["trigger_problem_id"],
                    "route": "chatgpt_subscription",
                    "api_fallback_allowed": False,
                }
            )
            parked = _parked_before_dispatch_dossier(
                case_id=case_id,
                problem_id=pid,
                evidence_assignment=evidence_assignment,
                evidence_atom_ids=evidence_atom_ids,
                requested_repo_ref=requested_repo_ref,
                resolved_repo_ref=resolved_repo_ref,
                checkpoint=stage_external_wait,
            )
            validated, _ = parse_research_dossier_list(json.dumps([parked]))
            commit_dossier(validated[0])
            continue

        persisted_dossier = resume_items_by_problem_id.get(pid)
        if persisted_dossier is not None:
            persisted_assignment_raw = persisted_dossier.get("evidence_assignment")
            persisted_assignment = (
                persisted_assignment_raw if isinstance(persisted_assignment_raw, dict) else {}
            )
            if persisted_assignment.get("assignment_sha256") != evidence_assignment.get(
                "assignment_sha256"
            ):
                raise ValueError(f"research_external_wait_resume_evidence_assignment_changed:{pid}")
        persisted_blockers = (
            _string_list(persisted_dossier.get("blocking_reasons"))
            if isinstance(persisted_dossier, dict)
            else []
        )
        persisted_was_parked_before_dispatch = any(
            reason.startswith("research_external_wait_stage_parked_before_dispatch:")
            for reason in persisted_blockers
        )
        if (
            resume_checkpoint is not None
            and pid != resume_trigger_problem_id
            and persisted_dossier is not None
            and not persisted_was_parked_before_dispatch
        ):
            persisted_validated, _ = parse_research_dossier_list(json.dumps([persisted_dossier]))
            commit_dossier(persisted_validated[0])
            req_meta.update(
                {
                    "dispatch_status": "retained_completed_before_external_wait",
                    "resume_checkpoint_sha256": resume_checkpoint["checkpoint_sha256"],
                }
            )
            continue

        if (
            resume_checkpoint is not None
            and not resume_trigger_cleared
            and pid == resume_trigger_problem_id
        ):
            if persisted_dossier is None:
                raise ValueError("research_external_wait_resume_trigger_dossier_missing")
            assert repo_input is not None
            resume_result = resume_research_dossier_from_external_wait(
                dossier=persisted_dossier,
                checkpoint=resume_checkpoint,
                repo_input=str(repo_input),
                requested_repo_ref=requested_repo_ref,
                resolved_repo_ref=resolved_repo_ref,
                agent=agent,
                model=model,
                cfg=cfg,
                replay_timeout_seconds=replay_timeout_seconds,
                replay_executor=replay_executor,
                artifacts_dir=stage_artifacts_dir / "external_wait_resume" / f"{idx:03d}_{seed}",
            )
            resumed_raw = resume_result.get("dossier")
            resumed = dict(resumed_raw) if isinstance(resumed_raw, dict) else persisted_dossier
            resumed_validated, _ = parse_research_dossier_list(json.dumps([resumed]))
            commit_dossier(resumed_validated[0])
            req_meta["attempts"] = [
                _research_attempt_request_summary(attempt)
                for attempt in (
                    resumed.get("research_attempts")
                    if isinstance(resumed.get("research_attempts"), list)
                    else []
                )
                if isinstance(attempt, dict)
            ]
            resume_status = str(resume_result.get("status") or "")
            if resume_status == "corrected":
                resume_trigger_cleared = True
                req_meta.update(
                    {
                        "dispatch_status": "resumed_same_author_after_provider_reset",
                        "resume_checkpoint_sha256": resume_checkpoint["checkpoint_sha256"],
                        "expected_session_id": resume_checkpoint.get("expected_session_id"),
                        "observed_session_id": resume_result.get("observed_session_id"),
                    }
                )
            elif resume_status == "parked_external_wait":
                resumed_wait = resume_result.get("external_wait")
                if not isinstance(resumed_wait, dict):
                    raise ValueError("research_external_wait_resume_repark_missing_attestation")
                stage_external_wait = _stage_external_wait_checkpoint(
                    external_wait=resumed_wait,
                    case_id=case_id,
                    problem_id=pid,
                    expected_session_id=_coerce_str(resume_result.get("expected_session_id")),
                    observed_session_id=_coerce_str(resume_result.get("observed_session_id")),
                )
                req_meta.update(
                    {
                        "dispatch_status": "reparked_during_same_session_resume",
                        "external_wait_checkpoint_sha256": stage_external_wait["checkpoint_sha256"],
                        "route": "chatgpt_subscription",
                        "api_fallback_allowed": False,
                    }
                )
            else:
                resume_trigger_cleared = True
                req_meta.update(
                    {
                        "dispatch_status": "resume_repairable_paused",
                        "resume_checkpoint_sha256": resume_checkpoint["checkpoint_sha256"],
                        "resume_status": resume_status,
                    }
                )
            continue

        missing_atom_ids = _string_list(problem.get("missing_evidence_atom_ids"))
        expected_atom_ids = _string_list(evidence_assignment.get("expected_atom_ids"))
        if expected_atom_ids != evidence_atom_ids:
            missing_atom_ids.append("assignment_atom_set_mismatch")
        if evidence_assignment.get("status") != "complete":
            assignment_errors = _string_list(evidence_assignment.get("errors"))
            missing_atom_ids.extend(assignment_errors or ["origin_evidence_assignment_incomplete"])
        missing_atom_ids = list(dict.fromkeys(missing_atom_ids))
        if missing_atom_ids:
            blocked = _blocked_research_placeholder(
                case_id=case_id,
                problem_id=pid,
                evidence_assignment=evidence_assignment,
                evidence_atom_ids=evidence_atom_ids,
                requested_repo_ref=requested_repo_ref,
                resolved_repo_ref=resolved_repo_ref,
                reason="origin_evidence_atoms_unresolved:" + ",".join(missing_atom_ids),
                unknown="One or more atoms cited by the canonical problem are unavailable",
                evidence_needed="Restore or explicitly disposition every cited atom",
            )
            validated, _ = parse_research_dossier_list(json.dumps([blocked]))
            commit_dossier(validated[0])
            continue

        problem_record_raw = problem.get("problem_record")
        problem_record = problem_record_raw if isinstance(problem_record_raw, dict) else {}
        if problem_record.get("case_identity_status") == "pending_relation":
            blocked = _blocked_research_placeholder(
                case_id=case_id,
                problem_id=pid,
                evidence_assignment=evidence_assignment,
                evidence_atom_ids=evidence_atom_ids,
                requested_repo_ref=requested_repo_ref,
                resolved_repo_ref=resolved_repo_ref,
                reason="canonical_case_identity_pending_relation_review",
                unknown=(
                    "The cited evidence resolves to multiple durable case identities and "
                    "relation review did not establish a merge, split, or provisional "
                    "same-cause research unit"
                ),
                evidence_needed=(
                    "Resolve the retained candidate case IDs with an evidence-citing "
                    "relation decision; do not mint a replacement case"
                ),
            )
            validated, _ = parse_research_dossier_list(json.dumps([blocked]))
            commit_dossier(validated[0])
            continue

        if dry_run:
            placeholder = _blocked_research_placeholder(
                case_id=case_id,
                problem_id=pid,
                evidence_assignment=evidence_assignment,
                evidence_atom_ids=evidence_atom_ids,
                requested_repo_ref=requested_repo_ref,
                resolved_repo_ref=resolved_repo_ref,
                reason="dry_run_research_not_executed",
                unknown="The failure mechanism was not investigated in dry-run mode",
                evidence_needed="Rerun stage 3 without --dry-run",
            )
            validated, _ = parse_research_dossier_list(json.dumps([placeholder]))
            commit_dossier(validated[0])
            continue

        prepared_workspace: Path | None = None
        origin_attachment_evidence: dict[str, Any] = {}
        problem_for_agent = dict(problem)
        if _has_origin_attachment_refs(evidence_atoms):
            assert repo_input is not None
            assert resolved_repo_ref is not None
            preferred_workspace = (
                stage_artifacts_dir / "research_workspaces" / f"{idx:03d}_{seed}_{uuid4().hex[:12]}"
            )
            prepared_workspace, origin_attachment_evidence = _prepare_origin_evidence_workspace(
                repo_input=str(repo_input),
                repo_ref=resolved_repo_ref,
                preferred_workspace_dir=preferred_workspace,
                evidence_atoms=[atom for atom in evidence_atoms if isinstance(atom, dict)],
                source_root=repo_root,
            )
            evidence_assignment["origin_attachment_evidence"] = origin_attachment_evidence
            materialization_errors_raw = origin_attachment_evidence.get("errors")
            materialization_errors = (
                materialization_errors_raw if isinstance(materialization_errors_raw, list) else []
            )
            if materialization_errors:
                evidence_assignment["status"] = "incomplete"
                existing_errors = _string_list(evidence_assignment.get("errors"))
                evidence_assignment["errors"] = list(
                    dict.fromkeys(
                        [
                            *existing_errors,
                            *[
                                "origin_attachment_materialization_failed:"
                                + str(error.get("atom_id") or "unknown")
                                + ":"
                                + str(error.get("error") or "unknown")
                                for error in materialization_errors
                                if isinstance(error, dict)
                            ],
                        ]
                    )
                )
            evidence_assignment["assignment_sha256"] = evidence_assignment_sha256(
                evidence_assignment
            )
            problem_for_agent["evidence_assignment"] = evidence_assignment
            problem_for_agent["origin_attachment_evidence"] = origin_attachment_evidence
            req_meta["origin_attachment_evidence"] = {
                "materialization_sha256": origin_attachment_evidence.get("materialization_sha256"),
                "artifact_count": len(origin_attachment_evidence.get("artifacts", [])),
                "error_count": len(materialization_errors),
                "workspace_dir": str(prepared_workspace),
            }
            if materialization_errors:
                blocked = _blocked_research_placeholder(
                    case_id=case_id,
                    problem_id=pid,
                    evidence_assignment=evidence_assignment,
                    evidence_atom_ids=evidence_atom_ids,
                    requested_repo_ref=requested_repo_ref,
                    resolved_repo_ref=resolved_repo_ref,
                    reason="origin_attachment_materialization_failed",
                    unknown=(
                        "The exact retained attachment could not be hash-verified "
                        "in the research workspace"
                    ),
                    evidence_needed=(
                        "Restore the declared attachment bytes or explicitly mark "
                        "the origin evidence integrity unknown"
                    ),
                )
                validated, _ = parse_research_dossier_list(json.dumps([blocked]))
                commit_dossier(validated[0])
                continue

        append_prompt = _append_prompt_for_problem(
            repo_root=repo_root,
            problem_payload=problem_for_agent,
        )
        request = RunRequest(
            repo=str(repo_input),
            ref=resolved_repo_ref,
            agent=str(agent),
            policy=_POLICY,
            persona_id=_PERSONA_ID,
            mission_id=_MISSION_ID,
            evidence_role="research",
            origin_stage="repro_research",
            parent_case_id=case_id,
            seed=seed,
            model=model,
            agent_append_system_prompt=append_prompt,
            keep_workspace=True,
            resume_workspace_dir=prepared_workspace,
            codex_execpolicy_allow_prefixes=(
                _CODEX_RESEARCH_EXEC_ALLOW_PREFIXES if agent == "codex" else ()
            ),
        )

        _LOG.info(
            "stage3: run_once problem_id=%s agent=%s policy=%s mission=%s persona=%s seed=%d",
            pid,
            agent,
            _POLICY,
            _MISSION_ID,
            _PERSONA_ID,
            seed,
        )
        try:
            result = run_once(config=cfg, request=request)
        except Exception as exc:  # noqa: BLE001
            reason = f"research_runner_exception:{type(exc).__name__}:{exc}"
            blocked = _blocked_research_after_run_failure(
                case_id=case_id,
                problem_id=pid,
                evidence_assignment=evidence_assignment,
                evidence_atom_ids=evidence_atom_ids,
                requested_repo_ref=requested_repo_ref,
                resolved_repo_ref=resolved_repo_ref,
                run_dir=None,
                reason=reason,
                unknown="The case-local research runner failed before producing evidence",
                evidence_needed="Retry this case and retain a valid research report",
            )
            validated, _ = parse_research_dossier_list(json.dumps([blocked]))
            commit_dossier(validated[0])
            continue
        run_dir = result.run_dir
        run_external_wait = _runner_external_wait(run_dir)
        _write_evidence_assignment_sidecar(
            run_dir,
            evidence_assignment=evidence_assignment,
        )

        report_path = run_dir / "report.json"
        report_obj: dict[str, Any] = {}
        report_loaded = False
        ext_block_raw: dict[str, Any] = {}
        output_contract_errors: list[str] = []
        if not report_path.is_file():
            output_contract_errors.append("research_report_missing")
        else:
            try:
                report_obj = _load_json_object(report_path)
                report_loaded = True
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                output_contract_errors.append(
                    f"research_report_malformed:{type(exc).__name__}:{exc}"
                )

        if report_loaded:
            output_contract_errors.extend(
                f"research_report_schema_invalid:{error}"
                for error in _model_report_schema_errors(
                    run_dir=run_dir,
                    report=report_obj,
                )
            )
            ext_raw = report_obj.get("extensions")
            ext_map = ext_raw if isinstance(ext_raw, dict) else {}
            ext_candidate = ext_map.get(_EXTENSION_KEY)
            if isinstance(ext_candidate, dict):
                ext_block_raw = dict(ext_candidate)
            else:
                output_contract_errors.append(f"research_extension_missing:{_EXTENSION_KEY}")

        ext_pid = _coerce_str(ext_block_raw.get("problem_id"))
        ext_case_id = _coerce_str(ext_block_raw.get("case_id"))
        if ext_block_raw:
            if ext_pid != pid:
                output_contract_errors.append(
                    f"research_dossier_problem_id_mismatch:expected={pid}:actual={ext_pid}"
                )
            if ext_case_id != case_id:
                output_contract_errors.append(
                    f"research_dossier_case_id_mismatch:expected={case_id}:actual={ext_case_id}"
                )
            if ext_block_raw.get("implementation_performed") is not True:
                output_contract_errors.extend(
                    research_dossier_output_contract_errors(
                        {
                            key: value
                            for key, value in ext_block_raw.items()
                            if key not in _RUNNER_OWNED_DOSSIER_FIELDS
                        },
                        evidence_assignment=evidence_assignment,
                    )
                )

        writes_purpose = _string_list(ext_block_raw.get("writes_purpose"))
        diff_numstat_path = run_dir / "diff_numstat.json"
        diff_numstat = _load_diff_numstat(diff_numstat_path)
        modified_paths = [
            path
            for entry in diff_numstat
            for path in [_coerce_str(entry.get("path"))]
            if path is not None
        ]
        diff_class, diff_reasons = _classify_diff(
            modified_paths,
            writes_purpose=writes_purpose,
        )

        if (
            ext_block_raw.get("implementation_performed") is True
            and diff_class == "suspicious_implementation"
        ):
            implementation_error = "research_implementation_performed_forbidden"
            implementation_attempt = _research_attempt_record(
                attempt_number=_attempt_number,
                outcome="runner_contract_invalid",
                run_dir=run_dir,
                report_path=report_path,
                validation_errors=[implementation_error, *output_contract_errors],
                attempted_dossier=ext_block_raw,
                attempt_kind=_full_attempt_kind,
                source_attempt_sha256=_full_source_attempt_sha256,
                baseline_dossier_sha256=_full_baseline_dossier_sha256,
                baseline_projection_sha256=_full_baseline_projection_sha256,
                validation_errors_before=_full_validation_errors_before,
                agent_session_id=result.agent_session_id,
                observed_agent_session_id=result.agent_session_id,
                attempt_wall_seconds=_run_wall_seconds(run_dir),
                repair_progress=_full_restart_provenance,
            )
            blocked = _blocked_research_after_run_failure(
                case_id=case_id,
                problem_id=pid,
                evidence_assignment=evidence_assignment,
                evidence_atom_ids=evidence_atom_ids,
                requested_repo_ref=requested_repo_ref,
                resolved_repo_ref=resolved_repo_ref,
                run_dir=run_dir,
                reason=implementation_error,
                unknown="The case-local run changed production code instead of researching",
                evidence_needed="Retry only this case in research-only mode",
            )
            implementation_history = [*map(dict, _prior_attempts), implementation_attempt]
            _set_research_attempts(blocked, implementation_history)
            validated, _ = parse_research_dossier_list(json.dumps([blocked]))
            commit_dossier(validated[0])
            req_meta["attempts"] = [
                _research_attempt_request_summary(attempt) for attempt in implementation_history
            ]
            continue

        if ext_block_raw.get("implementation_performed") is True:
            output_contract_errors.append(
                "research_implementation_performed_flag_contradicts_clean_diff"
            )

        nonretry_reason: str | None = None
        if result.exit_code != 0:
            nonretry_reason = (
                "research_external_wait_parked"
                if run_external_wait is not None
                else f"runner_exit_code:{result.exit_code}"
            )
        elif diff_class == "suspicious_implementation":
            nonretry_reason = "suspicious_implementation_diff"
        if nonretry_reason is not None:
            attempt_progress = _full_restart_provenance
            if run_external_wait is not None:
                if _full_attempt_kind == "fresh_research_retry" and isinstance(
                    _full_restart_provenance, dict
                ):
                    attempt_progress = dict(_full_restart_provenance)
                    attempt_progress.pop("provenance_sha256", None)
                    attempt_progress["external_wait"] = run_external_wait
                    attempt_progress["provenance_sha256"] = _canonical_json_sha256(attempt_progress)
                else:
                    attempt_progress = {
                        "decision": "parked",
                        "reason": "codex_chatgpt_subscription_usage_limit",
                        "external_wait": run_external_wait,
                        "authored_work_disposition": "retained",
                    }
            nonretry_attempt = _research_attempt_record(
                attempt_number=_attempt_number,
                outcome=(
                    "external_wait" if run_external_wait is not None else "runner_contract_invalid"
                ),
                run_dir=run_dir,
                report_path=report_path,
                validation_errors=[nonretry_reason, *output_contract_errors],
                attempted_dossier=ext_block_raw,
                attempt_kind=_full_attempt_kind,
                source_attempt_sha256=_full_source_attempt_sha256,
                baseline_dossier_sha256=_full_baseline_dossier_sha256,
                baseline_projection_sha256=_full_baseline_projection_sha256,
                validation_errors_before=_full_validation_errors_before,
                agent_session_id=result.agent_session_id,
                observed_agent_session_id=result.agent_session_id,
                attempt_wall_seconds=_run_wall_seconds(run_dir),
                repair_progress=attempt_progress,
            )
            blocked = _blocked_research_after_run_failure(
                case_id=case_id,
                problem_id=pid,
                evidence_assignment=evidence_assignment,
                evidence_atom_ids=evidence_atom_ids,
                requested_repo_ref=requested_repo_ref,
                resolved_repo_ref=resolved_repo_ref,
                run_dir=run_dir,
                reason=nonretry_reason,
                unknown=(
                    "Research is waiting for the ChatGPT subscription reset"
                    if run_external_wait is not None
                    else "The research run failed a non-retryable execution integrity gate"
                ),
                evidence_needed=(
                    "Resume the retained workflow after the recorded provider reset"
                    if run_external_wait is not None
                    else (
                        "Inspect the retained run and start a new research cycle only after "
                        "the execution or prohibited-diff failure is resolved"
                    )
                ),
            )
            blocked["diff_classification"] = diff_class
            if diff_reasons:
                blocked["diff_suspicious_reasons"] = diff_reasons
            nonretry_history = [*map(dict, _prior_attempts), nonretry_attempt]
            _set_research_attempts(blocked, nonretry_history)
            validated, _ = parse_research_dossier_list(json.dumps([blocked]))
            commit_dossier(validated[0])
            req_meta["attempts"] = [
                _research_attempt_request_summary(attempt) for attempt in nonretry_history
            ]
            if run_external_wait is not None:
                stage_external_wait = _stage_external_wait_checkpoint(
                    external_wait=run_external_wait,
                    case_id=case_id,
                    problem_id=pid,
                    expected_session_id=result.agent_session_id,
                    observed_session_id=result.agent_session_id,
                )
                req_meta.update(
                    {
                        "dispatch_status": "parked_during_dispatch",
                        "external_wait_checkpoint_sha256": stage_external_wait["checkpoint_sha256"],
                        "route": "chatgpt_subscription",
                        "api_fallback_allowed": False,
                    }
                )
            continue
        current_attempt = _research_attempt_record(
            attempt_number=_attempt_number,
            outcome=(
                "output_contract_invalid" if output_contract_errors else "output_contract_valid"
            ),
            run_dir=run_dir,
            report_path=report_path,
            validation_errors=output_contract_errors,
            attempted_dossier=ext_block_raw,
            attempt_kind=_full_attempt_kind,
            source_attempt_sha256=_full_source_attempt_sha256,
            baseline_dossier_sha256=_full_baseline_dossier_sha256,
            baseline_projection_sha256=_full_baseline_projection_sha256,
            validation_errors_before=_full_validation_errors_before,
            agent_session_id=result.agent_session_id,
            observed_agent_session_id=result.agent_session_id,
            attempt_wall_seconds=_run_wall_seconds(run_dir),
            repair_progress=_full_restart_provenance,
        )
        research_attempt_history = [*map(dict, _prior_attempts), current_attempt]
        retry_source_attempt = current_attempt
        full_restart_authorized = False
        restart_frontiers: dict[str, Any] | None = None
        fresh_restart_assessment: dict[str, Any] | None = None
        correction_block_reason: str | None = None
        if output_contract_errors:
            current_revision = _research_attempt_revision(current_attempt)
            if current_revision is not None:
                repair_result = _run_targeted_dossier_repairs(
                    repo_input=str(repo_input),
                    repo_revision=current_revision,
                    agent=agent,
                    model=model,
                    cfg=cfg,
                    case_id=case_id,
                    problem_id=pid,
                    evidence_assignment=evidence_assignment,
                    source_attempt=current_attempt,
                    validation_errors=output_contract_errors,
                    first_attempt_number=_attempt_number + 1,
                )
                repair_attempts = [
                    attempt
                    for attempt in repair_result.get("attempts", [])
                    if isinstance(attempt, dict)
                ]
                research_attempt_history.extend(repair_attempts)
                req_meta["targeted_dossier_repairs"] = {
                    "status": repair_result.get("status"),
                    "attempt_count": len(repair_attempts),
                    "repair_run_dirs": repair_result.get("repair_run_dirs", []),
                    "expected_session_id": repair_result.get("expected_session_id"),
                    "observed_session_id": repair_result.get("observed_session_id"),
                    "continuation_failure": repair_result.get("continuation_failure"),
                }
                repair_status = str(repair_result.get("status") or "")
                if repair_status == "parked_external_wait":
                    correction_block_reason = "research_external_wait_parked"
                    req_meta["targeted_dossier_repairs"]["external_wait"] = repair_result.get(
                        "external_wait"
                    )
                    repair_external_wait = repair_result.get("external_wait")
                    if isinstance(repair_external_wait, dict):
                        stage_external_wait = _stage_external_wait_checkpoint(
                            external_wait=repair_external_wait,
                            case_id=case_id,
                            problem_id=pid,
                            expected_session_id=_coerce_str(
                                repair_result.get("expected_session_id")
                            ),
                            observed_session_id=_coerce_str(
                                repair_result.get("observed_session_id")
                            ),
                        )
                        req_meta.update(
                            {
                                "dispatch_status": "parked_during_same_session_repair",
                                "external_wait_checkpoint_sha256": stage_external_wait[
                                    "checkpoint_sha256"
                                ],
                                "route": "chatgpt_subscription",
                                "api_fallback_allowed": False,
                            }
                        )
                continuation_unavailable = repair_status in {
                    "same_session_continuation_unavailable",
                    "workspace_unavailable",
                }
                repaired_raw = repair_result.get("dossier")
                repaired = dict(repaired_raw) if isinstance(repaired_raw, dict) else {}
                remaining_errors = _string_list(repair_result.get("validation_errors"))
                repaired_hash = _canonical_json_sha256(repaired)
                retry_source_attempt = next(
                    (
                        attempt
                        for attempt in reversed(research_attempt_history)
                        if attempt.get("outcome") != "repair_scope_rejected"
                        and attempt.get("attempted_dossier_sha256") == repaired_hash
                        and _string_list(attempt.get("validation_errors")) == remaining_errors
                    ),
                    current_attempt,
                )
                best_source_sha = _coerce_str(repair_result.get("best_source_attempt_sha256"))
                best_source_attempt = next(
                    (
                        attempt
                        for attempt in reversed(research_attempt_history)
                        if attempt.get("attempt_sha256") == best_source_sha
                    ),
                    current_attempt,
                )
                if repair_status.startswith("restart:") or continuation_unavailable:
                    fresh_restart_assessment = _fresh_restart_progress_assessment(
                        full_attempt_kind=_full_attempt_kind,
                        prior_attempts=[
                            attempt for attempt in _prior_attempts if isinstance(attempt, dict)
                        ],
                        current_cycle_attempts=research_attempt_history[len(_prior_attempts) :],
                        current_best_attempt=best_source_attempt,
                        repair_status=repair_status,
                    )
                    full_restart_authorized = fresh_restart_assessment.get("decision") == "restart"
                    req_meta["targeted_dossier_repairs"]["fresh_restart_assessment"] = (
                        fresh_restart_assessment
                    )
                    if continuation_unavailable:
                        fresh_restart_assessment["continuation_unavailable"] = True
                        fresh_restart_assessment["expected_session_id"] = repair_result.get(
                            "expected_session_id"
                        )
                        fresh_restart_assessment["observed_session_id"] = repair_result.get(
                            "observed_session_id"
                        )
                        fresh_restart_assessment["continuation_failure"] = repair_result.get(
                            "continuation_failure"
                        )
                        _record_terminal_continuation_unavailable(
                            current_attempt,
                            repair_result=repair_result,
                            assessment=fresh_restart_assessment,
                        )
                        # Continuation-unavailable returns no repair attempts, so the current full
                        # attempt is both the preserved forward and objective-best frontier.
                        retry_source_attempt = current_attempt
                        best_source_attempt = current_attempt
                    if not full_restart_authorized:
                        correction_block_reason = (
                            "research_dossier_repairable_paused:"
                            + str(fresh_restart_assessment.get("reason"))
                            + ":trigger="
                            + repair_status
                        )
                restart_frontiers = _research_correction_frontiers(
                    repair_status=repair_status,
                    latest_safe_attempt=retry_source_attempt,
                    best_count_attempt=best_source_attempt,
                    attempt_history=research_attempt_history,
                )
                if repair_result.get("status") == "corrected" and not remaining_errors:
                    ext_block_raw = repaired
                    ext_pid = _coerce_str(ext_block_raw.get("problem_id"))
                    ext_case_id = _coerce_str(ext_block_raw.get("case_id"))
                    output_contract_errors = []
                else:
                    output_contract_errors = remaining_errors or list(output_contract_errors)

        if output_contract_errors:
            if full_restart_authorized:
                retry_problem = dict(problem)
                prior_attempt_projection = _research_retry_prior_attempt_projection(
                    retry_source_attempt
                )
                fresh_restart_provenance: dict[str, Any] = {
                    "schema_version": 1,
                    "decision": "fresh_investigation",
                    "reason": (
                        fresh_restart_assessment.get("reason")
                        if isinstance(fresh_restart_assessment, dict)
                        else "recorded_restart_need"
                    ),
                    "trigger_status": (
                        fresh_restart_assessment.get("restart_trigger")
                        if isinstance(fresh_restart_assessment, dict)
                        else None
                    ),
                    "source_attempt_sha256": retry_source_attempt["attempt_sha256"],
                    "source_projection_sha256": prior_attempt_projection["projection_sha256"],
                    "correction_frontiers_sha256": (
                        restart_frontiers.get("frontiers_sha256")
                        if isinstance(restart_frontiers, dict)
                        else None
                    ),
                    "expected_session_id": (
                        fresh_restart_assessment.get("expected_session_id")
                        if isinstance(fresh_restart_assessment, dict)
                        else None
                    ),
                    "observed_session_id": (
                        fresh_restart_assessment.get("observed_session_id")
                        if isinstance(fresh_restart_assessment, dict)
                        else None
                    ),
                    "continuation_failure": (
                        fresh_restart_assessment.get("continuation_failure")
                        if isinstance(fresh_restart_assessment, dict)
                        else None
                    ),
                }
                fresh_restart_provenance["provenance_sha256"] = _canonical_json_sha256(
                    fresh_restart_provenance
                )
                fresh_attempt_number = len(research_attempt_history) + 1
                retry_problem["research_output_contract_retry"] = {
                    "attempt_number": fresh_attempt_number,
                    "prior_run_dir": str(run_dir.resolve()),
                    "prior_attempt_sha256": retry_source_attempt["attempt_sha256"],
                    "prior_attempt_projection": prior_attempt_projection,
                    "prior_attempt_projection_sha256": prior_attempt_projection[
                        "projection_sha256"
                    ],
                    "validation_errors": list(output_contract_errors),
                    "remediation_hints": _research_retry_remediation_hints(output_contract_errors),
                    "correction_frontiers": restart_frontiers,
                    "fresh_restart_assessment": fresh_restart_assessment,
                    "instruction": (
                        "Rerun the complete research assignment in the newly acquired "
                        "workspace. Re-read the assigned evidence and repository files, "
                        "re-execute every claimed experiment, and emit a fresh complete "
                        "dossier that satisfies these exact output-contract errors. The "
                        "content-addressed prior projection is a preservation aid, not evidence: "
                        "preserve stronger research only after reverifying it in this fresh "
                        "workspace at the same pinned revision. Do not mechanically rewrite the "
                        "prior JSON, add unrun experiments, or weaken an evidence status."
                    ),
                }
                retry_artifacts_root = (
                    stage_artifacts_dir
                    / "output_contract_retries"
                    / f"{idx:03d}_{seed}_{uuid4().hex[:12]}"
                )
                retry_doc = run_repro_research_stage(
                    repo_root=repo_root,
                    repo_input=repo_input,
                    repo_ref=resolved_repo_ref,
                    target_slug=target_slug,
                    selected_problems=[retry_problem],
                    artifacts_dir=retry_artifacts_root,
                    agent=agent,
                    model=model,
                    cfg=cfg,
                    dry_run=False,
                    replay_timeout_seconds=replay_timeout_seconds,
                    replay_executor=replay_executor,
                    replay_executor_metadata=replay_metadata,
                    _attempt_number=fresh_attempt_number,
                    _full_attempt_kind="fresh_research_retry",
                    _full_source_attempt_sha256=retry_source_attempt["attempt_sha256"],
                    _full_baseline_dossier_sha256=retry_source_attempt["attempted_dossier_sha256"],
                    _full_baseline_projection_sha256=prior_attempt_projection["projection_sha256"],
                    _full_validation_errors_before=output_contract_errors,
                    _full_restart_provenance=fresh_restart_provenance,
                    _prior_attempts=research_attempt_history,
                )
                retry_meta_raw = retry_doc.get("input_meta")
                retry_meta = retry_meta_raw if isinstance(retry_meta_raw, dict) else {}
                retry_external_wait = retry_meta.get("external_wait")
                if isinstance(retry_external_wait, dict):
                    stage_external_wait = json.loads(
                        json.dumps(retry_external_wait, ensure_ascii=False)
                    )
                    req_meta.update(
                        {
                            "dispatch_status": "parked_during_fresh_research_retry",
                            "external_wait_checkpoint_sha256": stage_external_wait.get(
                                "checkpoint_sha256"
                            ),
                            "route": "chatgpt_subscription",
                            "api_fallback_allowed": False,
                        }
                    )
                retry_items_raw = retry_doc.get("items")
                retry_items = retry_items_raw if isinstance(retry_items_raw, list) else []
                if retry_items and isinstance(retry_items[0], dict):
                    retried = dict(retry_items[0])
                else:
                    retried = _blocked_research_after_run_failure(
                        case_id=case_id,
                        problem_id=pid,
                        evidence_assignment=evidence_assignment,
                        evidence_atom_ids=evidence_atom_ids,
                        requested_repo_ref=requested_repo_ref,
                        resolved_repo_ref=resolved_repo_ref,
                        run_dir=None,
                        reason="research_output_contract_retry_result_missing",
                        unknown="The bounded output-contract retry returned no dossier",
                        evidence_needed="Retain and inspect the case-local retry artifacts",
                    )
                retry_attempts_raw = retried.get("research_attempts")
                retry_attempts = (
                    [attempt for attempt in retry_attempts_raw if isinstance(attempt, dict)]
                    if isinstance(retry_attempts_raw, list)
                    else []
                )
                new_retry_attempts = retry_attempts[len(research_attempt_history) :]
                if not new_retry_attempts:
                    invocation_failure = _research_invocation_failure_record(
                        attempt_number=fresh_attempt_number,
                        validation_errors=_string_list(retried.get("blocking_reasons"))
                        or ["research_output_contract_retry_failed"],
                        attempt_kind="fresh_research_retry",
                        source_attempt_sha256=retry_source_attempt["attempt_sha256"],
                        baseline_dossier_sha256=retry_source_attempt["attempted_dossier_sha256"],
                        baseline_projection_sha256=prior_attempt_projection["projection_sha256"],
                        validation_errors_before=output_contract_errors,
                        repair_progress=fresh_restart_provenance,
                    )
                    retry_attempts = [*research_attempt_history, invocation_failure]
                    new_retry_attempts = [invocation_failure]
                current_workspace = _research_attempt_workspace(current_attempt)
                retry_workspaces = {
                    workspace
                    for attempt in new_retry_attempts
                    for workspace in [_research_attempt_workspace(attempt)]
                    if workspace is not None
                }
                current_run_dir = str(run_dir.resolve())
                retry_run_dirs = {
                    str(attempt.get("run_dir"))
                    for attempt in new_retry_attempts
                    if _coerce_str(attempt.get("run_dir")) is not None
                }
                current_revision = _research_attempt_revision(current_attempt)
                retry_revisions = {
                    revision
                    for attempt in new_retry_attempts
                    for revision in [_research_attempt_revision(attempt)]
                    if revision is not None
                }
                successful_retry = retried.get("research_status") != "blocked"
                freshness_unverifiable = successful_retry and (
                    current_workspace is None
                    or not retry_workspaces
                    or current_revision is None
                    or not retry_revisions
                )
                freshness_reused = current_run_dir in retry_run_dirs or (
                    current_workspace is not None and current_workspace in retry_workspaces
                )
                revision_changed = (
                    current_revision is not None
                    and bool(retry_revisions)
                    and retry_revisions != {current_revision}
                )
                if freshness_unverifiable or freshness_reused or revision_changed:
                    freshness_reason = (
                        "research_output_contract_retry_freshness_unverifiable"
                        if freshness_unverifiable
                        else "research_output_contract_retry_not_fresh"
                        if freshness_reused
                        else "research_output_contract_retry_revision_changed"
                    )
                    retry_run_dir_raw = _coerce_str(new_retry_attempts[0].get("run_dir"))
                    retried = _blocked_research_after_run_failure(
                        case_id=case_id,
                        problem_id=pid,
                        evidence_assignment=evidence_assignment,
                        evidence_atom_ids=evidence_atom_ids,
                        requested_repo_ref=requested_repo_ref,
                        resolved_repo_ref=resolved_repo_ref,
                        run_dir=(
                            Path(retry_run_dir_raw) if retry_run_dir_raw is not None else None
                        ),
                        reason=freshness_reason,
                        unknown=(
                            "The bounded output-contract retry reused the prior run or workspace"
                        ),
                        evidence_needed=(
                            "Acquire a distinct workspace and rerun the full case from "
                            "assigned evidence"
                        ),
                    )
                _set_research_attempts(retried, retry_attempts)
                retried_validated, _ = parse_research_dossier_list(json.dumps([retried]))
                commit_dossier(retried_validated[0])
                req_meta["attempts"] = [
                    _research_attempt_request_summary(attempt) for attempt in retry_attempts
                ]
                req_meta["output_contract_retry_artifacts"] = retry_doc.get("artifacts", {})
                continue

            blocked = _blocked_research_after_run_failure(
                case_id=case_id,
                problem_id=pid,
                evidence_assignment=evidence_assignment,
                evidence_atom_ids=evidence_atom_ids,
                requested_repo_ref=requested_repo_ref,
                resolved_repo_ref=resolved_repo_ref,
                run_dir=run_dir,
                reason=correction_block_reason or "research_dossier_output_contract_invalid",
                unknown=(
                    "The retained author is waiting for the ChatGPT subscription reset"
                    if correction_block_reason == "research_external_wait_parked"
                    else "The author session or retained workspace required for correction was "
                    "unavailable"
                    if correction_block_reason is not None
                    else "The case-local dossier failed the model-output contract"
                ),
                evidence_needed=(
                    "Resume the same retained author session after the recorded provider reset"
                    if correction_block_reason == "research_external_wait_parked"
                    else "Inspect the retained validation errors and raw attempted dossier"
                ),
            )
            _set_research_attempts(blocked, research_attempt_history)
            validated, _ = parse_research_dossier_list(json.dumps([blocked]))
            commit_dossier(validated[0])
            req_meta["attempts"] = [
                _research_attempt_request_summary(attempt) for attempt in research_attempt_history
            ]
            continue

        dossier: dict[str, Any] = {
            key: value
            for key, value in ext_block_raw.items()
            if key not in _RUNNER_OWNED_DOSSIER_FIELDS
        }
        dossier["research_schema_version"] = RESEARCH_PROOF_SCHEMA_VERSION
        dossier["evidence_assignment"] = evidence_assignment
        repo_revision = _canonical_repo_revision(run_dir)
        blocking_reasons = _string_list(dossier.get("blocking_reasons"))
        if repo_revision is None:
            repo_revision = "unavailable"
            blocking_reasons.append("runner_repo_revision_unavailable")
        dossier["repo_revision"] = repo_revision
        dossier["diff_classification"] = diff_class
        if diff_reasons:
            dossier["diff_suspicious_reasons"] = diff_reasons
        dossier["run_dir"] = str(run_dir)
        dossier["runner_exit_code"] = int(result.exit_code)
        dossier["runner_report_validation_errors"] = list(result.report_validation_errors)
        dossier["artifacts"] = {
            "report_json": str(report_path),
            "report_md": str(run_dir / "report.md"),
            "patch_diff": str(run_dir / "patch.diff"),
            "diff_numstat_json": str(diff_numstat_path),
            "normalized_events_jsonl": str(run_dir / "normalized_events.jsonl"),
            "agent_stderr_txt": str(run_dir / "agent_stderr.txt"),
        }

        artifact_refs_raw = dossier.get("artifact_refs")
        artifact_refs = list(artifact_refs_raw) if isinstance(artifact_refs_raw, list) else []
        seen_artifact_paths = {
            str(ref.get("path"))
            for ref in artifact_refs
            if isinstance(ref, dict) and _coerce_str(ref.get("path")) is not None
        }
        for ref in _runner_artifact_refs(run_dir):
            if ref["path"] not in seen_artifact_paths:
                artifact_refs.append(ref)
        dossier["artifact_refs"] = artifact_refs
        dossier["research_attempts"] = research_attempt_history
        req_meta["attempts"] = [
            _research_attempt_request_summary(attempt) for attempt in research_attempt_history
        ]

        evidence_verification = verify_research_evidence(
            dossier,
            run_dir=run_dir,
            repo_revision=repo_revision,
            case_id=case_id,
            problem_id=pid,
            expected_case_id=ext_case_id,
            expected_problem_id=ext_pid,
            evidence_assignment=evidence_assignment,
            evidence_atom_ids=evidence_atom_ids,
            revision_view_destination=(
                stage_artifacts_dir
                / "revision_views"
                / sha256(f"{repo_input}\0{repo_revision}".encode()).hexdigest()[:16]
            ),
            replay_timeout_seconds=replay_timeout_seconds,
            requested_repo_ref=requested_repo_ref,
            resolved_repo_ref=resolved_repo_ref,
            replay_executor=replay_executor,
        )
        if origin_attachment_evidence and prepared_workspace is not None:
            attachment_reads, attachment_errors = _origin_attachment_read_receipts(
                run_dir=run_dir,
                workspace_dir=prepared_workspace,
                manifest=origin_attachment_evidence,
            )
            evidence_verification["origin_attachment_evidence"] = origin_attachment_evidence
            evidence_verification["origin_attachment_read_attestations"] = attachment_reads
            if attachment_errors:
                _fail_evidence_verification(
                    evidence_verification,
                    errors=attachment_errors,
                )
        dossier["evidence_verification"] = evidence_verification
        workspace_dir = evidence_verification.get("planning_workspace_dir")
        dossier["repo_workspace"] = workspace_dir if isinstance(workspace_dir, str) else None
        effective_result = result
        effective_report_obj = report_obj

        verification_errors = _string_list(evidence_verification.get("errors"))
        if (
            evidence_verification.get("status") != "verified"
            and verification_errors
            and agent == "codex"
            and _coerce_str(result.agent_session_id) is not None
        ):
            research_capabilities = _verifier_feedback_requires_research_tools(verification_errors)
            verified_candidates: dict[
                str,
                tuple[dict[str, Any], dict[str, Any], Path, Any, dict[str, Any]],
            ] = {}
            verifier_evidence_attempt_history = [
                dict(attempt) for attempt in research_attempt_history
            ]

            def validate_verifier_candidate(
                candidate: dict[str, Any],
                correction_result: Any,
                *,
                _research_capabilities: bool = research_capabilities,
                _original_run_dir: Path = run_dir,
                _evidence_assignment: dict[str, Any] = evidence_assignment,
                _repo_revision: str = repo_revision,
                _case_id: str = case_id,
                _problem_id: str = pid,
                _evidence_atom_ids: list[str] = evidence_atom_ids,
                _verified_candidates: dict[
                    str,
                    tuple[
                        dict[str, Any],
                        dict[str, Any],
                        Path,
                        Any,
                        dict[str, Any],
                    ],
                ] = verified_candidates,
                _origin_attachment_evidence: dict[str, Any] = origin_attachment_evidence,
                _prepared_workspace: Path | None = prepared_workspace,
                _evidence_attempt_history: list[dict[str, Any]] = verifier_evidence_attempt_history,
                _source_attempt: dict[str, Any] = current_attempt,
            ) -> Sequence[str]:
                verification_run_dir = (
                    correction_result.run_dir if _research_capabilities else _original_run_dir
                )
                prepared = {
                    key: value
                    for key, value in candidate.items()
                    if key not in _RUNNER_OWNED_DOSSIER_FIELDS
                }
                prepared["research_schema_version"] = RESEARCH_PROOF_SCHEMA_VERSION
                prepared["evidence_assignment"] = _evidence_assignment
                candidate_revision = _canonical_repo_revision(verification_run_dir)
                prepared["repo_revision"] = candidate_revision or _repo_revision
                candidate_diff_paths = [
                    path
                    for entry in _load_diff_numstat(verification_run_dir / "diff_numstat.json")
                    for path in [_coerce_str(entry.get("path"))]
                    if path is not None
                ]
                candidate_diff_class, candidate_diff_reasons = _classify_diff(
                    candidate_diff_paths,
                    writes_purpose=_string_list(prepared.get("writes_purpose")),
                )
                prepared["diff_classification"] = candidate_diff_class
                if candidate_diff_reasons:
                    prepared["diff_suspicious_reasons"] = candidate_diff_reasons
                prepared["run_dir"] = str(verification_run_dir)
                prepared["runner_exit_code"] = int(correction_result.exit_code)
                prepared["runner_report_validation_errors"] = list(
                    correction_result.report_validation_errors
                )
                candidate_refs_raw = prepared.get("artifact_refs")
                candidate_refs = (
                    list(candidate_refs_raw) if isinstance(candidate_refs_raw, list) else []
                )
                candidate_paths = {
                    str(ref.get("path"))
                    for ref in candidate_refs
                    if isinstance(ref, dict) and _coerce_str(ref.get("path")) is not None
                }
                for ref in _runner_artifact_refs(verification_run_dir):
                    if ref["path"] not in candidate_paths:
                        candidate_refs.append(ref)
                prepared["artifact_refs"] = candidate_refs
                retained_workspace = _research_attempt_workspace_path(_source_attempt)
                retained_session_id = _coerce_str(_source_attempt.get("agent_session_id"))
                evidence_attempts = (
                    _compatible_research_evidence_attempts(
                        _evidence_attempt_history,
                        case_id=_case_id,
                        problem_id=_problem_id,
                        repo_revision=prepared["repo_revision"],
                        agent_session_id=retained_session_id,
                        workspace=retained_workspace,
                        current_run_dir=verification_run_dir,
                    )
                    if _research_capabilities
                    and retained_workspace is not None
                    and retained_session_id is not None
                    else []
                )
                candidate_receipt = verify_research_evidence(
                    prepared,
                    run_dir=verification_run_dir,
                    evidence_attempts=evidence_attempts,
                    evidence_agent_session_id=retained_session_id,
                    repo_revision=prepared["repo_revision"],
                    case_id=_case_id,
                    problem_id=_problem_id,
                    expected_case_id=_coerce_str(prepared.get("case_id")),
                    expected_problem_id=_coerce_str(prepared.get("problem_id")),
                    evidence_assignment=_evidence_assignment,
                    evidence_atom_ids=_evidence_atom_ids,
                    revision_view_destination=(
                        stage_artifacts_dir
                        / "revision_views"
                        / sha256(
                            (
                                f"{repo_input}\0{prepared['repo_revision']}\0{verification_run_dir}"
                            ).encode()
                        ).hexdigest()[:16]
                    ),
                    replay_timeout_seconds=replay_timeout_seconds,
                    requested_repo_ref=requested_repo_ref,
                    resolved_repo_ref=resolved_repo_ref,
                    replay_executor=replay_executor,
                )
                if _origin_attachment_evidence:
                    candidate_workspace = _prepared_workspace
                    workspace_ref_path = verification_run_dir / "workspace_ref.json"
                    if workspace_ref_path.is_file():
                        try:
                            workspace_ref = _load_json_object(workspace_ref_path)
                        except (
                            OSError,
                            UnicodeError,
                            json.JSONDecodeError,
                            ValueError,
                        ):
                            workspace_ref = {}
                        workspace_raw = _coerce_str(workspace_ref.get("workspace_dir"))
                        if workspace_raw is not None:
                            candidate_workspace = Path(workspace_raw).resolve()
                    attachment_reads, attachment_errors = (
                        _origin_attachment_read_receipts(
                            run_dir=verification_run_dir,
                            workspace_dir=candidate_workspace,
                            manifest=_origin_attachment_evidence,
                            evidence_attempts=evidence_attempts,
                        )
                        if candidate_workspace is not None
                        else ([], ["origin_attachment_workspace_unavailable"])
                    )
                    candidate_receipt["origin_attachment_evidence"] = _origin_attachment_evidence
                    candidate_receipt["origin_attachment_read_attestations"] = attachment_reads
                    if attachment_errors:
                        _fail_evidence_verification(
                            candidate_receipt,
                            errors=attachment_errors,
                        )
                try:
                    correction_report = _load_json_object(correction_result.run_dir / "report.json")
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                    correction_report = {}
                _verified_candidates[_canonical_json_sha256(candidate)] = (
                    prepared,
                    candidate_receipt,
                    verification_run_dir,
                    correction_result,
                    correction_report,
                )
                return (
                    []
                    if candidate_receipt.get("status") == "verified"
                    else _string_list(candidate_receipt.get("errors"))
                    or ["research_evidence_verification_failed_without_diagnostic"]
                )

            verifier_source = _research_attempt_record(
                attempt_number=len(research_attempt_history) + 1,
                outcome="evidence_verification_invalid",
                run_dir=run_dir,
                report_path=report_path,
                validation_errors=verification_errors,
                attempted_dossier={
                    key: value
                    for key, value in dossier.items()
                    if key not in _RUNNER_OWNED_DOSSIER_FIELDS
                },
                attempt_kind="evidence_verification_feedback",
                source_attempt_sha256=current_attempt.get("attempt_sha256"),
                agent_session_id=result.agent_session_id,
                observed_agent_session_id=result.agent_session_id,
                attempt_wall_seconds=_run_wall_seconds(run_dir),
            )
            research_attempt_history.append(verifier_source)
            verifier_repair = _run_targeted_dossier_repairs(
                repo_input=str(repo_input),
                repo_revision=repo_revision,
                agent=agent,
                model=model,
                cfg=cfg,
                case_id=case_id,
                problem_id=pid,
                evidence_assignment=evidence_assignment,
                source_attempt=verifier_source,
                validation_errors=verification_errors,
                first_attempt_number=len(research_attempt_history) + 1,
                candidate_validator=validate_verifier_candidate,
                research_capabilities=research_capabilities,
                attempt_kind=(
                    "evidence_verification_research_continuation"
                    if research_capabilities
                    else "evidence_verification_dossier_repair"
                ),
                evidence_attempt_history=verifier_evidence_attempt_history,
            )
            verifier_attempts = [
                attempt
                for attempt in verifier_repair.get("attempts", [])
                if isinstance(attempt, dict)
            ]
            research_attempt_history.extend(verifier_attempts)
            req_meta["evidence_verification_corrections"] = {
                "status": verifier_repair.get("status"),
                "research_capabilities": research_capabilities,
                "attempt_count": len(verifier_attempts),
                "repair_run_dirs": verifier_repair.get("repair_run_dirs", []),
                "expected_session_id": verifier_repair.get("expected_session_id"),
                "observed_session_id": verifier_repair.get("observed_session_id"),
                "continuation_failure": verifier_repair.get("continuation_failure"),
            }
            if verifier_repair.get("status") == "parked_external_wait":
                verifier_external_wait = verifier_repair.get("external_wait")
                if isinstance(verifier_external_wait, dict):
                    stage_external_wait = _stage_external_wait_checkpoint(
                        external_wait=verifier_external_wait,
                        case_id=case_id,
                        problem_id=pid,
                        expected_session_id=_coerce_str(verifier_repair.get("expected_session_id")),
                        observed_session_id=_coerce_str(verifier_repair.get("observed_session_id")),
                    )
                    req_meta["evidence_verification_corrections"]["external_wait"] = (
                        verifier_external_wait
                    )
                    req_meta.update(
                        {
                            "dispatch_status": "parked_during_evidence_verification",
                            "external_wait_checkpoint_sha256": stage_external_wait[
                                "checkpoint_sha256"
                            ],
                            "route": "chatgpt_subscription",
                            "api_fallback_allowed": False,
                        }
                    )
                    blocking_reasons.append("research_external_wait_parked")
            repaired_raw = verifier_repair.get("dossier")
            repaired = dict(repaired_raw) if isinstance(repaired_raw, dict) else {}
            accepted = verified_candidates.get(_canonical_json_sha256(repaired))
            if verifier_repair.get("status") == "corrected" and accepted is not None:
                (
                    dossier,
                    evidence_verification,
                    accepted_run_dir,
                    effective_result,
                    effective_report_obj,
                ) = accepted
                dossier["evidence_verification"] = evidence_verification
                dossier["run_dir"] = str(accepted_run_dir)
                workspace_dir = evidence_verification.get("planning_workspace_dir")
                dossier["repo_workspace"] = (
                    workspace_dir if isinstance(workspace_dir, str) else None
                )
                blocking_reasons = _string_list(dossier.get("blocking_reasons"))
            dossier["research_attempts"] = research_attempt_history
            req_meta["attempts"] = [
                _research_attempt_request_summary(attempt) for attempt in research_attempt_history
            ]

        if effective_result.exit_code != 0:
            blocking_reasons.append(f"runner_exit_code:{effective_result.exit_code}")
        if effective_result.report_validation_errors:
            blocking_reasons.append("runner_report_validation_errors")
        if dossier.get("diff_classification") == "suspicious_implementation":
            blocking_reasons.append("suspicious_implementation_diff")
        report_status = _coerce_str(effective_report_obj.get("status"))
        report_status_reason = _report_status_blocking_reason(
            report_status,
            _coerce_str(dossier.get("research_status")),
        )
        if report_status_reason is not None:
            blocking_reasons.append(report_status_reason)
        if evidence_verification.get("status") != "verified":
            blocking_reasons.append("research_evidence_verification_failed")
        if blocking_reasons:
            # This is a failure-state override, never a success-like default.
            dossier["research_status"] = "blocked"
            dossier["blocking_reasons"] = list(dict.fromkeys(blocking_reasons))
        evidence_verification["claims_sha256"] = research_claims_sha256(dossier)
        evidence_verification["receipt_sha256"] = evidence_verification_sha256(
            evidence_verification
        )

        try:
            validated, warnings = parse_research_dossier_list(json.dumps([dossier]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            blocked = _blocked_research_after_run_failure(
                case_id=case_id,
                problem_id=pid,
                evidence_assignment=evidence_assignment,
                evidence_atom_ids=evidence_atom_ids,
                requested_repo_ref=requested_repo_ref,
                resolved_repo_ref=resolved_repo_ref,
                run_dir=run_dir,
                reason=f"research_dossier_malformed:{type(exc).__name__}",
                unknown="The case-local dossier failed the research proof contract",
                evidence_needed="Retry only this case and emit a schema-valid proof",
            )
            # Model-output attempts are immutable provenance. A later runner-proof failure is
            # recorded by the blocked wrapper, not retroactively rewritten into the model turn.
            _set_research_attempts(blocked, research_attempt_history)
            validated, _ = parse_research_dossier_list(json.dumps([blocked]))
            commit_dossier(validated[0])
            req_meta["attempts"] = [
                _research_attempt_request_summary(attempt) for attempt in research_attempt_history
            ]
            continue
        normalized = validated[0]
        if warnings:
            normalized["_parse_warning"] = "; ".join(warnings)
        commit_dossier(normalized)

    requests_path = stage_artifacts_dir / "repro_research_requests.json"
    requests_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "stage": _STAGE,
                "dry_run": bool(dry_run),
                "repo_input": repo_input,
                "requested_repo_ref": requested_repo_ref,
                "resolved_repo_ref": resolved_repo_ref,
                "target_slug": target_slug,
                "replay_executor": replay_metadata,
                "requests": requests,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    # Ensure the stage doc records the exact runs_dir used so a reader can find the run directories.
    cfg_effective = replace(cfg, runs_dir=Path(cfg.runs_dir))
    restart_cycle_metrics = {
        key: sum(
            float(metrics[key]) if key.endswith("_wall_seconds") else int(metrics[key])
            for dossier in dossiers
            for metrics in [
                _restart_cycle_metrics(
                    [
                        attempt
                        for attempt in (
                            dossier.get("research_attempts")
                            if isinstance(dossier.get("research_attempts"), list)
                            else []
                        )
                        if isinstance(attempt, dict)
                    ]
                )
            ]
        )
        for key in (
            "fresh_restart_cycle_count",
            "fresh_restart_objective_progress_cycle_count",
            "fresh_restart_nonprogress_cycle_count",
            "fresh_restart_equivalent_cycle_count",
            "fresh_restart_cycle_wall_seconds",
        )
    }
    completed_progress_checkpoint = (
        _completed_prefix_checkpoint(
            selected_problems=selected_problems,
            completed_dossiers=dossiers,
            resolved_repo_ref=resolved_repo_ref,
            compatibility_contract=compatibility_contract,
        )
        if stage_external_wait is None
        else None
    )
    final_completed_checkpoint = (
        completed_stage3_checkpoint(
            dossiers=dossiers,
            fresh_research_dossier_count=len(dossiers),
            retained_research_reused_count=0,
            compatibility_contract=compatibility_contract,
            progress_checkpoint=completed_progress_checkpoint,
        )
        if completed_progress_checkpoint is not None
        else None
    )
    stage_doc = build_stage_document(
        _STAGE,
        dossiers,
        input_meta={
            "selected_problem_count": len(selected_problems),
            "stage_status": (
                "parked_external_wait" if stage_external_wait is not None else "completed"
            ),
            "research_compatibility": compatibility_contract,
            "progress_checkpoint": completed_progress_checkpoint,
            "completed_stage_checkpoint": final_completed_checkpoint,
            "external_wait": (
                json.loads(json.dumps(stage_external_wait, ensure_ascii=False))
                if stage_external_wait is not None
                else None
            ),
            "parked_before_dispatch_count": sum(
                1
                for request_meta in requests
                if request_meta.get("dispatch_status") == "parked_not_started"
            ),
            "resumed_external_wait_checkpoint_sha256": (
                resume_checkpoint.get("checkpoint_sha256")
                if resume_checkpoint is not None
                else None
            ),
            "resumed_completed_prefix_count": len(completed_prefix),
            "resumed_progress_checkpoint_sha256": resumed_progress_checkpoint_sha256,
            "external_wait_resume_cleared": (
                resume_trigger_cleared if resume_checkpoint is not None else None
            ),
            "evidence_sufficient_count": sum(
                1 for dossier in dossiers if dossier.get("research_status") == "evidence_sufficient"
            ),
            "blocked_case_count": sum(
                1 for dossier in dossiers if dossier.get("research_status") == "blocked"
            ),
            "insufficient_evidence_count": sum(
                1
                for dossier in dossiers
                if dossier.get("research_status") == "insufficient_evidence"
            ),
            "output_contract_retry_count": sum(
                max(
                    0,
                    len(dossier.get("research_attempts", [])) - 1,
                )
                for dossier in dossiers
                if isinstance(dossier.get("research_attempts"), list)
            ),
            "output_contract_invalid_attempt_count": sum(
                1
                for dossier in dossiers
                for attempt in (
                    dossier.get("research_attempts")
                    if isinstance(dossier.get("research_attempts"), list)
                    else []
                )
                if isinstance(attempt, dict) and attempt.get("outcome") == "output_contract_invalid"
            ),
            "same_session_correction_attempt_count": sum(
                1
                for dossier in dossiers
                for attempt in (
                    dossier.get("research_attempts")
                    if isinstance(dossier.get("research_attempts"), list)
                    else []
                )
                if isinstance(attempt, dict)
                and attempt.get("attempt_kind") == "model_output_repair"
            ),
            "same_session_repaired_case_count": sum(
                1
                for dossier in dossiers
                if any(
                    isinstance(attempt, dict) and attempt.get("outcome") == "repair_contract_valid"
                    for attempt in (
                        dossier.get("research_attempts")
                        if isinstance(dossier.get("research_attempts"), list)
                        else []
                    )
                )
            ),
            "same_session_correction_wall_seconds": sum(
                float(attempt.get("attempt_wall_seconds") or 0.0)
                for dossier in dossiers
                for attempt in (
                    dossier.get("research_attempts")
                    if isinstance(dossier.get("research_attempts"), list)
                    else []
                )
                if isinstance(attempt, dict)
                and attempt.get("attempt_kind") == "model_output_repair"
            ),
            "same_session_stalled_restart_count": sum(
                1
                for dossier in dossiers
                for attempt in (
                    dossier.get("research_attempts")
                    if isinstance(dossier.get("research_attempts"), list)
                    else []
                )
                if isinstance(attempt, dict)
                and attempt.get("attempt_kind") == "model_output_repair"
                and isinstance(attempt.get("repair_progress"), dict)
                and attempt["repair_progress"].get("decision") == "restart"
            ),
            "same_session_unavailable_count": sum(
                1
                for dossier in dossiers
                for attempt in (
                    dossier.get("research_attempts")
                    if isinstance(dossier.get("research_attempts"), list)
                    else []
                )
                if isinstance(attempt, dict)
                and isinstance(attempt.get("repair_progress"), dict)
                and isinstance(attempt["repair_progress"].get("terminal_continuation"), dict)
                and attempt["repair_progress"]["terminal_continuation"].get("status")
                in {"same_session_continuation_unavailable", "workspace_unavailable"}
            ),
            "fresh_research_restart_count": sum(
                1
                for dossier in dossiers
                for attempt in (
                    dossier.get("research_attempts")
                    if isinstance(dossier.get("research_attempts"), list)
                    else []
                )
                if isinstance(attempt, dict)
                and attempt.get("attempt_kind") == "fresh_research_retry"
            ),
            **restart_cycle_metrics,
            "repairable_paused_case_count": sum(
                1
                for dossier in dossiers
                if any(
                    str(reason).startswith("research_dossier_repairable_paused:")
                    for reason in (
                        dossier.get("blocking_reasons")
                        if isinstance(dossier.get("blocking_reasons"), list)
                        else []
                    )
                )
            ),
            "useful_research_output_count": sum(
                1
                for dossier in dossiers
                if dossier.get("research_status")
                in {"evidence_sufficient", "insufficient_evidence"}
            ),
            # These require labeled benchmark outcomes; live contract validity cannot infer them.
            "accepted_bad_output_count": None,
            "false_rejected_output_count": None,
            "dry_run": bool(dry_run),
            "repo_input": repo_input,
            "target_slug": target_slug,
            "agent": agent,
            "model": model,
            "runner_runs_dir": str(cfg_effective.runs_dir),
            "mission_id": _MISSION_ID,
            "persona_id": _PERSONA_ID,
            "policy": _POLICY,
            "replay_executor": replay_metadata,
        },
        artifacts={"requests_json": str(requests_path)},
    )
    return stage_doc
