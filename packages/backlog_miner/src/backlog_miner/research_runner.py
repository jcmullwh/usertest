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
import subprocess
from collections.abc import Sequence
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
    research_claims_sha256,
)
from runner_core import RunnerConfig, RunRequest, run_once
from runner_core.target_acquire import acquire_target

from backlog_miner.origin_evidence import (
    materialize_origin_attachments,
    origin_attachment_requirements,
    verify_materialized_origin_attachments,
)
from backlog_miner.research_evidence import (
    ReplayExecutor,
    verify_research_evidence,
)

_LOG = logging.getLogger(__name__)

_MISSION_ID = "backlog_repro_research"
_PERSONA_ID = "repo_backlog_investigator"
_POLICY = "write"

_STAGE = "repro_research"

_GUIDANCE_PATH = Path("configs") / "backlog_stage_guidance" / "repro_research.md"
_REPO_INTENT_PATH = Path("configs") / "repo_intent.md"

_EXTENSION_KEY = "backlog_repro_research"


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
) -> tuple[list[dict[str, Any]], list[str]]:
    errors = verify_materialized_origin_attachments(
        workspace_dir=workspace_dir,
        manifest=manifest,
    )
    events_path = run_dir / "normalized_events.jsonl"
    if not events_path.is_file():
        return [], [*errors, "origin_attachment_normalized_events_missing"]
    events: list[dict[str, Any]] = []
    try:
        for line_number, line in enumerate(events_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ValueError(f"event {line_number} is not an object")
            events.append(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return [], [*errors, "origin_attachment_normalized_events_unreadable"]

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
        for event_index, event in enumerate(events):
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


def _coerce_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


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


def _canonical_json_sha256(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _write_evidence_assignment_sidecar(
    run_dir: Path,
    *,
    evidence_assignment: dict[str, Any],
) -> Path:
    """Persist runner-owned parent lineage before downstream report processing."""

    target_ref_path = run_dir / "target_ref.json"
    target_ref = (
        _load_json_object(target_ref_path) if target_ref_path.is_file() else {}
    )
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
        ("agent_stderr", "agent_stderr.txt", "Agent stderr captured by the runner"),
    ):
        path = run_dir / filename
        if path.exists():
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
        timeout=30,
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
        verification["claims_sha256"] = research_claims_sha256(dossier)
        verification["receipt_sha256"] = evidence_verification_sha256(verification)
    return dossier


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
    blocked research proofs so unrelated cases continue. Global configuration failures
    (for example a missing repo reference or stage guidance) still raise because no case
    can be researched correctly under that configuration.
    """
    stage_artifacts_dir = artifacts_dir / _STAGE
    stage_artifacts_dir.mkdir(parents=True, exist_ok=True)

    requests: list[dict[str, Any]] = []
    dossiers: list[dict[str, Any]] = []
    replay_metadata = dict(replay_executor_metadata or {"executor": "blocked"})

    if (
        not dry_run
        and selected_problems
        and (repo_input is None or not str(repo_input).strip())
    ):
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

    for idx, problem in enumerate(selected_problems, start=1):
        pid = _coerce_str(problem.get("problem_id"))
        if pid is None:
            raise ValueError(
                f"run_repro_research_stage: selected_problems[{idx}] missing problem_id"
            )
        case_id = _coerce_str(problem.get("case_id")) or "case:unassigned"
        assignment_raw = problem.get("evidence_assignment")
        evidence_assignment = (
            dict(assignment_raw) if isinstance(assignment_raw, dict) else {
                "status": "incomplete",
                "errors": ["origin_evidence_assignment_missing"],
                "case_id": case_id,
                "problem_id": pid,
                "expected_atom_ids": [],
                "atom_receipts": [],
            }
        )

        seed = _stable_seed(pid)
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
            dossiers.append(validated[0])
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
            dossiers.append(validated[0])
            continue

        prepared_workspace: Path | None = None
        origin_attachment_evidence: dict[str, Any] = {}
        problem_for_agent = dict(problem)
        if _has_origin_attachment_refs(evidence_atoms):
            assert repo_input is not None
            assert resolved_repo_ref is not None
            preferred_workspace = (
                stage_artifacts_dir
                / "research_workspaces"
                / f"{idx:03d}_{seed}_{uuid4().hex[:12]}"
            )
            prepared_workspace, origin_attachment_evidence = (
                _prepare_origin_evidence_workspace(
                    repo_input=str(repo_input),
                    repo_ref=resolved_repo_ref,
                    preferred_workspace_dir=preferred_workspace,
                    evidence_atoms=[atom for atom in evidence_atoms if isinstance(atom, dict)],
                    source_root=repo_root,
                )
            )
            evidence_assignment["origin_attachment_evidence"] = origin_attachment_evidence
            materialization_errors_raw = origin_attachment_evidence.get("errors")
            materialization_errors = (
                materialization_errors_raw
                if isinstance(materialization_errors_raw, list)
                else []
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
                "materialization_sha256": origin_attachment_evidence.get(
                    "materialization_sha256"
                ),
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
                dossiers.append(validated[0])
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
            seed=seed,
            model=model,
            agent_append_system_prompt=append_prompt,
            keep_workspace=True,
            resume_workspace_dir=prepared_workspace,
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
            dossiers.append(validated[0])
            continue
        run_dir = result.run_dir
        _write_evidence_assignment_sidecar(
            run_dir,
            evidence_assignment=evidence_assignment,
        )

        report_path = run_dir / "report.json"
        if not report_path.exists():
            blocked = _blocked_research_after_run_failure(
                case_id=case_id,
                problem_id=pid,
                evidence_assignment=evidence_assignment,
                evidence_atom_ids=evidence_atom_ids,
                requested_repo_ref=requested_repo_ref,
                resolved_repo_ref=resolved_repo_ref,
                run_dir=run_dir,
                reason="research_report_missing",
                unknown="The case-local research run did not produce report.json",
                evidence_needed="Retry only this case and retain a valid report.json",
            )
            validated, _ = parse_research_dossier_list(json.dumps([blocked]))
            dossiers.append(validated[0])
            continue

        try:
            report_obj = _load_json_object(report_path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            blocked = _blocked_research_after_run_failure(
                case_id=case_id,
                problem_id=pid,
                evidence_assignment=evidence_assignment,
                evidence_atom_ids=evidence_atom_ids,
                requested_repo_ref=requested_repo_ref,
                resolved_repo_ref=resolved_repo_ref,
                run_dir=run_dir,
                reason=f"research_report_malformed:{type(exc).__name__}",
                unknown="The case-local research report is malformed or unreadable",
                evidence_needed="Retry only this case and retain schema-valid JSON",
            )
            validated, _ = parse_research_dossier_list(json.dumps([blocked]))
            dossiers.append(validated[0])
            continue
        ext_raw = report_obj.get("extensions")
        ext_map = ext_raw if isinstance(ext_raw, dict) else {}
        ext_block_raw = ext_map.get(_EXTENSION_KEY)
        if not isinstance(ext_block_raw, dict):
            blocked = _blocked_research_after_run_failure(
                case_id=case_id,
                problem_id=pid,
                evidence_assignment=evidence_assignment,
                evidence_atom_ids=evidence_atom_ids,
                requested_repo_ref=requested_repo_ref,
                resolved_repo_ref=resolved_repo_ref,
                run_dir=run_dir,
                reason=f"research_extension_missing:{_EXTENSION_KEY}",
                unknown="The case-local report omitted the required research proof extension",
                evidence_needed="Retry only this case and emit the required extension",
            )
            validated, _ = parse_research_dossier_list(json.dumps([blocked]))
            dossiers.append(validated[0])
            continue

        ext_pid = _coerce_str(ext_block_raw.get("problem_id"))
        ext_case_id = _coerce_str(ext_block_raw.get("case_id"))
        problem_id_mismatch = ext_pid != pid
        case_id_mismatch = ext_case_id != case_id
        if problem_id_mismatch:
            _LOG.warning(
                "stage3: extension problem_id mismatch expected=%s got=%s (blocking proof)",
                pid,
                ext_pid,
            )
        if case_id_mismatch:
            _LOG.warning(
                "stage3: extension case_id mismatch expected=%s got=%s (blocking proof)",
                case_id,
                ext_case_id,
            )

        if ext_block_raw.get("implementation_performed") is True:
            blocked = _blocked_research_after_run_failure(
                case_id=case_id,
                problem_id=pid,
                evidence_assignment=evidence_assignment,
                evidence_atom_ids=evidence_atom_ids,
                requested_repo_ref=requested_repo_ref,
                resolved_repo_ref=resolved_repo_ref,
                run_dir=run_dir,
                reason="research_implementation_performed_forbidden",
                unknown="The case-local run changed production code instead of researching",
                evidence_needed="Retry only this case in research-only mode",
            )
            validated, _ = parse_research_dossier_list(json.dumps([blocked]))
            dossiers.append(validated[0])
            continue

        writes_purpose = _string_list(ext_block_raw.get("writes_purpose"))

        diff_numstat_path = run_dir / "diff_numstat.json"
        diff_numstat = _load_diff_numstat(diff_numstat_path)
        modified_paths: list[str] = []
        for entry in diff_numstat:
            p = _coerce_str(entry.get("path"))
            if p is not None:
                modified_paths.append(p)

        diff_class, diff_reasons = _classify_diff(modified_paths, writes_purpose=writes_purpose)

        dossier: dict[str, Any] = dict(ext_block_raw)
        dossier["research_schema_version"] = RESEARCH_PROOF_SCHEMA_VERSION
        dossier["case_id"] = case_id
        dossier["problem_id"] = pid
        dossier["evidence_assignment"] = evidence_assignment
        repo_revision = _canonical_repo_revision(run_dir)
        blocking_reasons = _string_list(dossier.get("blocking_reasons"))
        if repo_revision is None:
            repo_revision = _coerce_str(dossier.get("repo_revision")) or "unavailable"
            blocking_reasons.append("runner_repo_revision_unavailable")
        dossier["repo_revision"] = repo_revision
        dossier["diff_classification"] = diff_class
        if diff_reasons:
            dossier["diff_suspicious_reasons"] = diff_reasons
        dossier["run_dir"] = str(run_dir)
        dossier["runner_exit_code"] = int(result.exit_code)
        dossier["runner_report_validation_errors"] = list(result.report_validation_errors)
        artifacts = {
            "report_json": str(report_path),
            "report_md": str(run_dir / "report.md"),
            "patch_diff": str(run_dir / "patch.diff"),
            "diff_numstat_json": str(diff_numstat_path),
            "normalized_events_jsonl": str(run_dir / "normalized_events.jsonl"),
            "agent_stderr_txt": str(run_dir / "agent_stderr.txt"),
        }
        dossier["artifacts"] = artifacts

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
                verification_errors_raw = evidence_verification.get("errors")
                verification_errors = (
                    verification_errors_raw
                    if isinstance(verification_errors_raw, list)
                    else []
                )
                evidence_verification["errors"] = list(
                    dict.fromkeys([*verification_errors, *attachment_errors])
                )
                evidence_verification["status"] = "failed"
        dossier["evidence_verification"] = evidence_verification
        workspace_dir = evidence_verification.get("planning_workspace_dir")
        dossier["repo_workspace"] = workspace_dir if isinstance(workspace_dir, str) else None

        if result.exit_code != 0:
            blocking_reasons.append(f"runner_exit_code:{result.exit_code}")
        if result.report_validation_errors:
            blocking_reasons.append("runner_report_validation_errors")
        if dossier.get("diff_classification") == "suspicious_implementation":
            blocking_reasons.append("suspicious_implementation_diff")
        report_status = _coerce_str(report_obj.get("status"))
        if report_status in {"partial", "failure"}:
            blocking_reasons.append(f"runner_report_status:{report_status}")
        if problem_id_mismatch:
            blocking_reasons.append("research_problem_id_mismatch")
        if case_id_mismatch:
            blocking_reasons.append("research_case_id_mismatch")
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
            validated, _ = parse_research_dossier_list(json.dumps([blocked]))
            dossiers.append(validated[0])
            continue
        normalized = validated[0]
        if warnings:
            normalized["_parse_warning"] = "; ".join(warnings)
        dossiers.append(normalized)

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
    stage_doc = build_stage_document(
        _STAGE,
        dossiers,
        input_meta={
            "selected_problem_count": len(selected_problems),
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
