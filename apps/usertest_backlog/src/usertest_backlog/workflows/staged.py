# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

from backlog_core import assess_research_readiness, build_operational_failure_candidates
from backlog_miner.pipeline import model_invocation_manifest_ref
from backlog_miner.research_evidence import BlockedReplayExecutor
from backlog_miner.research_runner import (
    _resolve_repo_ref,
    _valid_stage3_research_compatibility_contract,
    _validated_completed_stage3_checkpoint,
    stage3_research_compatibility_contract,
    stage3_research_dossier_resume_sha256,
)
from backlog_repo import verify_outcome_record_provenance

from usertest_backlog.commands.atom_actions import (
    _backfill_failure_event_atoms_from_legacy_entries,
    _update_atom_actions_from_backlog,
)
from usertest_backlog.commands.export_tickets import (
    _build_export_projection,
    _export_artifact_paths,
    _ux_review_path_for_backlog,
)
from usertest_backlog.shared import *
from usertest_backlog.workflows.derived_evidence import (
    annotate_operational_failure_candidates,
    annotate_primary_derived_evidence,
    filter_derived_history_records,
    inferred_implementation_runs_root,
    ingest_derived_evidence_records,
    with_operational_candidate_metadata,
)
from usertest_backlog.workflows.downstream_hydration import (
    chain_matches_research_dossier,
    flatten_chain_items,
    hydrate_retained_downstream_chain,
)
from usertest_backlog.workflows.implementation_planning import (
    _render_change_plans_markdown,
    _run_implementation_planning_stage,
)
from usertest_backlog.workflows.orphan_implementation_history import (
    recover_orphan_implementation_history,
)
from usertest_backlog.workflows.pipeline_provenance import (
    first_party_module_binding_errors,
)
from usertest_backlog.workflows.post_research_relations import (
    apply_post_research_relation_assessments,
    collapse_post_research_verified_mechanisms,
)
from usertest_backlog.workflows.prioritization import (
    _research_dispatch_sort_key,
    _run_problem_prioritization_stage,
)
from usertest_backlog.workflows.problem_mining import (
    _persist_canonical_relation_receipts,
    _run_problem_case_relation_review,
    _run_problem_mining_stage,
)
from usertest_backlog.workflows.problem_mining_evidence import (
    immutable_atom_evidence_projection,
)
from usertest_backlog.workflows.qualification_healing import (
    pending_repaired_shadow_run_errors,
    qualification_correction_consumption_errors,
)
from usertest_backlog.workflows.qualification_repair_materialization import (
    best_qualified_fallback_errors,
    materialize_repaired_shadow_run,
    select_best_qualified_fallback,
)
from usertest_backlog.workflows.qualification_repair_runtime import (
    QualificationRepairRuntimeResult,
    plan_qualification_repair_route_groups,
    run_stage456_qualification_repairs,
)
from usertest_backlog.workflows.qualification_run_manifest import (
    SEMANTIC_RUN_EVIDENCE_MANIFEST_KIND,
    extend_semantic_manifest_atom_closure,
)
from usertest_backlog.workflows.qualification_transaction import (
    build_qualification_input_bundle,
    capture_qualification_preparation_snapshot,
    extend_qualification_preparation_snapshot,
    load_qualification_input_bundle,
    qualification_input_bundle_errors,
    qualification_runtime_compatibility_errors,
    write_qualification_input_bundle,
)
from usertest_backlog.workflows.reproduction_research import (
    _atomic_write_research_json,
    _build_authenticated_stage3_single_case_prefix,
    _configured_replay_executor,
    _render_research_dossiers_markdown,
    _run_repro_research_stage,
)
from usertest_backlog.workflows.research_hydration import hydrate_retained_research_proof
from usertest_backlog.workflows.shadow_validation import (
    evaluate_shadow_invariants,
    normalize_shadow_gate_config,
    operational_shadow_pending_run_path,
    record_shadow_cycle,
    shadow_pending_run_path,
    shadow_state_path,
    validate_pending_operational_shadow_run,
    validate_pending_shadow_run,
    write_pending_operational_shadow_run,
    write_pending_shadow_run,
)
from usertest_backlog.workflows.solution_options import (
    _render_solution_options_markdown,
    _run_solution_optioning_stage,
)
from usertest_backlog.workflows.solution_selection import (
    _render_solution_selection_markdown,
    _run_solution_selection_stage,
)

_EXACT_SESSION_CORRECTION_AGENTS = frozenset({"codex"})


def _attach_current_case_registry_context(
    problem_records: Sequence[Mapping[str, Any]],
    *,
    case_registry: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Attach the current durable evidence/proof frontier after Stage-1 rediscovery."""

    contexts_by_case_id = {
        str(context["case_id"]): context
        for context in problem_case_records_from_registry(case_registry)
        if isinstance(context.get("case_id"), str)
    }
    durable_fields = (
        "evidence_atom_ids",
        "source_evidence_atom_ids",
        "derived_evidence_atom_ids",
        "case_revision",
        "source_evidence_projection_version",
        "source_evidence_atom_sha256_by_id",
        "source_evidence_snapshot_complete",
        "source_evidence_snapshot_missing_atom_ids",
        "source_evidence_snapshot_sha256",
        "prior_stage_context",
        "_historical_case_context",
        "last_pipeline_stage",
    )
    attached: list[dict[str, Any]] = []
    for raw_record in problem_records:
        record = dict(raw_record)
        case_id = _coerce_string(record.get("case_id"))
        context = contexts_by_case_id.get(case_id or "")
        if context is not None:
            for field in durable_fields:
                if field in context:
                    record[field] = deepcopy(context[field])
        attached.append(record)
    return attached


def _restore_sealed_qualification_lineage(
    bundled_atoms: Sequence[Mapping[str, Any]],
    *,
    case_registry: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Reapply sealed durable case membership to decision-free bundle evidence."""

    return normalize_atom_lineage(
        [dict(atom) for atom in bundled_atoms],
        case_registry=case_registry,
        strict_new_output=True,
    )


def _qualification_case_registry_seed_path(
    qualification_input_bundle: Mapping[str, Any],
) -> Path:
    """Return the verified sealed registry seed used for the entire execution."""

    bundle_source = qualification_input_bundle.get("source_inputs")
    bundle_source = bundle_source if isinstance(bundle_source, Mapping) else {}
    registry_receipt = bundle_source.get("case_registry_seed")
    registry_receipt = registry_receipt if isinstance(registry_receipt, Mapping) else {}
    registry_seed_raw = _coerce_string(registry_receipt.get("path"))
    if registry_seed_raw is None:
        raise ValueError("Qualification input bundle is missing its registry seed.")
    return Path(registry_seed_raw).resolve()


def _qualification_correction_identity(
    *,
    source_pending_run_sha256: str,
    source_adjudication_sha256: str,
    phase1_bundle_sha256: str,
    qualification_manifest_sha256: str,
    source_artifact_sha256s: Mapping[str, Any],
    routes: Sequence[Mapping[str, Any]],
) -> str:
    return sha256(
        json.dumps(
            {
                "source_pending_run_sha256": source_pending_run_sha256,
                "source_adjudication_sha256": source_adjudication_sha256,
                "phase1_bundle_sha256": phase1_bundle_sha256,
                "qualification_manifest_sha256": qualification_manifest_sha256,
                "source_artifact_sha256s": dict(source_artifact_sha256s),
                "route_sha256s": [route.get("route_sha256") for route in routes],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _write_qualification_json_once(path: Path, payload: Mapping[str, Any]) -> Path:
    encoded = (json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(encoded)
    except FileExistsError:
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"qualification_correction_write_once_unreadable:{path}") from exc
        if existing != encoded:
            raise ValueError(f"qualification_correction_write_once_conflict:{path}") from None
    return path


def _write_qualification_bytes_once(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(content)
    except FileExistsError:
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"qualification_write_once_unreadable:{path}") from exc
        if existing != content:
            raise ValueError(f"qualification_write_once_conflict:{path}") from None
    if path.read_bytes() != content:
        raise ValueError(f"qualification_write_once_verification_failed:{path}")
    return path


def _load_qualification_correction_completion(
    *,
    path: Path,
    expected_input_sha256: str,
) -> dict[str, Any] | None:
    candidates = [path] if path.is_file() else []
    candidates.extend(
        candidate
        for candidate in sorted(path.parent.glob(f"{path.stem}.terminal.*{path.suffix}"))
        if candidate not in candidates
    )
    for candidate in candidates:
        value = _load_qualification_json_object(
            candidate,
            name="qualification_correction_completion",
        )
        if value.get("contract_kind") != "qualification_correction_completion":
            raise ValueError("qualification_correction_completion_kind_invalid")
        if value.get("correction_input_sha256") != expected_input_sha256:
            raise ValueError("qualification_correction_completion_input_mismatch")
        observed_hash = value.get("content_sha256")
        projected = {key: item for key, item in value.items() if key != "content_sha256"}
        expected_hash = sha256(
            json.dumps(
                projected,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        if observed_hash != expected_hash:
            raise ValueError("qualification_correction_completion_hash_invalid")
        repair_result = value.get("repair_result")
        if not isinstance(repair_result, dict):
            raise ValueError("qualification_correction_completion_result_invalid")
        scheduler_pending = repair_result.get("qualification_scheduler_pending")
        if scheduler_pending is False:
            return dict(repair_result)
        if scheduler_pending is True:
            continue
        # Pre-scheduler completions did not distinguish a terminal recurrence from a
        # repairable pause. Only a fully accepted legacy route set is safe to reuse.
        accepted = repair_result.get("accepted_repair_count")
        unresolved = repair_result.get("unresolved_route_count")
        if (
            isinstance(accepted, int)
            and not isinstance(accepted, bool)
            and accepted > 0
            and unresolved == 0
        ):
            return dict(repair_result)
    return None


def _build_qualification_correction_completion(
    *,
    correction_input_sha256: str,
    consumption_path: Path,
    consumption_sha256: str,
    repair_result: Mapping[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "contract_kind": "qualification_correction_completion",
        "correction_input_sha256": correction_input_sha256,
        "consumption_path": str(consumption_path.resolve()),
        "consumption_sha256": consumption_sha256,
        "repair_result": dict(repair_result),
    }
    payload["content_sha256"] = sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return payload


def _live_agent_preflight_error(
    *,
    agent: str,
    dry_run: bool,
    score_shadow: bool,
) -> str | None:
    """Reject live authors that cannot resume the exact originating session."""

    normalized = agent.strip().casefold()
    if dry_run or score_shadow or normalized in _EXACT_SESSION_CORRECTION_AGENTS:
        return None
    return (
        "live_backlog_agent_exact_session_correction_unsupported:"
        f"{normalized or 'missing'}:use=codex_or_dry_run"
    )


def _qualification_artifact_path(repo_root: Path, value: Any) -> Path | None:
    raw = _coerce_string(value)
    return _resolve_optional_path(repo_root, Path(raw)) if raw is not None else None


def _qualification_file_sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _qualification_valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


def _qualification_cycle_contract(
    *,
    bundle_path: Path,
    bundle_sha256: str,
    manifest_sha256: str,
    cycle_root: Path,
    source_runs_dir: Path,
    stage_runs_dir: Path,
    out_json: Path,
    out_md: Path,
    state_path: Path,
    repo_root: Path,
    repo_input: str | None,
    target: str | None,
    research_ref: str | None,
    breadth_profile: str,
    execution_profile: Mapping[str, Any],
    owned_names: Sequence[str],
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": 1,
        "contract_kind": "sealed_qualification_cycle",
        "qualification_input_bundle_path": str(bundle_path.resolve()),
        "qualification_input_bundle_sha256": bundle_sha256,
        "qualification_manifest_sha256": manifest_sha256,
        "cycle_root": str(cycle_root.resolve()),
        "source_runs_dir": str(source_runs_dir.resolve()),
        "stage_runs_dir": str(stage_runs_dir.resolve()),
        "out_json": str(out_json.resolve()),
        "out_md": str(out_md.resolve()),
        "shadow_state_path": str(state_path.resolve()),
        "repo_root": str(repo_root.resolve()),
        "repo_input": repo_input,
        "target": target,
        "research_ref": research_ref,
        "breadth_profile": breadth_profile,
        "execution_profile": dict(execution_profile),
        "owned_cycle_names": sorted(set(owned_names)),
    }
    body["content_sha256"] = _qualification_canonical_sha256(body)
    return body


def _normalized_qualification_execution_profile(
    *,
    args: argparse.Namespace,
    agent: str,
    model: str | None,
    breadth_profile: str,
    prompts_dir: Path,
    policy_config_path: Path | None,
) -> dict[str, Any]:
    excluded_raw = getattr(args, "exclude_atom_status", None)
    excluded = excluded_raw or ["ticketed", "queued", "actioned"]
    return {
        "agent": agent.strip().casefold(),
        "model": model.strip() if isinstance(model, str) and model.strip() else None,
        "breadth_profile": breadth_profile,
        "prompts_dir": str(prompts_dir.expanduser().resolve()),
        "policy_config_path": (
            str(policy_config_path.expanduser().resolve())
            if policy_config_path is not None
            else None
        ),
        "policy_enabled": not bool(getattr(args, "no_policy", False)),
        "carryover_actioned_only": bool(getattr(args, "carryover_actioned_only", False)),
        "exclude_atom_statuses": sorted(
            {
                normalized
                for value in excluded
                for normalized in [_normalize_atom_status(_coerce_string(value))]
                if normalized
            }
        ),
    }


def _qualification_cycle_marker_paths(
    *,
    cycle_root: Path,
    stage_runs_dir: Path,
) -> tuple[Path, Path]:
    return (
        cycle_root.resolve() / ".qualification_transaction.json",
        stage_runs_dir.resolve() / ".qualification_transaction.json",
    )


def _qualification_cycle_namespace_errors(
    *,
    contract: Mapping[str, Any],
    resume: bool,
    score: bool,
) -> list[str]:
    cycle_root = Path(str(contract["cycle_root"])).resolve()
    stage_runs_dir = Path(str(contract["stage_runs_dir"])).resolve()
    if (
        cycle_root == stage_runs_dir
        or cycle_root in stage_runs_dir.parents
        or stage_runs_dir in cycle_root.parents
    ):
        return ["qualification_cycle_and_stage_roots_not_isolated"]
    markers = _qualification_cycle_marker_paths(
        cycle_root=cycle_root,
        stage_runs_dir=stage_runs_dir,
    )
    expected_bytes = (json.dumps(dict(contract), indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    marker_exists = [marker.is_file() for marker in markers]
    if any(marker_exists):
        if not all(marker_exists):
            return ["qualification_cycle_marker_incomplete"]
        if any(marker.read_bytes() != expected_bytes for marker in markers):
            return ["qualification_cycle_identity_changed"]
        if not (resume or score):
            return ["qualification_cycle_resume_not_requested"]
    elif score:
        return ["qualification_cycle_marker_missing_for_score"]
    elif cycle_root.exists():
        foreign = list(cycle_root.rglob("*"))
        if foreign:
            return [
                "qualification_cycle_foreign_preexisting_path:"
                + str(sorted(foreign, key=lambda item: item.as_posix())[0])
            ]
    if not any(marker_exists) and stage_runs_dir.exists():
        foreign_stage = list(stage_runs_dir.rglob("*"))
        if foreign_stage:
            return [
                "qualification_stage_foreign_preexisting_path:"
                + str(
                    sorted(
                        foreign_stage,
                        key=lambda item: item.as_posix(),
                    )[0]
                )
            ]

    if all(marker_exists):
        owned_names_raw = contract.get("owned_cycle_names")
        owned_names = set(owned_names_raw) if isinstance(owned_names_raw, list) else set()
        for path in cycle_root.iterdir():
            if path.name == markers[0].name:
                continue
            if path.name in owned_names or any(
                path.name.startswith(f"{name}.") for name in owned_names
            ):
                continue
            return [f"qualification_cycle_foreign_preexisting_path:{path}"]
    return []


def _prepare_or_validate_qualification_cycle_namespace(
    *,
    contract: Mapping[str, Any],
    resume: bool,
    score: bool,
) -> None:
    errors = _qualification_cycle_namespace_errors(
        contract=contract,
        resume=resume,
        score=score,
    )
    if errors:
        raise ValueError(",".join(errors))
    cycle_root = Path(str(contract["cycle_root"])).resolve()
    stage_runs_dir = Path(str(contract["stage_runs_dir"])).resolve()
    markers = _qualification_cycle_marker_paths(
        cycle_root=cycle_root,
        stage_runs_dir=stage_runs_dir,
    )
    if not markers[0].exists():
        encoded = (json.dumps(dict(contract), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        for marker in markers:
            _write_qualification_bytes_once(marker, encoded)


def _load_post_pipeline_qualification_artifact(path: Path | None) -> Any:
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "artifact_load_error": type(exc).__name__,
            "artifact_path": str(path),
        }


def _qualification_workspace_exposure_errors(
    *,
    artifact_paths: Mapping[str, Path | None],
    model_readable_roots: Iterable[Path],
) -> list[str]:
    roots = {root.resolve() for root in model_readable_roots}
    errors: list[str] = []
    for name, raw_path in artifact_paths.items():
        if raw_path is None:
            continue
        path = raw_path.resolve()
        for root in roots:
            if path == root or root in path.parents:
                errors.append(
                    f"qualification_artifact_inside_model_readable_root:{name}:{path}:{root}"
                )
                break
    return errors


def _qualification_custody_errors(
    *,
    custody_paths: Mapping[str, Path | None],
    model_readable_roots: Iterable[Path],
) -> list[str]:
    roots = {root.resolve() for root in model_readable_roots}
    errors: list[str] = []
    for name, raw_path in custody_paths.items():
        if raw_path is None:
            continue
        path = raw_path.resolve()
        for root in roots:
            if path == root or root in path.parents:
                errors.append(
                    f"qualification_custody_inside_model_readable_root:{name}:{path}:{root}"
                )
                break
    return errors


def _prior_qualification_label_paths(state_path: Path) -> dict[str, Path]:
    if not state_path.is_file():
        return {}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"shared_state_unreadable": state_path}
    cycles_raw = state.get("cycles") if isinstance(state, Mapping) else None
    cycles = cycles_raw if isinstance(cycles_raw, list) else []
    result: dict[str, Path] = {}
    for cycle_index, cycle in enumerate(cycles):
        receipts_raw = cycle.get("artifact_receipts") if isinstance(cycle, Mapping) else None
        receipts = receipts_raw if isinstance(receipts_raw, list) else []
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            name = _coerce_string(receipt.get("name"))
            if name is None or not name.startswith("qualification."):
                continue
            if name == "qualification.input_bundle":
                continue
            for path_field in ("source_path", "snapshot_path"):
                raw = _coerce_string(receipt.get(path_field))
                if raw is not None:
                    result[f"prior_cycle_{cycle_index}:{name}:{path_field}"] = Path(raw).resolve()
    return result


def _require_stage_model_invocation_provenance(stage_doc: dict[str, Any]) -> None:
    errors = verify_stage_model_invocation_contract(stage_doc)
    if errors:
        raise ValueError(
            "stage_model_invocation_provenance_invalid:"
            + str(stage_doc.get("stage") or "unknown")
            + ":"
            + ",".join(errors)
        )


def _load_stage1_relation_resume(
    *,
    problem_records_path: Path,
    artifacts_dir: Path,
) -> dict[str, Any] | None:
    """Load an interrupted post-mining relation checkpoint without rerunning miners."""

    if not problem_records_path.is_file():
        return None
    try:
        stage_doc_raw = json.loads(problem_records_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"stage1_relation_resume_document_unreadable:{type(exc).__name__}"
        ) from exc
    if not isinstance(stage_doc_raw, dict):
        raise ValueError("stage1_relation_resume_document_not_object")
    stage_doc = dict(stage_doc_raw)
    meta_raw = stage_doc.get("input_meta")
    meta = meta_raw if isinstance(meta_raw, Mapping) else {}
    if stage_doc.get("stage") != "problem_mining":
        return None
    if not isinstance(meta.get("problem_mining_evidence_draft"), Mapping):
        return None
    items = stage_doc.get("items")
    if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
        raise ValueError("stage1_relation_resume_problem_records_invalid")
    _require_stage_model_invocation_provenance(stage_doc)

    tag = "problem_mining_relation_review_001"
    review_dir = artifacts_dir / "problem_mining" / tag
    batch_path = review_dir / f"{tag}.prompt.txt"
    decisions_path = review_dir / f"{tag}.response.txt"
    try:
        checkpoint_raw = json.loads(batch_path.read_text(encoding="utf-8"))
        decisions_raw = json.loads(decisions_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"stage1_relation_resume_checkpoint_unreadable:{type(exc).__name__}"
        ) from exc
    if not isinstance(checkpoint_raw, dict) or (
        checkpoint_raw.get("schema_version") != "problem_relation_review_batches_v1"
    ):
        raise ValueError("stage1_relation_resume_checkpoint_schema_invalid")
    batches_raw = checkpoint_raw.get("batches")
    if not isinstance(batches_raw, list) or not all(
        isinstance(batch, Mapping) for batch in batches_raw
    ):
        raise ValueError("stage1_relation_resume_batches_invalid")
    batches = [dict(batch) for batch in batches_raw]
    if checkpoint_raw.get("batch_count") != len(batches):
        raise ValueError("stage1_relation_resume_batch_count_invalid")
    if not isinstance(decisions_raw, list) or not all(
        isinstance(decision, Mapping) for decision in decisions_raw
    ):
        raise ValueError("stage1_relation_resume_decisions_invalid")
    decisions = [dict(decision) for decision in decisions_raw]
    focus_ids = [
        focus_id
        for batch in batches
        for focus_id in (
            batch.get("focus_ids") if isinstance(batch.get("focus_ids"), list) else []
        )
        if isinstance(focus_id, str) and focus_id.strip()
    ]
    decision_focus_ids = [
        str(decision["focus_id"])
        for decision in decisions
        if isinstance(decision.get("focus_id"), str)
    ]
    if (
        len(focus_ids) != len(set(focus_ids))
        or len(decision_focus_ids) != len(decisions)
        or sorted(decision_focus_ids) != sorted(focus_ids)
        or sum(int(batch.get("decision_count") or 0) for batch in batches)
        != len(decisions)
    ):
        raise ValueError("stage1_relation_resume_decision_partition_invalid")

    manifest_paths = sorted(review_dir.glob("*.model_invocation.json"))
    manifest_refs = [
        model_invocation_manifest_ref(path, require_verified=False)
        for path in manifest_paths
    ]
    manifest_status_by_tag = {
        str(ref.get("tag") or ""): str(ref.get("status") or "")
        for ref in manifest_refs
    }
    attempted_tags = {
        str(attempt.get("tag") or "")
        for batch in batches
        for attempt in (
            batch.get("attempt_history")
            if isinstance(batch.get("attempt_history"), list)
            else []
        )
        if isinstance(attempt, Mapping) and attempt.get("tag")
    }
    if attempted_tags != set(manifest_status_by_tag):
        raise ValueError("stage1_relation_resume_manifest_frontier_mismatch")
    for batch in batches:
        if batch.get("status") != "completed":
            continue
        successful_tag = str(batch.get("successful_attempt_tag") or "")
        if manifest_status_by_tag.get(successful_tag) != "verified":
            raise ValueError("stage1_relation_resume_completed_batch_unverified")

    return {
        "stage_doc": stage_doc,
        "decisions": decisions,
        "batches": batches,
        "manifest_refs": manifest_refs,
    }


def _stage3_provider_external_wait(stage_doc: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return only the runner-owned, non-API Stage-3 subscription checkpoint."""
    meta_raw = stage_doc.get("input_meta")
    meta = meta_raw if isinstance(meta_raw, Mapping) else {}
    checkpoint_raw = meta.get("external_wait")
    checkpoint = checkpoint_raw if isinstance(checkpoint_raw, Mapping) else {}
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
        or not _qualification_valid_sha256(wait.get("error_artifact_sha256"))
        or not isinstance(wait.get("error_artifact_size_bytes"), int)
        or checkpoint.get("checkpoint_sha256")
        != _qualification_canonical_sha256(checkpoint_without_hash)
    ):
        return None
    return json.loads(json.dumps(checkpoint, ensure_ascii=False))


def _stage3_completed_progress(
    stage_doc: Mapping[str, Any],
    *,
    expected_compatibility_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return only a hash-bound, sequential Stage-3 progress checkpoint."""
    meta_raw = stage_doc.get("input_meta")
    meta = meta_raw if isinstance(meta_raw, Mapping) else {}
    checkpoint_raw = meta.get("progress_checkpoint")
    checkpoint = checkpoint_raw if isinstance(checkpoint_raw, Mapping) else {}
    selected_raw = checkpoint.get("selected")
    selected = selected_raw if isinstance(selected_raw, list) else []
    completed_raw = checkpoint.get("completed_prefix")
    completed = completed_raw if isinstance(completed_raw, list) else []
    items_raw = stage_doc.get("items")
    items = items_raw if isinstance(items_raw, list) else []
    checkpoint_without_hash = {
        key: value for key, value in checkpoint.items() if key != "checkpoint_sha256"
    }
    compatibility = _valid_stage3_research_compatibility_contract(
        checkpoint.get("research_compatibility")
    )
    meta_compatibility = _valid_stage3_research_compatibility_contract(
        meta.get("research_compatibility")
    )
    expected_compatibility = (
        dict(expected_compatibility_contract)
        if isinstance(expected_compatibility_contract, Mapping)
        else None
    )
    if (
        meta.get("stage_status") != "checkpointed_progress"
        or checkpoint.get("schema_version") != 1
        or checkpoint.get("status") != "checkpointed_progress"
        or checkpoint.get("scope") != "repro_research_stage"
        or not selected
        or not completed
        or len(completed) != len(items)
        or len(completed) > len(selected)
        or stage_doc.get("item_count") != len(items)
        or compatibility is None
        or meta_compatibility != compatibility
        or (expected_compatibility is not None and compatibility != expected_compatibility)
        or checkpoint.get("checkpoint_sha256")
        != _qualification_canonical_sha256(checkpoint_without_hash)
    ):
        return None
    for index, (summary, item) in enumerate(zip(completed, items, strict=True)):
        selected_item = selected[index] if index < len(selected) else None
        if (
            not isinstance(summary, Mapping)
            or not isinstance(item, Mapping)
            or not isinstance(selected_item, Mapping)
            or summary.get("problem_id") != selected_item.get("problem_id")
            or summary.get("case_id") != selected_item.get("case_id")
            or item.get("problem_id") != selected_item.get("problem_id")
            or item.get("case_id") != selected_item.get("case_id")
            or summary.get("dossier_sha256") != stage3_research_dossier_resume_sha256(item)
        ):
            return None
    return json.loads(json.dumps(checkpoint, ensure_ascii=False))


def _stage3_completed_stage(
    stage_doc: Mapping[str, Any],
    *,
    expected_compatibility_contract: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return only a final Stage-3 artifact safe to reuse after later-stage failure."""

    meta_raw = stage_doc.get("input_meta")
    meta = meta_raw if isinstance(meta_raw, Mapping) else {}
    if not isinstance(meta.get("resume_upstream"), Mapping):
        return None
    return _validated_completed_stage3_checkpoint(
        stage_doc,
        expected_compatibility_contract=expected_compatibility_contract,
    )


def _annotate_completed_stage3_document(
    stage_doc: Mapping[str, Any],
    *,
    input_meta_updates: Mapping[str, Any],
    artifact_updates: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach post-research lineage without rewriting hash-bound Stage-3 proofs.

    Canonicalized/split dossier lists are downstream in-memory views.  The completed
    Stage-3 artifact remains the authored proof sequence bound by its progress and
    completion checkpoints, so a mixed split/non-split run can resume itself.
    """

    annotated = dict(stage_doc)
    meta_raw = annotated.get("input_meta")
    meta = dict(meta_raw) if isinstance(meta_raw, Mapping) else {}
    meta.update(dict(input_meta_updates))
    annotated["input_meta"] = meta
    artifacts_raw = annotated.get("artifacts")
    artifacts = dict(artifacts_raw) if isinstance(artifacts_raw, Mapping) else {}
    artifacts.update(dict(artifact_updates))
    annotated["artifacts"] = artifacts
    return annotated


def _stage3_resume_file_receipt(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    return {
        "path": str(path.resolve()),
        "sha256": sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def _stage3_resume_upstream_contract(
    *,
    paths: Mapping[str, Path],
    source_atoms: Sequence[Mapping[str, Any]],
    target_slug: str | None,
    repo_input: str | None,
    research_ref: str | None,
    selected_problem_ids: Sequence[str],
) -> dict[str, Any]:
    contract: dict[str, Any] = {
        "schema_version": 1,
        "contract_kind": "stage3_resume_upstream",
        "scope": {
            "target_slug": target_slug,
            "repo_input": repo_input,
            "research_ref": research_ref,
        },
        "selected_problem_ids": list(selected_problem_ids),
        "source_atom_corpus": {
            "projection": "immutable_atom_evidence_projection_v1",
            "atom_count": len(source_atoms),
            "content_sha256": _qualification_canonical_sha256(
                sorted(
                    [immutable_atom_evidence_projection(atom) for atom in source_atoms],
                    key=lambda atom: (
                        _coerce_string(atom.get("atom_id")) or "",
                        _qualification_canonical_sha256(atom),
                    ),
                )
            ),
        },
        "artifacts": {
            name: _stage3_resume_file_receipt(path)
            for name, path in paths.items()
            if path.is_file()
        },
    }
    contract["content_sha256"] = _qualification_canonical_sha256(contract)
    return contract


def _load_stage3_resume_upstream(
    *,
    stage3_document: Mapping[str, Any],
    expected_paths: Mapping[str, Path],
    target_slug: str | None,
    repo_input: str | None,
    research_ref: str | None,
    current_atoms: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    meta_raw = stage3_document.get("input_meta")
    meta = meta_raw if isinstance(meta_raw, Mapping) else {}
    contract_raw = meta.get("resume_upstream")
    contract = dict(contract_raw) if isinstance(contract_raw, Mapping) else {}
    without_hash = {key: value for key, value in contract.items() if key != "content_sha256"}
    scope_raw = contract.get("scope")
    scope = scope_raw if isinstance(scope_raw, Mapping) else {}
    artifacts_raw = contract.get("artifacts")
    artifacts = artifacts_raw if isinstance(artifacts_raw, Mapping) else {}
    source_atom_corpus_raw = contract.get("source_atom_corpus")
    source_atom_corpus = (
        source_atom_corpus_raw if isinstance(source_atom_corpus_raw, Mapping) else {}
    )
    if (
        contract.get("schema_version") != 1
        or contract.get("contract_kind")
        not in {"stage3_resume_upstream", "stage3_external_wait_resume_upstream"}
        or contract.get("content_sha256") != _qualification_canonical_sha256(without_hash)
        or scope.get("target_slug") != target_slug
        or scope.get("repo_input") != repo_input
        or scope.get("research_ref") != research_ref
        or source_atom_corpus.get("projection") != "immutable_atom_evidence_projection_v1"
    ):
        raise ValueError("stage3_external_wait_resume_upstream_contract_invalid")
    required = {
        "atoms",
        "problem_records",
        "problem_mining_evidence",
        "prioritized_problems",
        "case_registry",
    }
    if not required <= set(artifacts):
        raise ValueError("stage3_external_wait_resume_upstream_artifacts_missing")
    for name in required:
        path = expected_paths[name].resolve()
        receipt_raw = artifacts.get(name)
        receipt = receipt_raw if isinstance(receipt_raw, Mapping) else {}
        if (
            receipt.get("path") != str(path)
            or not path.is_file()
            or receipt != _stage3_resume_file_receipt(path)
        ):
            raise ValueError(f"stage3_external_wait_resume_upstream_changed:{name}")
    current_source_projection = sorted(
        [immutable_atom_evidence_projection(atom) for atom in current_atoms],
        key=lambda atom: (
            _coerce_string(atom.get("atom_id")) or "",
            _qualification_canonical_sha256(atom),
        ),
    )
    if source_atom_corpus.get("atom_count") != len(current_atoms) or source_atom_corpus.get(
        "content_sha256"
    ) != _qualification_canonical_sha256(current_source_projection):
        raise ValueError("stage3_external_wait_resume_source_atoms_changed")
    try:
        stage1 = json.loads(expected_paths["problem_records"].read_text(encoding="utf-8"))
        stage2 = json.loads(expected_paths["prioritized_problems"].read_text(encoding="utf-8"))
        case_registry = json.loads(expected_paths["case_registry"].read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"stage3_external_wait_resume_upstream_unreadable:{type(exc).__name__}"
        ) from exc
    if not all(isinstance(value, dict) for value in (stage1, stage2, case_registry)):
        raise ValueError("stage3_external_wait_resume_upstream_not_object")
    if stage1.get("stage") != "problem_mining" or stage2.get("stage") != "problem_prioritization":
        raise ValueError("stage3_external_wait_resume_upstream_stage_mismatch")
    _require_stage_model_invocation_provenance(stage1)
    _require_stage_model_invocation_provenance(stage2)
    selected_ids = [
        str(item["problem_id"])
        for item in sorted(
            (
                item
                for item in (stage2.get("items") if isinstance(stage2.get("items"), list) else [])
                if isinstance(item, dict)
                and item.get("selected_for_research") is True
                and isinstance(item.get("problem_id"), str)
            ),
            key=_research_dispatch_sort_key,
        )
    ]
    if selected_ids != contract.get("selected_problem_ids"):
        raise ValueError("stage3_external_wait_resume_upstream_selection_changed")
    return stage1, stage2, case_registry


def _persist_downstream_case_lineage(
    *,
    stage_doc: dict[str, Any],
    out_json: Path,
    problem_cases: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Attach canonical case identity to a stage document and persist it.

    Stage parsers retain legacy wire compatibility, so lineage is enforced at this
    orchestration boundary for every newly written stage artifact.
    """

    items_raw = stage_doc.get("items")
    items = (
        [item for item in items_raw if isinstance(item, dict)]
        if isinstance(items_raw, list)
        else []
    )
    propagated = propagate_case_lineage(
        items,
        problem_cases,
        strict_new_output=True,
    )
    updated_doc = dict(stage_doc)
    updated_doc["items"] = propagated
    input_meta_raw = updated_doc.get("input_meta")
    input_meta = dict(input_meta_raw) if isinstance(input_meta_raw, dict) else {}
    input_meta["case_lineage_propagated"] = True
    input_meta["canonical_case_count"] = len(problem_cases)
    updated_doc["input_meta"] = input_meta
    if updated_doc.get("stage") == "repro_research":
        _atomic_write_research_json(out_json, updated_doc)
    else:
        out_json.write_text(
            json.dumps(updated_doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return updated_doc, propagated


def _persist_case_registry_stage_lineage(
    *,
    case_registry: dict[str, Any],
    case_registry_path: Path,
    stage_doc: dict[str, Any],
) -> dict[str, Any]:
    """Persist one completed stage into the cumulative case graph."""

    updated = update_case_registry_stage_lineage(
        case_registry,
        stage_doc=stage_doc,
        strict=True,
    )
    write_case_registry(case_registry_path, updated)
    return updated


def _persist_authenticated_stage3_single_case_prefix(
    *,
    repo_root: Path,
    repo_input: str | None,
    research_ref: str | None,
    target_slug: str | None,
    upstream_paths: Mapping[str, Path],
    research_json: Path,
    research_md: Path,
    imported_dossier: Mapping[str, Any],
    agent: str,
    model: str | None,
    validation_error_rescore: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist one authenticated Stage-3 dossier as the canonical resume prefix."""

    required_paths = {
        "atoms",
        "problem_records",
        "problem_mining_evidence",
        "prioritized_problems",
        "case_registry",
    }
    if not required_paths <= set(upstream_paths):
        raise ValueError("stage3_prefix_import_upstream_paths_invalid")
    canonical_paths = {name: upstream_paths[name] for name in required_paths}
    if not isinstance(repo_input, str) or not repo_input.strip():
        raise ValueError("stage3_prefix_import_repo_input_missing")
    if not isinstance(research_ref, str) or not research_ref.strip():
        raise ValueError("stage3_prefix_import_research_ref_missing")
    resolved_repo_ref = _resolve_repo_ref(repo_input, research_ref)

    try:
        atoms = load_atoms_jsonl(canonical_paths["atoms"])
        stage2_seed_raw = json.loads(
            canonical_paths["prioritized_problems"].read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(
            f"stage3_prefix_import_upstream_unreadable:{type(exc).__name__}"
        ) from exc
    if not isinstance(stage2_seed_raw, dict):
        raise ValueError("stage3_prefix_import_priority_document_not_object")
    stage2_seed_items_raw = stage2_seed_raw.get("items")
    stage2_seed_items = (
        stage2_seed_items_raw if isinstance(stage2_seed_items_raw, list) else []
    )
    selected_seed = sorted(
        (
            item
            for item in stage2_seed_items
            if isinstance(item, Mapping)
            and item.get("selected_for_research") is True
            and isinstance(item.get("problem_id"), str)
        ),
        key=_research_dispatch_sort_key,
    )
    if not selected_seed:
        raise ValueError("stage3_prefix_import_selection_empty")
    selected_problem_ids = [str(item["problem_id"]) for item in selected_seed]
    initial_upstream = _stage3_resume_upstream_contract(
        paths=canonical_paths,
        source_atoms=atoms,
        target_slug=target_slug,
        repo_input=repo_input,
        research_ref=research_ref,
        selected_problem_ids=selected_problem_ids,
    )
    stage1, stage2, case_registry = _load_stage3_resume_upstream(
        stage3_document={"input_meta": {"resume_upstream": initial_upstream}},
        expected_paths=canonical_paths,
        target_slug=target_slug,
        repo_input=repo_input,
        research_ref=research_ref,
        current_atoms=atoms,
    )
    stage1_items_raw = stage1.get("items")
    problem_records = (
        [dict(item) for item in stage1_items_raw if isinstance(item, Mapping)]
        if isinstance(stage1_items_raw, list)
        else []
    )
    stage2_items_raw = stage2.get("items")
    selected = sorted(
        (
            dict(item)
            for item in stage2_items_raw
            if isinstance(item, Mapping)
            and item.get("selected_for_research") is True
            and isinstance(item.get("problem_id"), str)
        ),
        key=_research_dispatch_sort_key,
    ) if isinstance(stage2_items_raw, list) else []

    prior_stage_document: dict[str, Any] | None = None
    if research_json.is_file():
        try:
            prior_raw = json.loads(research_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"stage3_prefix_import_prior_unreadable:{type(exc).__name__}"
            ) from exc
        if not isinstance(prior_raw, dict):
            raise ValueError("stage3_prefix_import_prior_not_object")
        prior_stage_document = prior_raw
        _load_stage3_resume_upstream(
            stage3_document=prior_stage_document,
            expected_paths=canonical_paths,
            target_slug=target_slug,
            repo_input=repo_input,
            research_ref=research_ref,
            current_atoms=atoms,
        )

    candidate = _build_authenticated_stage3_single_case_prefix(
        repo_root=repo_root,
        selected_priority_decisions=selected,
        problem_records=problem_records,
        atoms=atoms,
        imported_dossier=imported_dossier,
        resolved_repo_ref=resolved_repo_ref,
        repo_input=repo_input,
        target_slug=target_slug,
        agent=agent,
        model=model,
        artifacts={"research_json": str(research_json), "research_md": str(research_md)},
        prior_stage_document=prior_stage_document,
        validation_error_rescore=validation_error_rescore,
    )

    candidate_meta_raw = candidate.get("input_meta")
    candidate_meta = (
        dict(candidate_meta_raw) if isinstance(candidate_meta_raw, Mapping) else {}
    )
    candidate_meta["resume_upstream"] = initial_upstream
    candidate["input_meta"] = candidate_meta

    # Validate the current Stage-1/2 corpus and registry binding before the first write.
    _load_stage3_resume_upstream(
        stage3_document=candidate,
        expected_paths=canonical_paths,
        target_slug=target_slug,
        repo_input=repo_input,
        research_ref=research_ref,
        current_atoms=atoms,
    )

    persisted, _ = _persist_downstream_case_lineage(
        stage_doc=candidate,
        out_json=research_json,
        problem_cases=[dict(item) for item in problem_records],
    )
    updated_registry = _persist_case_registry_stage_lineage(
        case_registry=dict(case_registry),
        case_registry_path=canonical_paths["case_registry"],
        stage_doc=persisted,
    )
    final_upstream = _stage3_resume_upstream_contract(
        paths=canonical_paths,
        source_atoms=atoms,
        target_slug=target_slug,
        repo_input=repo_input,
        research_ref=research_ref,
        selected_problem_ids=selected_problem_ids,
    )
    persisted_meta_raw = persisted.get("input_meta")
    persisted_meta = (
        dict(persisted_meta_raw) if isinstance(persisted_meta_raw, Mapping) else {}
    )
    persisted_meta["resume_upstream"] = final_upstream
    persisted["input_meta"] = persisted_meta
    _atomic_write_research_json(research_json, persisted)

    try:
        final_stage_raw = json.loads(research_json.read_text(encoding="utf-8"))
        final_registry_raw = json.loads(
            canonical_paths["case_registry"].read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"stage3_prefix_import_final_unreadable:{type(exc).__name__}") from exc
    if not isinstance(final_stage_raw, dict) or not isinstance(final_registry_raw, dict):
        raise ValueError("stage3_prefix_import_final_not_object")
    compatibility = stage3_research_compatibility_contract(agent=agent)
    if (
        _stage3_completed_progress(
            final_stage_raw,
            expected_compatibility_contract=compatibility,
        )
        is None
    ):
        raise ValueError("stage3_prefix_import_final_progress_invalid")
    _load_stage3_resume_upstream(
        stage3_document=final_stage_raw,
        expected_paths=canonical_paths,
        target_slug=target_slug,
        repo_input=repo_input,
        research_ref=research_ref,
        current_atoms=atoms,
    )
    if final_registry_raw != updated_registry:
        raise ValueError("stage3_prefix_import_final_registry_changed")
    final_items_raw = final_stage_raw.get("items")
    final_items = (
        [item for item in final_items_raw if isinstance(item, dict)]
        if isinstance(final_items_raw, list)
        else []
    )
    records_by_id = {
        str(item.get("problem_id")): dict(item)
        for item in problem_records
        if isinstance(item.get("problem_id"), str)
    }
    title = research_json.stem.removesuffix(".research") or "Research"
    research_md.parent.mkdir(parents=True, exist_ok=True)
    research_md.write_text(
        _render_research_dossiers_markdown(
            final_items,
            problem_records_by_id=records_by_id,
            title=f"{title} – Research Dossiers",
        ),
        encoding="utf-8",
    )
    return final_stage_raw, final_registry_raw


def _merge_reused_downstream_stage_document(
    *,
    stage: str,
    stage_doc: Mapping[str, Any] | None,
    reused_items: Sequence[Mapping[str, Any]],
    agent: str,
    dry_run: bool,
    artifacts: Mapping[str, str],
    count_updates: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge exact retained items without manufacturing a model invocation."""

    local_only = stage_doc is None
    if stage_doc is None:
        merged = build_stage_document(
            stage,
            [],
            input_meta={
                "model_invocation_skipped": "all_ready_downstream_chains_reused",
                "stage_status": "completed",
                "dry_run": bool(dry_run),
            },
            artifacts=dict(artifacts),
        )
        merged = attach_stage_model_invocation_contract(
            merged,
            agent=agent,
            dry_run=dry_run,
            manifest_refs=[],
            invocation_expected=False,
        )
    else:
        merged = dict(stage_doc)
    fresh_raw = merged.get("items")
    fresh = (
        [dict(item) for item in fresh_raw if isinstance(item, Mapping)]
        if isinstance(fresh_raw, list)
        else []
    )
    reused = [dict(item) for item in reused_items]
    all_items = [*fresh, *reused]
    identity_rows = [
        (
            _coerce_string(item.get("case_id")),
            _coerce_string(item.get("problem_id")),
            _coerce_string(item.get("option_id")),
            _coerce_string(item.get("selected_option_id")),
            _coerce_string(item.get("plan_revision_id")),
        )
        for item in all_items
    ]
    if len(identity_rows) != len(set(identity_rows)):
        raise ValueError(f"{stage}_reused_downstream_item_identity_duplicate")
    merged["items"] = all_items
    merged["item_count"] = len(all_items)
    input_meta_raw = merged.get("input_meta")
    input_meta = dict(input_meta_raw) if isinstance(input_meta_raw, Mapping) else {}
    input_meta.update(dict(count_updates))
    input_meta.update(
        {
            "fresh_item_count": len(fresh),
            "reused_item_count": len(reused),
            "retained_downstream_chain_reuse": bool(reused),
            "all_items_reused": local_only and bool(reused),
        }
    )
    merged["input_meta"] = input_meta
    artifacts_raw = merged.get("artifacts")
    merged_artifacts = dict(artifacts_raw) if isinstance(artifacts_raw, Mapping) else {}
    merged_artifacts.update(dict(artifacts))
    merged["artifacts"] = merged_artifacts
    return merged


def _run_fresh_downstream_stage(
    *,
    fresh_problem_records: Sequence[Mapping[str, Any]],
    reused_chains: Sequence[Mapping[str, Any]],
    run_stage: Callable[[], dict[str, Any]],
) -> dict[str, Any] | None:
    """Dispatch only when at least one case still needs downstream model work."""

    if not fresh_problem_records and reused_chains:
        return None
    return run_stage()


def _ticket_lineage_stage_document(
    *,
    tickets: list[dict[str, Any]],
    problem_cases: list[dict[str, Any]],
    generated_at: str,
    backlog_json_path: Path,
    backlog_md_path: Path,
) -> dict[str, Any]:
    """Build a compact ticket-assembly artifact for the persistent case graph."""

    records: list[dict[str, Any]] = []
    represented_case_ids: set[str] = set()
    for ticket in tickets:
        case_id = ticket_export_case_id(ticket)
        if case_id is None:
            raise ValueError("ticket_assembly_lineage_missing_case_id")
        represented_case_ids.add(case_id)
        problem_raw = ticket.get("problem_record")
        problem = problem_raw if isinstance(problem_raw, dict) else {}
        records.append(
            {
                "case_id": case_id,
                "problem_id": _coerce_string(ticket.get("problem_id"))
                or _coerce_string(problem.get("problem_id")),
                "plan_revision_id": ticket_export_plan_revision_id(ticket),
                "ticket_fingerprint": ticket_export_fingerprint(ticket),
                "ticket_stage": _coerce_string(ticket.get("stage")) or "triage",
            }
        )

    # A no-ticket result is still a ticket-assembly outcome worth retaining.  It has
    # no fingerprint and therefore cannot be confused with an exported ticket identity.
    for problem_case in problem_cases:
        case_id = _coerce_string(problem_case.get("case_id"))
        if case_id is None or case_id in represented_case_ids:
            continue
        records.append(
            {
                "case_id": case_id,
                "problem_id": _coerce_string(problem_case.get("problem_id")),
                "ticket_stage": "not_emitted",
            }
        )

    stage_doc = build_stage_document(
        "ticket_assembly",
        records,
        input_meta={
            "ticket_count": len(tickets),
            "case_count": len(problem_cases),
        },
        artifacts={
            "backlog_json": str(backlog_json_path),
            "backlog_md": str(backlog_md_path),
        },
    )
    stage_doc["generated_at"] = generated_at
    return stage_doc


def _sync_case_registry_outcomes(
    *,
    case_registry: dict[str, Any],
    atom_actions: dict[str, dict[str, Any]],
    trusted_runs_roots: tuple[Path, ...] = (),
    owner_roots: tuple[Path, ...] = (),
) -> dict[str, int]:
    """Apply durable atom-ledger outcomes to persistent case lifecycle state.

    Plan-folder reconciliation validates embedded outcome records before copying their
    state and case identity into the atom ledger.  The backlog pipeline consumes that
    durable projection here instead of treating queue/action status itself as resolution.
    """

    cases_raw = case_registry.get("cases")
    cases = cases_raw if isinstance(cases_raw, dict) else {}
    plans_by_case: dict[str, dict[str, dict[str, Any]]] = {}
    legacy_latest_by_case: dict[str, tuple[str, str]] = {}
    atom_ids_by_case: dict[str, set[str]] = {}
    verified_identities_by_case: dict[str, dict[str, dict[str, Any]]] = {}
    conflicting_case_identities: set[str] = set()
    invalid_outcome_records = 0
    provenance_failed_outcome_records = 0

    def retain_verified_identity(
        *,
        case_id: str,
        revision_id: str,
        outcome_verification: Mapping[str, Any] | None,
        normalized_outcome: Mapping[str, Any] | None,
    ) -> None:
        if (
            outcome_verification is None
            or outcome_verification.get("verified") is not True
            or outcome_verification.get("provenance_status") != "verified"
            or normalized_outcome is None
        ):
            return
        identity_raw = outcome_verification.get("verified_case_identity")
        if (
            not isinstance(identity_raw, Mapping)
            or identity_raw.get("case_id") != case_id
            or identity_raw.get("plan_revision_id") != revision_id
        ):
            return
        candidate = {
            "identity": dict(identity_raw),
            "recorded_at": str(normalized_outcome["recorded_at"]),
        }
        identities = verified_identities_by_case.setdefault(case_id, {})
        previous = identities.get(revision_id)
        if previous is not None:
            if previous.get("identity") != candidate["identity"]:
                conflicting_case_identities.add(case_id)
                return
            if previous.get("recorded_at", "") >= candidate["recorded_at"]:
                return
        identities[revision_id] = candidate

    for raw_atom_id, entry in atom_actions.items():
        case_id = _coerce_string(entry.get("case_id"))
        if case_id is None:
            continue
        atom_id = _coerce_string(raw_atom_id) or _coerce_string(entry.get("atom_id"))
        if atom_id is not None:
            atom_ids_by_case.setdefault(case_id, set()).add(atom_id)
        plan_outcomes_raw = entry.get("plan_outcomes")
        if isinstance(plan_outcomes_raw, dict):
            case_plans = plans_by_case.setdefault(case_id, {})
            for raw_revision_id, raw_plan_outcome in plan_outcomes_raw.items():
                revision_id = _coerce_string(raw_revision_id)
                if revision_id is None or not isinstance(raw_plan_outcome, dict):
                    continue
                required = raw_plan_outcome.get("required") is not False
                outcome_record_raw = raw_plan_outcome.get("outcome_record")
                normalized_outcome: dict[str, Any] | None = None
                outcome_verification: dict[str, Any] | None = None
                if isinstance(outcome_record_raw, dict):
                    outcome_verification = verify_outcome_record_provenance(
                        outcome_record_raw,
                        trusted_runs_roots=trusted_runs_roots,
                        owner_roots=owner_roots,
                        case_registry=case_registry,
                    )
                    structural_record = outcome_verification.get("outcome_record")
                    if outcome_verification.get("structural_status") != "valid":
                        invalid_outcome_records += 1
                    elif outcome_verification.get("verified") is not True:
                        provenance_failed_outcome_records += 1
                    elif isinstance(structural_record, dict):
                        normalized_outcome = structural_record
                        if normalized_outcome.get("outcome_scope") == "plan_copy":
                            # Copy disposition is archival lineage, never case lifecycle.
                            continue
                        if (
                            normalized_outcome.get("case_id") != case_id
                            or normalized_outcome.get("plan_revision_id") != revision_id
                        ):
                            invalid_outcome_records += 1
                            normalized_outcome = None

                retain_verified_identity(
                    case_id=case_id,
                    revision_id=revision_id,
                    outcome_verification=outcome_verification,
                    normalized_outcome=normalized_outcome,
                )

                if normalized_outcome is not None:
                    state = str(normalized_outcome["state"])
                    recorded_at = str(normalized_outcome["recorded_at"])
                else:
                    raw_state = (_coerce_string(raw_plan_outcome.get("state")) or "").lower()
                    # Only a planned sentinel is trusted without a complete validated
                    # OutcomeRecord. Any projected terminal state fails open.
                    state = "planned" if raw_state == "planned" else "unverified"
                    recorded_at = _coerce_string(raw_plan_outcome.get("recorded_at")) or ""
                candidate = {
                    "state": state,
                    "recorded_at": recorded_at,
                    "path": _coerce_string(raw_plan_outcome.get("path")) or "",
                    "fingerprint": _coerce_string(raw_plan_outcome.get("fingerprint")) or "",
                    "required": required,
                }
                if outcome_verification is not None:
                    candidate["outcome_verification"] = outcome_verification
                    if (
                        outcome_verification.get("structural_status") == "valid"
                        and outcome_verification.get("verified") is not True
                    ):
                        candidate["structural_outcome_record"] = outcome_verification.get(
                            "outcome_record"
                        )
                if normalized_outcome is not None:
                    candidate["outcome_record"] = normalized_outcome
                previous = case_plans.get(revision_id)
                if previous is None:
                    case_plans[revision_id] = candidate
                else:
                    previous_state = previous.get("state", "planned")
                    previous_terminal = previous_state in TERMINAL_CASE_STATES
                    candidate_terminal = state in TERMINAL_CASE_STATES
                    if previous_terminal and not candidate_terminal:
                        case_plans[revision_id] = candidate
                    elif previous_terminal == candidate_terminal and recorded_at > previous.get(
                        "recorded_at", ""
                    ):
                        case_plans[revision_id] = candidate
                    case_plans[revision_id]["required"] = (
                        previous.get("required") is not False or required
                    )
            continue

        outcome_record_raw = entry.get("last_outcome_record")
        normalized_outcome = None
        outcome_verification = None
        if isinstance(outcome_record_raw, dict):
            outcome_verification = verify_outcome_record_provenance(
                outcome_record_raw,
                trusted_runs_roots=trusted_runs_roots,
                owner_roots=owner_roots,
                case_registry=case_registry,
            )
            structural_record = outcome_verification.get("outcome_record")
            if outcome_verification.get("structural_status") != "valid":
                invalid_outcome_records += 1
            elif outcome_verification.get("verified") is not True:
                provenance_failed_outcome_records += 1
            elif isinstance(structural_record, dict):
                normalized_outcome = structural_record
                if (
                    normalized_outcome.get("outcome_scope") != "case"
                    or normalized_outcome.get("case_id") != case_id
                ):
                    invalid_outcome_records += 1
                    normalized_outcome = None
        verified_identity_raw = (
            outcome_verification.get("verified_case_identity")
            if isinstance(outcome_verification, dict)
            else None
        )
        revision_id = (
            _coerce_string(verified_identity_raw.get("plan_revision_id"))
            if isinstance(verified_identity_raw, Mapping)
            else None
        )
        if revision_id is not None:
            retain_verified_identity(
                case_id=case_id,
                revision_id=revision_id,
                outcome_verification=outcome_verification,
                normalized_outcome=normalized_outcome,
            )
        raw_outcome_state = (_coerce_string(entry.get("last_outcome_state")) or "").lower()
        if normalized_outcome is not None:
            outcome_state = str(normalized_outcome["state"])
            recorded_at = str(normalized_outcome["recorded_at"])
        elif raw_outcome_state:
            # A bare legacy label proves neither implementation nor verification.
            # Preserve the harmless planned sentinel; every more advanced state must
            # carry a complete validated OutcomeRecord or fail open to unverified.
            outcome_state = "planned" if raw_outcome_state == "planned" else "unverified"
            recorded_at = _coerce_string(entry.get("last_outcome_recorded_at")) or ""
        else:
            continue
        previous = legacy_latest_by_case.get(case_id)
        if previous is None:
            legacy_latest_by_case[case_id] = (recorded_at, outcome_state)
        else:
            previous_state = previous[1]
            previous_terminal = previous_state in TERMINAL_CASE_STATES
            candidate_terminal = outcome_state in TERMINAL_CASE_STATES
            if previous_terminal and not candidate_terminal:
                legacy_latest_by_case[case_id] = (recorded_at, outcome_state)
            elif previous_terminal == candidate_terminal and recorded_at > previous[0]:
                legacy_latest_by_case[case_id] = (recorded_at, outcome_state)

    updated = 0
    terminal = 0
    nonterminal = 0
    all_case_ids = set(plans_by_case) | set(legacy_latest_by_case)
    missing_case_ids = {
        case_id for case_id in all_case_ids if not isinstance(cases.get(case_id), dict)
    }
    materialization_records: list[dict[str, Any]] = []
    materialization_context: dict[str, dict[str, Any]] = {}
    for case_id in sorted(missing_case_ids):
        if case_id in conflicting_case_identities:
            continue
        identities_by_revision = verified_identities_by_case.get(case_id, {})
        evidence_atom_ids = sorted(atom_ids_by_case.get(case_id, set()))
        if not identities_by_revision or not evidence_atom_ids:
            continue
        ordered_identities = sorted(
            identities_by_revision.items(),
            key=lambda item: (item[1].get("recorded_at", ""), item[0]),
        )
        selected_revision_id, selected = ordered_identities[-1]
        selected_identity_raw = selected.get("identity")
        if not isinstance(selected_identity_raw, dict):
            continue
        selected_identity = dict(selected_identity_raw)
        canonical_problem_id = _coerce_string(selected_identity.get("problem_id"))
        if canonical_problem_id is None:
            continue
        member_problem_ids = sorted(
            {
                problem_id
                for item in identities_by_revision.values()
                for identity in [item.get("identity")]
                if isinstance(identity, dict)
                for problem_id in [_coerce_string(identity.get("problem_id"))]
                if problem_id is not None
            }
        )
        fingerprints = sorted(
            {
                fingerprint
                for item in identities_by_revision.values()
                for identity in [item.get("identity")]
                if isinstance(identity, dict)
                for fingerprint in [_coerce_string(identity.get("fingerprint"))]
                if fingerprint is not None
            }
        )
        materialization_records.append(
            {
                "case_id": case_id,
                "problem_id": canonical_problem_id,
                "canonical_problem_id": canonical_problem_id,
                "case_member_problem_ids": member_problem_ids,
                "evidence_atom_ids": evidence_atom_ids,
                "source_evidence_atom_ids": evidence_atom_ids,
                "ticket_fingerprints": fingerprints,
                "case_state": "active",
                "problem_status": "identified",
                "root_cause_status": "unestablished",
            }
        )
        materialization_context[case_id] = {
            "schema_version": 1,
            "source": "verified_plan_target_contract",
            "context_status": "identity_only",
            "selected_plan_revision_id": selected_revision_id,
            "plan_revision_ids": sorted(identities_by_revision),
            "evidence_atom_ids": evidence_atom_ids,
            "verified_case_identity": selected_identity,
        }
    if materialization_records:
        rebuilt_registry = build_case_registry(
            materialization_records,
            previous=case_registry,
        )
        rebuilt_cases_raw = rebuilt_registry.get("cases")
        rebuilt_cases = rebuilt_cases_raw if isinstance(rebuilt_cases_raw, dict) else {}
        for case_id, context in materialization_context.items():
            raw_materialized_case = rebuilt_cases.get(case_id)
            if not isinstance(raw_materialized_case, dict):
                continue
            materialized_case = dict(raw_materialized_case)
            materialized_case["context_status"] = "identity_only"
            materialized_case["identity_materialization"] = context
            rebuilt_cases[case_id] = materialized_case
        rebuilt_registry["cases"] = rebuilt_cases
        case_registry.clear()
        case_registry.update(rebuilt_registry)
        cases = rebuilt_cases
    materialized_cases = len(materialization_context)
    unmaterializable_case_outcomes = len(missing_case_ids) - materialized_cases
    terminal_priority = {"superseded": 1, "duplicate": 2, "resolved": 3}
    for case_id in sorted(all_case_ids):
        raw_case = cases.get(case_id)
        if not isinstance(raw_case, dict):
            continue
        case_plans = plans_by_case.get(case_id, {})
        selected_revision_id: str | None = None
        selected_outcome: dict[str, Any] | None = None
        legacy_recorded_at: str | None = None
        if case_plans:
            required_plans = [
                (revision_id, outcome)
                for revision_id, outcome in case_plans.items()
                if outcome.get("required") is not False
            ]
            if not required_plans:
                case = dict(raw_case)
                case["plan_outcomes"] = case_plans
                cases[case_id] = case
                continue
            open_plans = [
                (revision_id, outcome)
                for revision_id, outcome in required_plans
                if outcome.get("state") not in TERMINAL_CASE_STATES
            ]
            if open_plans:
                selected_revision_id, selected = max(
                    open_plans,
                    key=lambda item: (
                        item[1].get("recorded_at", ""),
                        item[0],
                    ),
                )
                selected_outcome = selected
                outcome_state = selected.get("state", "planned")
            else:
                selected_revision_id, selected = max(
                    required_plans,
                    key=lambda item: (
                        terminal_priority.get(item[1].get("state", ""), 0),
                        item[1].get("recorded_at", ""),
                        item[0],
                    ),
                )
                selected_outcome = selected
                outcome_state = selected.get("state", "resolved")
        else:
            if case_id not in legacy_latest_by_case:
                continue
            legacy_recorded_at, outcome_state = legacy_latest_by_case[case_id]
        case = dict(raw_case)
        selected_recorded_state = outcome_state
        recurrence_reopen_raw = case.get("recurrence_reopen")
        recurrence_reopen = (
            recurrence_reopen_raw if isinstance(recurrence_reopen_raw, dict) else None
        )
        recurrence_reopened = bool(
            recurrence_reopen is not None
            and selected_revision_id is not None
            and outcome_state in TERMINAL_CASE_STATES
            and recurrence_reopen.get("against_plan_revision_id") == selected_revision_id
        )
        if recurrence_reopened:
            # A previously terminal outcome cannot suppress newer evidence that relation
            # review attached to the same canonical case. Keep the old outcome as
            # provenance, but reopen lifecycle state until a different plan revision earns
            # a new terminal outcome.
            outcome_state = "unverified"
        elif (
            recurrence_reopen is not None
            and selected_revision_id is not None
            and outcome_state in TERMINAL_CASE_STATES
            and recurrence_reopen.get("against_plan_revision_id") != selected_revision_id
        ):
            case.pop("recurrence_reopen", None)
        if _coerce_string(case.get("state")) != outcome_state:
            updated += 1
        case["state"] = outcome_state
        case["last_outcome_state"] = selected_recorded_state
        current_lifecycle: dict[str, Any] = {"state": outcome_state}
        if selected_revision_id is not None and selected_outcome is not None:
            verification_raw = selected_outcome.get("outcome_verification")
            verification = verification_raw if isinstance(verification_raw, dict) else None
            provenance_status = (
                str(verification.get("provenance_status"))
                if verification is not None
                else "fail_open_projection"
            )
            has_accepted_record = isinstance(selected_outcome.get("outcome_record"), dict)
            if has_accepted_record:
                outcome_source = (
                    "provenance_verified_plan_outcome"
                    if provenance_status == "verified"
                    else "structurally_valid_nonterminal_plan_outcome"
                )
            elif verification is not None and verification.get("structural_status") == "valid":
                outcome_source = "structurally_valid_unverified_plan_outcome"
            else:
                outcome_source = "atom_action_projection"
            outcome_reference = {
                "source": outcome_source,
                "validation_status": provenance_status,
                "plan_revision_id": selected_revision_id,
                "recorded_at": selected_outcome.get("recorded_at", ""),
                "path": selected_outcome.get("path", ""),
                "fingerprint": selected_outcome.get("fingerprint", ""),
            }
            if recurrence_reopened:
                outcome_reference["source"] = "same_class_recurrence_reopen"
                outcome_reference["recurrence_reopen"] = recurrence_reopen
            if verification is not None and verification.get("errors"):
                outcome_reference["verification_errors"] = verification.get("errors")
            current_lifecycle["outcome_reference"] = outcome_reference
            case["last_outcome_recorded_at"] = selected_outcome.get("recorded_at", "")
        elif legacy_recorded_at is not None:
            current_lifecycle["outcome_reference"] = {
                "source": "legacy_atom_action_projection",
                "validation_status": "projected",
                "recorded_at": legacy_recorded_at,
            }
            case["last_outcome_recorded_at"] = legacy_recorded_at
        case["current_lifecycle"] = current_lifecycle
        if case_plans:
            case["plan_outcomes"] = case_plans
        cases[case_id] = case
        if outcome_state in TERMINAL_CASE_STATES:
            terminal += 1
        else:
            nonterminal += 1
    case_registry["cases"] = cases
    return {
        "cases_updated": updated,
        "terminal_cases": terminal,
        "nonterminal_cases": nonterminal,
        "invalid_outcome_records": invalid_outcome_records,
        "provenance_failed_outcome_records": provenance_failed_outcome_records,
        "missing_case_outcomes": len(missing_case_ids),
        "materialized_cases": materialized_cases,
        "unmaterializable_case_outcomes": unmaterializable_case_outcomes,
        "conflicting_case_identities": len(conflicting_case_identities),
    }


def _outcome_trusted_runs_roots(
    *,
    primary_runs_dir: Path,
    configured_runs_dir: Path,
    implementation_runs_root: Path,
    additional_runs_roots: Sequence[Path] = (),
) -> tuple[Path, ...]:
    """Return the complete retained-evidence boundary for outcome verification."""

    return tuple(
        sorted(
            {
                primary_runs_dir.resolve(),
                configured_runs_dir.resolve(),
                implementation_runs_root.resolve(),
                *(path.resolve() for path in additional_runs_roots),
            },
            key=lambda path: str(path),
        )
    )


def _prepare_qualification_retained_records(
    records: Sequence[Mapping[str, Any]],
    *,
    source_manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Namespace one explicitly sealed retained root without path-derived identity.

    The physical root remains provenance, while the logical record identity is based
    on its canonical run coordinate and the content-sealed files beneath that run.
    The separately recorded root digest seals the complete selected root without making
    every atom ID churn when an unrelated run changes. Moving an unchanged retained run
    therefore preserves atom identity; two divergent copies cannot silently collapse.
    """

    root_raw = _coerce_string(source_manifest.get("root"))
    root_sha256 = _coerce_string(source_manifest.get("entries_sha256"))
    if root_raw is None or not _qualification_valid_sha256(root_sha256):
        raise ValueError("qualification_retained_evidence_manifest_invalid")

    semantic_manifest = (
        source_manifest.get("manifest_kind")
        == SEMANTIC_RUN_EVIDENCE_MANIFEST_KIND
    )
    run_receipts_by_rel: dict[str, str] = {}
    entries: list[dict[str, Any]] = []
    if semantic_manifest:
        run_receipts_raw = source_manifest.get("run_receipts")
        run_receipts = (
            run_receipts_raw if isinstance(run_receipts_raw, list) else []
        )
        for receipt in run_receipts:
            if not isinstance(receipt, Mapping):
                raise ValueError("qualification_retained_evidence_manifest_invalid")
            run_rel = _coerce_string(receipt.get("run_rel"))
            digest = _coerce_string(receipt.get("receipt_sha256"))
            if run_rel is None or not _qualification_valid_sha256(digest):
                raise ValueError("qualification_retained_evidence_manifest_invalid")
            if run_rel in run_receipts_by_rel:
                raise ValueError("qualification_retained_evidence_manifest_invalid")
            run_receipts_by_rel[run_rel] = digest
    else:
        entries_raw = source_manifest.get("entries")
        entries = (
            [dict(entry) for entry in entries_raw if isinstance(entry, Mapping)]
            if isinstance(entries_raw, list)
            else []
        )
    prepared: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for raw_record in records:
        record = dict(raw_record)
        original_run_rel = _coerce_string(record.get("run_rel"))
        if original_run_rel is None:
            raise ValueError("qualification_retained_evidence_run_rel_missing")
        if semantic_manifest:
            run_receipt_sha256 = run_receipts_by_rel.get(original_run_rel)
            if run_receipt_sha256 is None:
                raise ValueError("qualification_retained_evidence_run_receipt_missing")
        else:
            run_prefix = original_run_rel.replace("\\", "/").strip("/") + "/"
            run_entries = [
                {
                    **entry,
                    "path": str(entry.get("path") or "")[len(run_prefix) :],
                }
                for entry in entries
                if str(entry.get("path") or "").replace("\\", "/").startswith(
                    run_prefix
                )
            ]
            if not run_entries:
                raise ValueError("qualification_retained_evidence_run_receipt_missing")
            run_receipt_sha256 = _qualification_canonical_sha256(run_entries)
        record_identity = _qualification_canonical_sha256(
            {
                "source_kind": "retained_usertest_runs",
                "run_rel": original_run_rel,
                "run_receipt_sha256": run_receipt_sha256,
            }
        )
        namespaced_run_rel = f"__retained__/usertest/{record_identity}"
        prior = seen.get(namespaced_run_rel)
        if prior is not None and prior != original_run_rel:
            raise ValueError("qualification_retained_evidence_record_identity_conflict")
        seen[namespaced_run_rel] = original_run_rel
        record.update(
            {
                "run_rel": namespaced_run_rel,
                "retained_evidence_source_root": str(Path(root_raw).resolve()),
                "retained_evidence_source_root_sha256": root_sha256,
                "retained_evidence_source_run_rel": original_run_rel,
                "retained_evidence_source_run_sha256": run_receipt_sha256,
                "retained_evidence_source_record_sha256": record_identity,
            }
        )
        prepared.append(record)
    return prepared


def _annotate_qualification_retained_atoms(
    atoms: Sequence[Mapping[str, Any]],
    *,
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    records_by_run = {
        str(record["run_rel"]): record
        for record in records
        if _coerce_string(record.get("run_rel")) is not None
    }
    annotated: list[dict[str, Any]] = []
    for raw_atom in atoms:
        atom = dict(raw_atom)
        record = records_by_run.get(str(atom.get("run_rel") or ""), {})
        for key in (
            "retained_evidence_source_root",
            "retained_evidence_source_root_sha256",
            "retained_evidence_source_run_rel",
            "retained_evidence_source_run_sha256",
            "retained_evidence_source_record_sha256",
        ):
            value = record.get(key)
            if value is not None:
                atom[key] = value
        annotated.append(atom)
    return annotated


def _qualification_additional_source_roots(
    source_inputs: Mapping[str, Any],
) -> tuple[Path, ...]:
    manifests_raw = source_inputs.get("additional_evidence_runs")
    manifests = manifests_raw if isinstance(manifests_raw, list) else []
    roots = {
        Path(root).resolve()
        for item in manifests
        if isinstance(item, Mapping)
        for root in [_coerce_string(item.get("root"))]
        if root is not None
    }
    return tuple(sorted(roots, key=lambda path: str(path)))


def _qualification_correction_metrics(
    *,
    routes: Sequence[Mapping[str, Any]],
    result: Mapping[str, Any] | None,
) -> tuple[dict[str, int], Path | None, str | None]:
    metrics = {
        "correction_route_count": len(routes),
        "correction_attempt_count": 0,
        "correction_assessment_count": 0,
        "accepted_repair_count": 0,
        "accepted_repair_group_count": 0,
        "unresolved_route_count": len(routes),
        "pending_not_invoked_route_count": 0,
    }
    result_map = result if isinstance(result, Mapping) else {}
    consumption_path_raw = _coerce_string(result_map.get("correction_consumption_path"))
    consumption_path = (
        Path(consumption_path_raw).resolve() if consumption_path_raw is not None else None
    )
    consumption_sha256: str | None = None
    consumption: Mapping[str, Any] = {}
    if consumption_path is not None and consumption_path.is_file():
        loaded = _load_qualification_json_object(
            consumption_path,
            name="qualification_correction_metrics_consumption",
        )
        if not qualification_correction_consumption_errors(loaded):
            consumption = loaded
            consumption_sha256 = _coerce_string(loaded.get("content_sha256"))
    route_receipts_raw = consumption.get("route_receipts")
    route_receipts = (
        [item for item in route_receipts_raw if isinstance(item, Mapping)]
        if isinstance(route_receipts_raw, list)
        else []
    )
    metrics.update(
        {
            "correction_attempt_count": sum(
                len(item.get("attempts", []))
                for item in route_receipts
                if isinstance(item.get("attempts"), list)
            ),
            "correction_assessment_count": sum(
                len(item.get("assessments", []))
                for item in route_receipts
                if isinstance(item.get("assessments"), list)
            ),
        }
    )
    for field in (
        "accepted_repair_count",
        "accepted_repair_group_count",
        "unresolved_route_count",
        "pending_not_invoked_route_count",
    ):
        value = consumption.get(field, result_map.get(field))
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            metrics[field] = value
    return metrics, consumption_path, consumption_sha256


def _qualification_correction_history(
    *,
    qualification_meta: Mapping[str, Any],
    round_metrics: Mapping[str, int],
    disposition: str,
    result_status: str,
    failed_report_path: str | None,
    failed_report_sha256: str | None,
    correction_consumption_path: Path | None,
    correction_consumption_sha256: str | None,
    correction_failure_path: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Append one correction round and derive metrics across the full frontier.

    A repair can itself be re-adjudicated and corrected again.  Treating every
    fallback as round one loses evidence and makes a regressing second attempt look
    equivalent to a first attempt.  The retained history is already bound by the
    scored backlog/pending receipt, so extend it rather than replacing it.
    """

    prior_raw = qualification_meta.get("correction_history")
    prior = (
        [dict(item) for item in prior_raw if isinstance(item, Mapping)]
        if isinstance(prior_raw, list)
        else []
    )
    normalized_metrics = {
        key: int(value)
        for key, value in round_metrics.items()
        if isinstance(key, str)
        and key
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }
    entry_body = {
        "round": len(prior) + 1,
        "disposition": disposition,
        "result_status": result_status,
        "failed_report_path": failed_report_path,
        "failed_report_sha256": failed_report_sha256,
        "correction_consumption_path": (
            str(correction_consumption_path.resolve())
            if correction_consumption_path is not None
            else None
        ),
        "correction_consumption_sha256": correction_consumption_sha256,
        "correction_failure_path": correction_failure_path,
        "metrics": normalized_metrics,
    }
    entry = {
        **entry_body,
        "content_sha256": _qualification_canonical_sha256(entry_body),
    }
    history = [*prior, entry]
    metric_names = sorted(
        {
            key
            for item in history
            for metrics in [item.get("metrics")]
            if isinstance(metrics, Mapping)
            for key in metrics
            if isinstance(key, str) and key
        }
    )
    cumulative = {
        key: sum(
            int(metrics.get(key) or 0)
            for item in history
            for metrics in [item.get("metrics")]
            if isinstance(metrics, Mapping)
            and isinstance(metrics.get(key), int)
            and not isinstance(metrics.get(key), bool)
        )
        for key in metric_names
    }
    return history, cumulative


def _record_best_qualified_fallback(
    *,
    binding: Mapping[str, Any],
    current_qualification_meta: Mapping[str, Any],
    round_metrics: Mapping[str, int],
    result_status: str,
    correction_consumption_path: Path | None,
    correction_consumption_sha256: str | None,
    correction_failure_path: str | None,
    out_json: Path,
    repo_root: Path,
    repo_input: str | None,
    state_path: Path,
    policy_config_path: Path,
    export_gate_config_path: Path,
    shadow_gate_config: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """Record the best bound ancestor when a later repair cannot improve it."""

    binding_errors = best_qualified_fallback_errors(binding, verify_files=True)
    if binding_errors:
        raise ValueError("best_qualified_fallback_invalid:" + ",".join(binding_errors))
    report_path = Path(str(binding["report_path"])).resolve()
    report_wrapper = _load_qualification_json_object(
        report_path,
        name="best_qualified_fallback_report",
    )
    phase1_bundle = _load_qualification_json_object(
        Path(str(binding["phase1_bundle_path"])).resolve(),
        name="best_qualified_fallback_phase1_bundle",
    )
    bundle_artifacts_raw = phase1_bundle.get("artifacts")
    bundle_artifacts = bundle_artifacts_raw if isinstance(bundle_artifacts_raw, Mapping) else {}

    def bundled_path(name: str) -> Path | None:
        receipt = bundle_artifacts.get(name)
        raw = _coerce_string(receipt.get("snapshot_path")) if isinstance(receipt, Mapping) else None
        return Path(raw).resolve() if raw is not None else None

    report_raw = report_wrapper.get("report")
    if not isinstance(report_raw, Mapping):
        raise ValueError("best_qualified_fallback_report_invalid")
    report = dict(report_raw)
    qualification_raw = report.get("qualification")
    if not isinstance(qualification_raw, Mapping):
        raise ValueError("best_qualified_fallback_qualification_invalid")
    history, cumulative_metrics = _qualification_correction_history(
        qualification_meta=current_qualification_meta,
        round_metrics=round_metrics,
        disposition="best_qualified_ancestor_fallback",
        result_status=result_status,
        failed_report_path=_coerce_string(
            current_qualification_meta.get("latest_failed_adjudication_report_path")
        ),
        failed_report_sha256=_coerce_string(
            current_qualification_meta.get("latest_failed_adjudication_report_sha256")
        ),
        correction_consumption_path=correction_consumption_path,
        correction_consumption_sha256=correction_consumption_sha256,
        correction_failure_path=correction_failure_path,
    )
    qualification = dict(qualification_raw)
    qualification.update(
        {
            "clean_first_pass": False,
            "correction_required": True,
            "correction_metrics": cumulative_metrics,
            "best_qualified_fallback_used": True,
            "best_qualified_fallback_content_sha256": binding.get("content_sha256"),
        }
    )
    report["qualification"] = qualification

    source_backlog = _load_qualification_json_object(
        Path(str(binding["backlog_path"])).resolve(),
        name="best_qualified_fallback_backlog",
    )
    fallback_backlog = dict(source_backlog)
    artifacts_raw = fallback_backlog.get("artifacts")
    artifacts = dict(artifacts_raw) if isinstance(artifacts_raw, Mapping) else {}
    pipeline_raw = artifacts.get("six_stage_pipeline")
    pipeline = dict(pipeline_raw) if isinstance(pipeline_raw, Mapping) else {}
    for field, receipt_name in (
        ("problem_records_json", "problem_records"),
        ("problem_mining_evidence_json", "problem_mining_evidence"),
        ("prioritized_problems_json", "prioritized_problems"),
        ("research_json", "research"),
        ("solution_options_json", "solution_options"),
        ("solution_selection_json", "solution_selection"),
        ("change_plans_json", "change_plans"),
        ("case_registry_json", "case_registry"),
    ):
        path = bundled_path(receipt_name)
        if path is not None:
            pipeline[field] = str(path)
    atoms_snapshot = bundled_path("atoms")
    registry_snapshot = bundled_path("case_registry")
    if atoms_snapshot is not None:
        artifacts["atoms_jsonl"] = str(atoms_snapshot)
    if registry_snapshot is not None:
        artifacts["case_registry_json"] = str(registry_snapshot)
    artifacts["six_stage_pipeline"] = pipeline
    prompts_snapshot_raw = _coerce_string(phase1_bundle.get("prompts_snapshot_dir"))
    if prompts_snapshot_raw is not None:
        artifacts["prompts_dir"] = str(Path(prompts_snapshot_raw).resolve())
    source_shadow_raw = artifacts.get("shadow_qualification")
    source_shadow = dict(source_shadow_raw) if isinstance(source_shadow_raw, Mapping) else {}
    source_shadow.update(
        {
            "pending_adjudication": False,
            "qualification_status": qualification.get("status"),
            "qualification_passed": True,
            "qualification_failures": qualification.get("failures", []),
            "qualification_basis_sha256": report.get("qualification_basis_sha256"),
            "qualification_output_adjudication_path": binding.get("output_adjudication_path"),
            "qualification_output_adjudication_sha256_post_run": binding.get(
                "output_adjudication_sha256"
            ),
            "raw_first_pass_report_path": binding.get("report_path"),
            "raw_first_pass_report_sha256": binding.get("report_sha256"),
            "qualification_corpus_manifest_path": (
                str(bundled_path("qualification.corpus_manifest"))
                if bundled_path("qualification.corpus_manifest") is not None
                else source_shadow.get("qualification_corpus_manifest_path")
            ),
            "qualification_input_bundle_path": (
                str(bundled_path("qualification.input_bundle"))
                if bundled_path("qualification.input_bundle") is not None
                else source_shadow.get("qualification_input_bundle_path")
            ),
            "no_actionable_evidence_receipt_path": (
                str(bundled_path("qualification.no_actionable_receipt"))
                if bundled_path("qualification.no_actionable_receipt") is not None
                else None
            ),
            "pending_run_receipt_path": (
                phase1_bundle.get("immutable_pending_run", {}).get("path")
                if isinstance(phase1_bundle.get("immutable_pending_run"), Mapping)
                else source_shadow.get("pending_run_receipt_path")
            ),
            "clean_first_pass": False,
            "correction_required": True,
            "correction_metrics": cumulative_metrics,
            "correction_history": history,
            "best_qualified_fallback": dict(binding),
            "best_qualified_fallback_used": True,
            "best_qualified_fallback_selected_from": str(out_json.resolve()),
            "release_qualification_eligible": True,
            "useful_output_verified": True,
            "shadow_state_path": str(state_path.resolve()),
        }
    )
    artifacts["shadow_qualification"] = source_shadow
    export_contract_raw = artifacts.get("export_contract")
    export_contract = dict(export_contract_raw) if isinstance(export_contract_raw, Mapping) else {}
    fallback_policy_path = bundled_path("config.policy") or policy_config_path
    fallback_export_gate_path = bundled_path("config.export_gate") or export_gate_config_path
    export_contract.update(
        {
            "policy_config_path": str(fallback_policy_path.resolve()),
            "shadow_state_path": str(state_path.resolve()),
        }
    )
    artifacts["export_contract"] = export_contract
    fallback_backlog["artifacts"] = artifacts
    fallback_identity = _qualification_canonical_sha256(
        {
            "best_qualified_fallback_content_sha256": binding.get("content_sha256"),
            "correction_history_sha256s": [item.get("content_sha256") for item in history],
        }
    )
    fallback_root = out_json.parent / f"{out_json.stem}.qualified_raw_fallback" / fallback_identity
    fallback_json = fallback_root / "qualified_raw_fallback.backlog.json"
    fallback_md = fallback_root / "qualified_raw_fallback.backlog.md"
    write_backlog(
        fallback_backlog,
        out_json_path=fallback_json,
        out_md_path=fallback_md,
        title="Usertest Backlog (best independently qualified fallback)",
    )
    artifact_paths = _export_artifact_paths(
        backlog=fallback_backlog,
        backlog_path=fallback_json,
        repo_root=repo_root,
        policy_config_path=fallback_policy_path,
        export_gate_config_path=fallback_export_gate_path,
        cli_repo_input=repo_input,
    )
    state = record_shadow_cycle(
        state_path=state_path,
        backlog_path=fallback_json,
        invariant_report=report,
        artifact_paths=artifact_paths,
        generated_at=str(
            current_qualification_meta.get("scored_at")
            or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ),
        required_consecutive_cycles=shadow_gate_config["required_consecutive_shadow_cycles"],
        require_exact_export_projection=shadow_gate_config["require_exact_export_projection"],
    )
    return state, report, fallback_json


def _case_state_from_registry(case_registry: dict[str, Any], case_id: str | None) -> str | None:
    """Return a case lifecycle state without treating aliases as resolution."""

    if case_id is None:
        return None
    cases = case_registry.get("cases")
    if not isinstance(cases, dict):
        return None
    entry = cases.get(case_id)
    if not isinstance(entry, dict):
        return None
    return _coerce_string(entry.get("state")) or "active"


def _case_has_proven_terminal_outcome(
    case_registry: dict[str, Any],
    case_id: str | None,
) -> bool:
    """Return whether a terminal case is backed by provenance-verified evidence."""

    if case_id is None:
        return False
    cases_raw = case_registry.get("cases")
    cases = cases_raw if isinstance(cases_raw, dict) else {}
    entry_raw = cases.get(case_id)
    entry = entry_raw if isinstance(entry_raw, dict) else {}
    state = _coerce_string(entry.get("state"))
    lifecycle_raw = entry.get("current_lifecycle")
    lifecycle = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
    reference_raw = lifecycle.get("outcome_reference")
    reference = reference_raw if isinstance(reference_raw, dict) else {}
    return bool(
        state in TERMINAL_CASE_STATES
        and _coerce_string(lifecycle.get("state")) in {None, state}
        and _coerce_string(reference.get("validation_status")) == "verified"
    )


def _registry_case_id_for_atom(
    case_registry: dict[str, Any],
    atom_id: str | None,
) -> str | None:
    """Resolve the canonical case mapping retained by the runner-owned registry."""

    if atom_id is None:
        return None
    mapping_raw = case_registry.get("atom_id_to_case_id")
    mapping = mapping_raw if isinstance(mapping_raw, dict) else {}
    return _coerce_string(mapping.get(atom_id))


def _reset_stale_unproven_actioned_atoms(
    *,
    atom_actions: dict[str, dict[str, Any]],
    case_registry: dict[str, Any],
    current_plan_sync_at: str | None,
    generated_at: str,
) -> dict[str, int]:
    """Fail open legacy ``actioned`` rows whose plan and outcome disappeared.

    A historical status label is not resolution evidence. When a complete plan-folder
    scan finds no surviving plan and no provenance-verified terminal case, the atom is
    returned to ``new`` so its observed evidence can be researched again. IDEA intake
    records remain outside this automated remediation boundary.
    """

    if current_plan_sync_at is None:
        return {"examined": 0, "reset_to_new": 0, "idea_excluded": 0}
    cases_raw = case_registry.get("cases")
    cases = cases_raw if isinstance(cases_raw, dict) else {}
    examined = 0
    reset = 0
    idea_excluded = 0
    for entry in atom_actions.values():
        if _normalize_atom_status(_coerce_string(entry.get("status"))) != "actioned":
            continue
        examined += 1
        if atom_is_idea_originated(entry):
            idea_excluded += 1
            continue
        if _coerce_string(entry.get("last_plan_seen_at")) == current_plan_sync_at:
            continue
        case_id = _coerce_string(entry.get("case_id"))
        case_raw = cases.get(case_id) if case_id is not None else None
        case = case_raw if isinstance(case_raw, dict) else {}
        lifecycle_raw = case.get("current_lifecycle")
        lifecycle = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
        reference_raw = lifecycle.get("outcome_reference")
        reference = reference_raw if isinstance(reference_raw, dict) else {}
        case_state = _coerce_string(case.get("state"))
        if case and case_state not in TERMINAL_CASE_STATES:
            # A live canonical work unit remains the durable owner of this evidence.
            # Queue/action state must not detach or relabel it as a new problem.
            continue
        if (
            case_state in TERMINAL_CASE_STATES
            and _coerce_string(reference.get("validation_status")) == "verified"
        ):
            continue

        entry["stale_actioned_previous_status"] = "actioned"
        entry["stale_actioned_previous_case_id"] = case_id
        entry["stale_actioned_previous_disposition"] = _coerce_string(entry.get("disposition"))
        entry["stale_actioned_previous_supporting_case_ids"] = (
            list(entry.get("supporting_case_ids"))
            if isinstance(entry.get("supporting_case_ids"), list)
            else []
        )
        entry["status"] = "new"
        entry["stale_actioned_reset_at"] = generated_at
        entry["stale_actioned_reset_reason"] = (
            "no_surviving_plan_or_provenance_verified_terminal_outcome"
        )
        entry["disposition"] = "unresolved"
        entry["disposition_status"] = "pending"
        entry["disposition_rationale"] = (
            "The historical action label lacks a surviving plan or a "
            "provenance-verified terminal outcome and must be reconsidered."
        )
        entry.pop("disposition_receipt", None)
        entry.pop("supporting_case_ids", None)
        if not case:
            entry.pop("case_id", None)
            entry.pop("novel_case_rationale", None)
        reset += 1
    return {"examined": examined, "reset_to_new": reset, "idea_excluded": idea_excluded}


def _load_qualification_json_object(path: Path, *, name: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name}_unreadable:{type(exc).__name__}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{name}_invalid")
    return raw


def _load_qualification_atoms(path: Path) -> list[dict[str, Any]]:
    try:
        text_value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"qualification_atoms_unreadable:{type(exc).__name__}") from exc
    stripped = text_value.strip()
    if not stripped:
        return []
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        atoms: list[dict[str, Any]] = []
        for line_number, line in enumerate(text_value.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"qualification_atoms_jsonl_invalid:{line_number}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"qualification_atoms_row_invalid:{line_number}") from None
            atoms.append(item)
        return atoms
    if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
        return parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("atoms"), list):
        atoms_raw = parsed["atoms"]
        if all(isinstance(item, dict) for item in atoms_raw):
            return atoms_raw
    if isinstance(parsed, dict):
        return [parsed]
    raise ValueError("qualification_atoms_invalid")


_PHASE1_REQUIRED_ARTIFACT_NAMES = (
    "atoms",
    "problem_records",
    "problem_mining_evidence",
    "prioritized_problems",
    "research",
    "solution_options",
    "solution_selection",
    "change_plans",
    "case_registry",
)


def _qualification_canonical_sha256(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _qualification_json_from_bytes(content: bytes, *, name: str) -> Any:
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name}_unreadable:{type(exc).__name__}") from exc


def _qualification_atoms_from_bytes(content: bytes) -> list[dict[str, Any]]:
    text_value = content.decode("utf-8")
    stripped = text_value.strip()
    if not stripped:
        return []
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        atoms: list[dict[str, Any]] = []
        for line_number, line in enumerate(text_value.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"qualification_atoms_jsonl_invalid:{line_number}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"qualification_atoms_row_invalid:{line_number}") from None
            atoms.append(item)
        return atoms
    if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
        return parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("atoms"), list):
        atoms_raw = parsed["atoms"]
        if all(isinstance(item, dict) for item in atoms_raw):
            return atoms_raw
    if isinstance(parsed, dict):
        return [parsed]
    raise ValueError("qualification_atoms_invalid")


def _phase1_artifact_path_from_backlog(
    backlog: Mapping[str, Any],
    *,
    repo_root: Path,
    name: str,
) -> Path | None:
    artifacts_raw = backlog.get("artifacts")
    artifacts = artifacts_raw if isinstance(artifacts_raw, Mapping) else {}
    pipeline_raw = artifacts.get("six_stage_pipeline")
    pipeline = pipeline_raw if isinstance(pipeline_raw, Mapping) else {}
    raw_by_name = {
        "atoms": artifacts.get("atoms_jsonl"),
        "problem_records": pipeline.get("problem_records_json"),
        "problem_mining_evidence": pipeline.get("problem_mining_evidence_json"),
        "prioritized_problems": pipeline.get("prioritized_problems_json"),
        "research": pipeline.get("research_json"),
        "solution_options": pipeline.get("solution_options_json"),
        "solution_selection": pipeline.get("solution_selection_json"),
        "change_plans": pipeline.get("change_plans_json"),
        "case_registry": pipeline.get("case_registry_json") or artifacts.get("case_registry_json"),
    }
    raw = _coerce_string(raw_by_name.get(name))
    if raw is None:
        return None
    path = Path(raw)
    return (path if path.is_absolute() else repo_root / path).resolve()


def _phase1_snapshot_destination(
    *,
    snapshot_root: Path,
    name: str,
    source_path: Path,
    content_sha256: str,
) -> Path:
    if name == "pipeline.manifest":
        return snapshot_root / "repository" / "configs" / "backlog_prompts" / source_path.name
    if name.startswith("pipeline.prompt:"):
        relative = name.split(":", 1)[1]
        return snapshot_root / "repository" / "configs" / "backlog_prompts" / relative
    if name == "pipeline.stage_guidance_manifest":
        return (
            snapshot_root / "repository" / "configs" / "backlog_stage_guidance" / source_path.name
        )
    if name.startswith("pipeline.guidance:"):
        relative = name.split(":", 1)[1]
        return snapshot_root / "repository" / "configs" / "backlog_stage_guidance" / relative
    if name == "pipeline.taxonomy":
        return snapshot_root / "repository" / "configs" / "backlog_taxonomy.json"
    if name == "pipeline.relation_review_config":
        return snapshot_root / "repository" / "configs" / "backlog_relation_review.yaml"
    safe_name = "".join(character if character.isalnum() else "_" for character in name)
    return (
        snapshot_root
        / "artifacts"
        / f"{safe_name[:48]}-{sha256(name.encode('utf-8')).hexdigest()[:12]}"
        / content_sha256
        / source_path.name
    )


def _phase1_selected_artifact_names(
    artifact_paths: Mapping[str, Path | None],
) -> set[str]:
    selected = {
        *_PHASE1_REQUIRED_ARTIFACT_NAMES,
        "config.policy",
        "config.research",
        "config.export_gate",
        "qualification.corpus_manifest",
        "qualification.input_bundle",
        "qualification.no_actionable_receipt",
        "pipeline.manifest",
        "pipeline.stage_guidance_manifest",
        "pipeline.taxonomy",
        "pipeline.relation_review_config",
    }
    selected.update(
        name
        for name in artifact_paths
        if name.startswith("pipeline.prompt:") or name.startswith("pipeline.guidance:")
    )
    return selected


def _phase1_expected_receipts(pending: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    receipts_raw = pending.get("artifact_receipts")
    receipts = (
        [dict(item) for item in receipts_raw if isinstance(item, Mapping)]
        if isinstance(receipts_raw, list)
        else []
    )
    result: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        name = _coerce_string(receipt.get("name"))
        if name is None or name in result:
            raise ValueError("qualification_phase1_pending_receipts_invalid")
        result[name] = receipt
    return result


def _snapshot_phase1_qualification_bundle(
    *,
    backlog: Mapping[str, Any],
    backlog_path: Path,
    repo_root: Path,
    pending: Mapping[str, Any],
    artifact_paths: Mapping[str, Path | None],
    qualification_manifest_path: Path | None = None,
    qualification_output_adjudication_path: Path | None = None,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Copy the validated phase-one scoring inputs once and bind the copied bytes.

    The original pending receipt is validated before this function is called. Every copied
    source is then reconciled to that receipt, closing the mutation window between validation
    and use. Scoring and crash recovery consume only the returned in-memory bundle projection.
    """

    pending_sha256 = _coerce_string(pending.get("content_sha256"))
    if pending_sha256 is None:
        raise ValueError("qualification_phase1_pending_sha256_missing")
    snapshot_root = (
        backlog_path.parent / f"{backlog_path.stem}.qualification_pending_snapshot" / pending_sha256
    )
    expected_receipts = _phase1_expected_receipts(pending)
    backlog_bytes = backlog_path.read_bytes()
    backlog_sha256 = sha256(backlog_bytes).hexdigest()
    if pending.get("backlog_sha256") != backlog_sha256:
        raise ValueError("pending_shadow_phase1_snapshot_hash_mismatch")
    backlog_snapshot_path = snapshot_root / "backlog" / backlog_sha256 / "pending.backlog.json"
    _write_qualification_bytes_once(backlog_snapshot_path, backlog_bytes)

    selected_names = _phase1_selected_artifact_names(artifact_paths)
    snapshot_artifacts: dict[str, dict[str, Any]] = {}
    validation_artifact_paths: dict[str, Path | None] = {}
    loaded_bytes: dict[str, bytes | None] = {}
    for name in sorted(selected_names):
        source_path = artifact_paths.get(name)
        if source_path is None and name in _PHASE1_REQUIRED_ARTIFACT_NAMES:
            source_path = _phase1_artifact_path_from_backlog(
                backlog,
                repo_root=repo_root,
                name=name,
            )
        expected = expected_receipts.get(name)
        if name in _PHASE1_REQUIRED_ARTIFACT_NAMES and expected is None:
            raise ValueError(f"qualification_phase1_pending_receipt_missing:{name}")
        if expected is None:
            continue
        expected_exists = expected.get("exists") is True
        resolved_source = source_path.resolve() if isinstance(source_path, Path) else None
        if expected.get("source_path") != (
            str(resolved_source) if resolved_source is not None else None
        ):
            raise ValueError(f"qualification_phase1_source_path_mismatch:{name}")
        if not expected_exists:
            if resolved_source is not None and resolved_source.is_file():
                raise ValueError(f"qualification_phase1_unexpected_source_exists:{name}")
            validation_artifact_paths[name] = None
            loaded_bytes[name] = None
            snapshot_artifacts[name] = {
                "source_path": expected.get("source_path"),
                "snapshot_path": None,
                "exists": False,
                "sha256": None,
                "size_bytes": None,
            }
            continue
        if resolved_source is None or not resolved_source.is_file():
            raise ValueError(f"qualification_phase1_source_missing:{name}")
        content = resolved_source.read_bytes()
        content_sha256 = sha256(content).hexdigest()
        if expected.get("sha256") != content_sha256 or expected.get("size_bytes") != len(content):
            raise ValueError(f"qualification_phase1_source_changed_after_validation:{name}")
        destination = _phase1_snapshot_destination(
            snapshot_root=snapshot_root,
            name=name,
            source_path=resolved_source,
            content_sha256=content_sha256,
        )
        _write_qualification_bytes_once(destination, content)
        validation_artifact_paths[name] = destination
        loaded_bytes[name] = content
        snapshot_artifacts[name] = {
            "source_path": str(resolved_source),
            "snapshot_path": str(destination.resolve()),
            "exists": True,
            "sha256": content_sha256,
            "size_bytes": len(content),
        }

    # In the sealed transaction the manifest path and bytes are intentionally
    # unavailable during phase one. Phase two verifies the supplied bytes against
    # the pre-run digest and snapshots them only after model output is immutable.
    if qualification_manifest_path is not None:
        manifest_source = qualification_manifest_path.resolve()
        manifest_bytes = manifest_source.read_bytes()
        manifest_sha256 = sha256(manifest_bytes).hexdigest()
        expected_manifest_sha256 = _coerce_string(
            pending.get("qualification_manifest_sha256_expected")
        )
        if manifest_sha256 != expected_manifest_sha256:
            raise ValueError("qualification_phase2_manifest_digest_mismatch")
        manifest_snapshot_path = (
            snapshot_root
            / "qualification"
            / "corpus_manifest"
            / manifest_sha256
            / "qualification_manifest.json"
        )
        _write_qualification_bytes_once(manifest_snapshot_path, manifest_bytes)
        validation_artifact_paths["qualification.corpus_manifest"] = manifest_snapshot_path
        loaded_bytes["qualification.corpus_manifest"] = manifest_bytes
        snapshot_artifacts["qualification.corpus_manifest"] = {
            "source_path": str(manifest_source),
            "snapshot_path": str(manifest_snapshot_path.resolve()),
            "exists": True,
            "sha256": manifest_sha256,
            "size_bytes": len(manifest_bytes),
            "phase1_path_withheld": True,
        }

    adjudication_receipt: dict[str, Any] = {
        "source_path": (
            str(qualification_output_adjudication_path.resolve())
            if qualification_output_adjudication_path is not None
            else None
        ),
        "snapshot_path": None,
        "exists": False,
        "sha256": None,
        "size_bytes": None,
    }
    adjudication_bytes: bytes | None = None
    if qualification_output_adjudication_path is not None:
        adjudication_source = qualification_output_adjudication_path.resolve()
        adjudication_bytes = adjudication_source.read_bytes()
        adjudication_sha256 = sha256(adjudication_bytes).hexdigest()
        adjudication_path = (
            snapshot_root
            / "qualification"
            / "output_adjudication"
            / adjudication_sha256
            / "output_adjudication.json"
        )
        _write_qualification_bytes_once(adjudication_path, adjudication_bytes)
        adjudication_receipt.update(
            snapshot_path=str(adjudication_path.resolve()),
            exists=True,
            sha256=adjudication_sha256,
            size_bytes=len(adjudication_bytes),
        )

    immutable_pending_path = snapshot_root / "phase1.validation.pending.json"
    pending_build_path = (
        snapshot_root / ".pending-build" / uuid4().hex / "phase1.validation.pending.json"
    )
    immutable_pending = write_pending_shadow_run(
        pending_path=pending_build_path,
        backlog_path=backlog_snapshot_path,
        artifact_paths=validation_artifact_paths,
        qualification_manifest_sha256_expected=(
            _coerce_string(pending.get("qualification_manifest_sha256_expected"))
        ),
        output_adjudication_sha256_pre_run=(
            _coerce_string(pending.get("output_adjudication_sha256_pre_run"))
        ),
        generated_at=str(pending.get("generated_at") or "phase1-snapshot"),
    )
    pending_build_bytes = pending_build_path.read_bytes()
    _write_qualification_bytes_once(immutable_pending_path, pending_build_bytes)
    immutable_pending_loaded, immutable_pending_errors = validate_pending_shadow_run(
        pending_path=immutable_pending_path,
        backlog_path=backlog_snapshot_path,
        artifact_paths=validation_artifact_paths,
    )
    if immutable_pending_errors or immutable_pending_loaded is None:
        raise ValueError(
            "qualification_phase1_snapshot_pending_invalid:" + ",".join(immutable_pending_errors)
        )

    bundle: dict[str, Any] = {
        "schema_version": 1,
        "contract_kind": "qualification_phase1_bundle",
        "source_pending_run_sha256": pending_sha256,
        "backlog": {
            "source_path": str(backlog_path.resolve()),
            "snapshot_path": str(backlog_snapshot_path.resolve()),
            "sha256": backlog_sha256,
            "size_bytes": len(backlog_bytes),
        },
        "qualification_output_adjudication": adjudication_receipt,
        "artifacts": snapshot_artifacts,
        "source_artifact_sha256s": {
            name: snapshot_artifacts[name]["sha256"] for name in _PHASE1_REQUIRED_ARTIFACT_NAMES
        },
        "immutable_pending_run": {
            "path": str(immutable_pending_path.resolve()),
            "content_sha256": immutable_pending["content_sha256"],
            "file_sha256": sha256(immutable_pending_path.read_bytes()).hexdigest(),
        },
        "prompts_snapshot_dir": str(
            (snapshot_root / "repository" / "configs" / "backlog_prompts").resolve()
        ),
    }
    bundle["content_sha256"] = _qualification_canonical_sha256(bundle)
    bundle_path = snapshot_root / "bundle" / str(bundle["content_sha256"]) / "phase1.bundle.json"
    _write_qualification_json_once(bundle_path, bundle)
    loaded = {
        "bundle": bundle,
        "backlog": _qualification_json_from_bytes(
            backlog_bytes,
            name="qualification_phase1_backlog_snapshot",
        ),
        "artifact_bytes": loaded_bytes,
        "adjudication_bytes": adjudication_bytes,
        "validation_artifact_paths": validation_artifact_paths,
    }
    if not isinstance(loaded["backlog"], dict):
        raise ValueError("qualification_phase1_backlog_snapshot_invalid")
    return bundle_path, bundle, loaded


def _load_phase1_qualification_bundle(
    *,
    bundle_path: Path,
    expected_content_sha256: str,
) -> dict[str, Any]:
    bundle_bytes = bundle_path.read_bytes()
    bundle_raw = _qualification_json_from_bytes(
        bundle_bytes,
        name="qualification_phase1_bundle",
    )
    if not isinstance(bundle_raw, dict):
        raise ValueError("qualification_phase1_bundle_invalid")
    bundle = dict(bundle_raw)
    observed_content_sha256 = bundle.pop("content_sha256", None)
    if (
        bundle_raw.get("contract_kind") != "qualification_phase1_bundle"
        or observed_content_sha256 != expected_content_sha256
        or observed_content_sha256 != _qualification_canonical_sha256(bundle)
    ):
        raise ValueError("qualification_phase1_bundle_hash_invalid")
    bundle["content_sha256"] = observed_content_sha256

    backlog_receipt = bundle.get("backlog")
    if not isinstance(backlog_receipt, Mapping):
        raise ValueError("qualification_phase1_bundle_backlog_receipt_missing")
    backlog_snapshot = Path(str(backlog_receipt.get("snapshot_path"))).resolve()
    backlog_bytes = backlog_snapshot.read_bytes()
    if sha256(backlog_bytes).hexdigest() != backlog_receipt.get("sha256") or len(
        backlog_bytes
    ) != backlog_receipt.get("size_bytes"):
        raise ValueError("qualification_phase1_bundle_backlog_changed")
    backlog = _qualification_json_from_bytes(
        backlog_bytes,
        name="qualification_phase1_backlog_snapshot",
    )
    if not isinstance(backlog, dict):
        raise ValueError("qualification_phase1_backlog_snapshot_invalid")

    artifact_receipts_raw = bundle.get("artifacts")
    if not isinstance(artifact_receipts_raw, Mapping):
        raise ValueError("qualification_phase1_bundle_artifacts_missing")
    artifact_bytes: dict[str, bytes | None] = {}
    validation_artifact_paths: dict[str, Path | None] = {}
    for name, raw_receipt in artifact_receipts_raw.items():
        if not isinstance(name, str) or not isinstance(raw_receipt, Mapping):
            raise ValueError("qualification_phase1_bundle_artifact_receipt_invalid")
        exists = raw_receipt.get("exists") is True
        snapshot_path_raw = _coerce_string(raw_receipt.get("snapshot_path"))
        if not exists:
            if snapshot_path_raw is not None or raw_receipt.get("sha256") is not None:
                raise ValueError(f"qualification_phase1_bundle_absent_artifact_invalid:{name}")
            artifact_bytes[name] = None
            validation_artifact_paths[name] = None
            continue
        if snapshot_path_raw is None:
            raise ValueError(f"qualification_phase1_bundle_artifact_path_missing:{name}")
        snapshot_path = Path(snapshot_path_raw).resolve()
        content = snapshot_path.read_bytes()
        if sha256(content).hexdigest() != raw_receipt.get("sha256") or len(
            content
        ) != raw_receipt.get("size_bytes"):
            raise ValueError(f"qualification_phase1_bundle_artifact_changed:{name}")
        artifact_bytes[name] = content
        validation_artifact_paths[name] = snapshot_path

    source_hashes_raw = bundle.get("source_artifact_sha256s")
    source_hashes = source_hashes_raw if isinstance(source_hashes_raw, Mapping) else {}
    observed_source_hashes = {
        name: (
            artifact_receipts_raw.get(name, {}).get("sha256")
            if isinstance(artifact_receipts_raw.get(name), Mapping)
            else None
        )
        for name in _PHASE1_REQUIRED_ARTIFACT_NAMES
    }
    if dict(source_hashes) != observed_source_hashes:
        raise ValueError("qualification_phase1_bundle_source_hashes_invalid")

    adjudication_raw = bundle.get("qualification_output_adjudication")
    adjudication = adjudication_raw if isinstance(adjudication_raw, Mapping) else {}
    adjudication_bytes: bytes | None = None
    if adjudication.get("exists") is True:
        adjudication_path = Path(str(adjudication.get("snapshot_path"))).resolve()
        adjudication_bytes = adjudication_path.read_bytes()
        if sha256(adjudication_bytes).hexdigest() != adjudication.get("sha256") or len(
            adjudication_bytes
        ) != adjudication.get("size_bytes"):
            raise ValueError("qualification_phase1_bundle_adjudication_changed")

    immutable_pending_raw = bundle.get("immutable_pending_run")
    immutable_pending = immutable_pending_raw if isinstance(immutable_pending_raw, Mapping) else {}
    immutable_pending_path = Path(str(immutable_pending.get("path"))).resolve()
    immutable_pending_bytes = immutable_pending_path.read_bytes()
    if sha256(immutable_pending_bytes).hexdigest() != immutable_pending.get("file_sha256"):
        raise ValueError("qualification_phase1_bundle_pending_receipt_changed")
    validated_pending, pending_errors = validate_pending_shadow_run(
        pending_path=immutable_pending_path,
        backlog_path=backlog_snapshot,
        artifact_paths=validation_artifact_paths,
    )
    if (
        pending_errors
        or validated_pending is None
        or validated_pending.get("content_sha256") != immutable_pending.get("content_sha256")
    ):
        raise ValueError("qualification_phase1_bundle_pending_invalid:" + ",".join(pending_errors))
    return {
        "bundle": bundle,
        "backlog": backlog,
        "artifact_bytes": artifact_bytes,
        "adjudication_bytes": adjudication_bytes,
        "validation_artifact_paths": validation_artifact_paths,
    }


def _phase1_bundle_json_artifact(
    loaded_bundle: Mapping[str, Any],
    *,
    name: str,
    required: bool = True,
) -> Any:
    artifact_bytes = loaded_bundle.get("artifact_bytes")
    content = artifact_bytes.get(name) if isinstance(artifact_bytes, Mapping) else None
    if content is None:
        if required:
            raise ValueError(f"qualification_phase1_bundle_artifact_missing:{name}")
        return None
    return _qualification_json_from_bytes(content, name=f"qualification_phase1_{name}")


def _phase1_bundle_context(loaded_bundle: Mapping[str, Any]) -> dict[str, Any]:
    backlog_raw = loaded_bundle.get("backlog")
    if not isinstance(backlog_raw, Mapping):
        raise ValueError("qualification_phase1_bundle_backlog_missing")
    artifacts_raw = backlog_raw.get("artifacts")
    artifacts = dict(artifacts_raw) if isinstance(artifacts_raw, Mapping) else {}
    bundle_raw = loaded_bundle.get("bundle")
    bundle = bundle_raw if isinstance(bundle_raw, Mapping) else {}
    prompts_snapshot_dir = _coerce_string(bundle.get("prompts_snapshot_dir"))
    if prompts_snapshot_dir is not None and Path(prompts_snapshot_dir).is_dir():
        artifacts["prompts_dir"] = prompts_snapshot_dir

    documents: dict[str, dict[str, Any]] = {}
    for key, name in (
        ("stage1", "problem_records"),
        ("stage2", "prioritized_problems"),
        ("stage3", "research"),
        ("stage4", "solution_options"),
        ("stage5", "solution_selection"),
        ("stage6", "change_plans"),
        ("case_registry", "case_registry"),
    ):
        value = _phase1_bundle_json_artifact(loaded_bundle, name=name)
        if not isinstance(value, dict):
            raise ValueError(f"qualification_phase1_{name}_invalid")
        documents[key] = value
    return {
        "artifacts": artifacts,
        "atoms": _qualification_atoms_from_bytes(loaded_bundle["artifact_bytes"]["atoms"]),
        "qualification_manifest": _phase1_bundle_json_artifact(
            loaded_bundle,
            name="qualification.corpus_manifest",
            required=False,
        ),
        **documents,
    }


def _qualification_repair_context(
    *,
    backlog: Mapping[str, Any],
    repo_root: Path,
) -> dict[str, Any]:
    artifacts_raw = backlog.get("artifacts")
    artifacts = dict(artifacts_raw) if isinstance(artifacts_raw, Mapping) else {}
    pipeline_raw = artifacts.get("six_stage_pipeline")
    pipeline = dict(pipeline_raw) if isinstance(pipeline_raw, Mapping) else {}

    def artifact_path(value: Any, *, name: str) -> Path:
        raw = _coerce_string(value)
        if raw is None:
            raise ValueError(f"qualification_repair_artifact_path_missing:{name}")
        path = Path(raw)
        if not path.is_absolute():
            path = repo_root / path
        path = path.resolve()
        if not path.is_file():
            raise ValueError(f"qualification_repair_artifact_missing:{name}:{path}")
        return path

    return {
        "artifacts": artifacts,
        "atoms": _load_qualification_atoms(
            artifact_path(artifacts.get("atoms_jsonl"), name="atoms")
        ),
        "stage1": _load_qualification_json_object(
            artifact_path(pipeline.get("problem_records_json"), name="problem_records"),
            name="problem_records",
        ),
        "stage2": _load_qualification_json_object(
            artifact_path(
                pipeline.get("prioritized_problems_json"),
                name="prioritized_problems",
            ),
            name="prioritized_problems",
        ),
        "stage3": _load_qualification_json_object(
            artifact_path(pipeline.get("research_json"), name="research"),
            name="research",
        ),
        "stage4": _load_qualification_json_object(
            artifact_path(
                pipeline.get("solution_options_json"),
                name="solution_options",
            ),
            name="solution_options",
        ),
        "stage5": _load_qualification_json_object(
            artifact_path(
                pipeline.get("solution_selection_json"),
                name="solution_selection",
            ),
            name="solution_selection",
        ),
        "stage6": _load_qualification_json_object(
            artifact_path(pipeline.get("change_plans_json"), name="change_plans"),
            name="change_plans",
        ),
        "case_registry": _load_qualification_json_object(
            artifact_path(
                pipeline.get("case_registry_json") or artifacts.get("case_registry_json"),
                name="case_registry",
            ),
            name="case_registry",
        ),
    }


def _qualification_hashed_payload(
    payload: Mapping[str, Any],
    *,
    contract_kind: str,
    error_prefix: str,
) -> dict[str, Any]:
    value = dict(payload)
    if value.get("contract_kind") != contract_kind:
        raise ValueError(f"{error_prefix}_kind_invalid")
    observed = value.pop("content_sha256", None)
    if observed != _qualification_canonical_sha256(value):
        raise ValueError(f"{error_prefix}_hash_invalid")
    value["content_sha256"] = observed
    return value


def _load_qualification_execution_claim(
    path: Path,
    *,
    correction_input_sha256: str,
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = _qualification_hashed_payload(
        _load_qualification_json_object(path, name="qualification_correction_execution_claim"),
        contract_kind="qualification_correction_execution_claim",
        error_prefix="qualification_correction_execution_claim",
    )
    if value.get("correction_input_sha256") != correction_input_sha256:
        raise ValueError("qualification_correction_execution_claim_input_mismatch")
    return value


def _write_qualification_execution_claim(
    *,
    path: Path,
    correction_input_sha256: str,
    phase1_bundle_sha256: str,
    source_pending_run_sha256: str,
    source_adjudication_sha256: str,
    routes: Sequence[Mapping[str, Any]],
    attempt_dir: Path,
    recovery_kind: str,
) -> dict[str, Any]:
    claim: dict[str, Any] = {
        "schema_version": 1,
        "contract_kind": "qualification_correction_execution_claim",
        "correction_input_sha256": correction_input_sha256,
        "phase1_bundle_sha256": phase1_bundle_sha256,
        "source_pending_run_sha256": source_pending_run_sha256,
        "source_adjudication_sha256": source_adjudication_sha256,
        "route_sha256s": [route.get("route_sha256") for route in routes],
        "author_session_ids": [route.get("agent_session_id") for route in routes],
        "attempt_dir": str(attempt_dir.resolve()),
        "recovery_kind": recovery_kind,
    }
    claim["content_sha256"] = _qualification_canonical_sha256(claim)
    _write_qualification_json_once(path, claim)
    return claim


def _qualification_runtime_projection(
    runtime: QualificationRepairRuntimeResult,
) -> dict[str, Any]:
    return {
        "consumption": runtime.consumption,
        "stage_documents": runtime.stage_documents,
        "tickets": runtime.tickets,
        "affected_problem_ids": runtime.affected_problem_ids,
        "atoms": runtime.atoms,
        "case_registry": runtime.case_registry,
    }


def _qualification_runtime_from_projection(value: Any) -> QualificationRepairRuntimeResult:
    if not isinstance(value, Mapping):
        raise ValueError("qualification_scheduler_runtime_invalid")
    consumption = value.get("consumption")
    stage_documents = value.get("stage_documents")
    tickets = value.get("tickets")
    affected_problem_ids = value.get("affected_problem_ids")
    atoms = value.get("atoms")
    case_registry = value.get("case_registry")
    if (
        not isinstance(consumption, Mapping)
        or not isinstance(stage_documents, Mapping)
        or not isinstance(tickets, list)
        or not isinstance(affected_problem_ids, list)
        or (atoms is not None and not isinstance(atoms, list))
        or (case_registry is not None and not isinstance(case_registry, Mapping))
    ):
        raise ValueError("qualification_scheduler_runtime_invalid")
    return QualificationRepairRuntimeResult(
        consumption=dict(consumption),
        stage_documents={
            str(key): dict(document)
            for key, document in stage_documents.items()
            if isinstance(key, str) and isinstance(document, Mapping)
        },
        tickets=[dict(ticket) for ticket in tickets if isinstance(ticket, Mapping)],
        affected_problem_ids=[
            value for value in affected_problem_ids if isinstance(value, str) and value.strip()
        ],
        atoms=(
            [dict(atom) for atom in atoms if isinstance(atom, Mapping)]
            if isinstance(atoms, list)
            else None
        ),
        case_registry=(dict(case_registry) if isinstance(case_registry, Mapping) else None),
    )


def _qualification_scheduler_checkpoint_dir(completion_path: Path) -> Path:
    return completion_path.parent / "scheduler_checkpoints"


def _qualification_scheduler_checkpoint_path(
    completion_path: Path,
    content_sha256: str,
) -> Path:
    return _qualification_scheduler_checkpoint_dir(completion_path) / f"{content_sha256}.json"


def _write_qualification_scheduler_checkpoint(
    *,
    completion_path: Path,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {key: value for key, value in dict(state).items() if key != "content_sha256"}
    payload["content_sha256"] = _qualification_canonical_sha256(payload)
    _write_qualification_json_once(
        _qualification_scheduler_checkpoint_path(
            completion_path,
            str(payload["content_sha256"]),
        ),
        payload,
    )
    return payload


def _load_qualification_scheduler_checkpoint(
    *,
    completion_path: Path,
    correction_input_sha256: str,
) -> dict[str, Any] | None:
    checkpoint_dir = _qualification_scheduler_checkpoint_dir(completion_path)
    if not checkpoint_dir.is_dir():
        return None
    states: dict[str, dict[str, Any]] = {}
    for path in sorted(checkpoint_dir.glob("*.json")):
        value = _qualification_hashed_payload(
            _load_qualification_json_object(path, name="qualification_scheduler_checkpoint"),
            contract_kind="qualification_correction_scheduler_checkpoint",
            error_prefix="qualification_correction_scheduler_checkpoint",
        )
        content_sha256 = str(value["content_sha256"])
        if path.stem != content_sha256:
            raise ValueError("qualification_correction_scheduler_checkpoint_filename_mismatch")
        if value.get("correction_input_sha256") != correction_input_sha256:
            raise ValueError("qualification_correction_scheduler_checkpoint_input_mismatch")
        generation = value.get("generation")
        parent_sha256 = value.get("parent_checkpoint_sha256")
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
            or (parent_sha256 is not None and not isinstance(parent_sha256, str))
            or not isinstance(value.get("plan"), list)
            or not isinstance(value.get("group_states"), Mapping)
            or not isinstance(value.get("route_sha256s"), list)
        ):
            raise ValueError("qualification_correction_scheduler_checkpoint_payload_invalid")
        states[content_sha256] = value
    if not states:
        return None
    children: dict[str, list[str]] = {key: [] for key in states}
    roots: list[str] = []
    for content_sha256, state in states.items():
        parent = state.get("parent_checkpoint_sha256")
        if parent is None:
            if state.get("generation") != 0:
                raise ValueError("qualification_correction_scheduler_checkpoint_root_invalid")
            roots.append(content_sha256)
            continue
        if parent not in states:
            raise ValueError("qualification_correction_scheduler_checkpoint_parent_missing")
        if state.get("generation") != states[parent].get("generation", -1) + 1:
            raise ValueError("qualification_correction_scheduler_checkpoint_generation_invalid")
        children[parent].append(content_sha256)
    if len(roots) != 1 or any(len(items) > 1 for items in children.values()):
        raise ValueError("qualification_correction_scheduler_checkpoint_chain_ambiguous")
    current = roots[0]
    visited: set[str] = set()
    while True:
        if current in visited:
            raise ValueError("qualification_correction_scheduler_checkpoint_cycle")
        visited.add(current)
        next_items = children[current]
        if not next_items:
            break
        current = next_items[0]
    if visited != set(states):
        raise ValueError("qualification_correction_scheduler_checkpoint_chain_disconnected")
    return states[current]


def _qualification_scheduler_next_state(
    state: Mapping[str, Any],
) -> dict[str, Any]:
    copied = json.loads(json.dumps(dict(state), ensure_ascii=False))
    parent = copied.pop("content_sha256", None)
    copied["generation"] = int(copied.get("generation") or 0) + 1
    copied["parent_checkpoint_sha256"] = parent
    return copied


def _qualification_scheduler_initial_state(
    *,
    correction_input_sha256: str,
    routes: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    plan = plan_qualification_repair_route_groups(
        routes,
        stage1=context["stage1"],
        case_registry=(
            context["case_registry"] if isinstance(context.get("case_registry"), Mapping) else None
        ),
    )
    group_states: dict[str, dict[str, Any]] = {}
    route_by_sha = {str(route.get("route_sha256")): route for route in routes}
    for record in plan:
        group_id = str(record["group_id"])
        selected = record.get("disposition") == "selected_causal_frontier"
        invocable = record.get("invocable") is True
        route_statuses = {
            route_by_sha[route_sha].get("route_status")
            for route_sha in record.get("route_sha256s", [])
            if route_sha in route_by_sha
        }
        if selected and invocable:
            status = "pending_invocation"
        elif selected and route_statuses == {"uncorrectable"}:
            status = "terminal_nonprogress"
        elif selected:
            status = "repairable_paused:author_provenance_unavailable"
        else:
            status = "retained_pending_causal_predecessor"
        group_states[group_id] = {
            **dict(record),
            "status": status,
            "invocation_count": 0,
            "reconciliation_count": 0,
            "active_attempt": None,
            "latest_route_receipts": [],
            "receipt_history": [],
            "runtime_history": [],
        }
    state: dict[str, Any] = {
        "schema_version": 1,
        "contract_kind": "qualification_correction_scheduler_checkpoint",
        "correction_input_sha256": correction_input_sha256,
        "generation": 0,
        "parent_checkpoint_sha256": None,
        "route_sha256s": [str(route.get("route_sha256")) for route in routes],
        "route_bindings": {
            str(route.get("route_sha256")): {
                "route_status": route.get("route_status"),
                "authoring_stage": route.get("authoring_stage"),
                "agent_session_id": route.get("agent_session_id"),
                "workspace_dir": route.get("workspace_dir"),
            }
            for route in routes
        },
        "plan": [dict(record) for record in plan],
        "plan_sha256": _qualification_canonical_sha256(plan),
        "group_states": group_states,
        "current_runtime": None,
        "materialization_result": None,
    }
    return state


def _qualification_scheduler_group_routes(
    group: Mapping[str, Any],
    *,
    routes: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    expected = [str(value) for value in group.get("route_sha256s", [])]
    route_by_sha = {str(route.get("route_sha256")): route for route in routes}
    if any(value not in route_by_sha for value in expected):
        raise ValueError("qualification_correction_scheduler_group_route_missing")
    return [route_by_sha[value] for value in expected]


def _qualification_scheduler_resume_frontiers(
    group: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    receipts = group.get("latest_route_receipts")
    if not isinstance(receipts, list):
        return {}
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            continue
        status = str(receipt.get("status") or "")
        frontier = receipt.get("correction_frontier")
        route_sha256 = _coerce_string(receipt.get("route_sha256"))
        if (
            status.startswith("repairable_paused:")
            and isinstance(frontier, Mapping)
            and route_sha256 is not None
        ):
            return {route_sha256: dict(frontier)}
    return {}


def _qualification_scheduler_status_from_receipts(
    receipts: Sequence[Mapping[str, Any]],
    *,
    accepted_repair_count: int,
) -> str:
    if accepted_repair_count > 0:
        return "accepted"
    statuses = [str(receipt.get("status") or "") for receipt in receipts]
    repairable = next(
        (status for status in statuses if status.startswith("repairable_paused:")),
        None,
    )
    if repairable is not None:
        return repairable
    if any(status.startswith("retained_pending") for status in statuses):
        return "retained_pending_not_invoked"
    if statuses and all(
        status == "uncorrectable" or status.startswith("stalled:") for status in statuses
    ):
        return "terminal_nonprogress"
    return "repairable_paused:qualification_correction_status_unknown"


def _qualification_scheduler_apply_current_causal_plan(
    state: dict[str, Any],
    *,
    routes: Sequence[Mapping[str, Any]],
    runtime: QualificationRepairRuntimeResult,
    preserve_group_ids: set[str],
) -> None:
    """Block stale downstream authors after an accepted upstream graph change.

    The immutable initial plan remains the scheduler identity. This current plan is a
    monotonic safety refinement: it may newly retain a not-yet-invoked group, but never
    re-enable a route that an earlier accepted correction already made stale.
    """

    current_plan = plan_qualification_repair_route_groups(
        routes,
        stage1=runtime.stage_documents["problem_mining"],
        case_registry=runtime.case_registry,
    )
    groups = state.get("group_states")
    if not isinstance(groups, dict):
        raise ValueError("qualification_correction_scheduler_groups_invalid")
    for record in current_plan:
        group_id = str(record["group_id"])
        group_raw = groups.get(group_id)
        if not isinstance(group_raw, Mapping):
            raise ValueError("qualification_correction_scheduler_replan_group_missing")
        group = dict(group_raw)
        for field in (
            "component_id",
            "causal_tokens",
            "causal_rank",
            "blocked_by_group_id",
        ):
            group[field] = record.get(field)
        if (
            group_id not in preserve_group_ids
            and record.get("disposition") == "retained_pending_causal_predecessor"
            and group.get("status") not in {"accepted", "terminal_nonprogress"}
        ):
            group["disposition"] = "retained_pending_causal_predecessor"
            group["status"] = "retained_pending_causal_predecessor"
        groups[group_id] = group
    state["current_causal_plan"] = current_plan
    state["current_causal_plan_sha256"] = _qualification_canonical_sha256(current_plan)


def _qualification_scheduler_has_pending(state: Mapping[str, Any]) -> bool:
    groups = state.get("group_states")
    if not isinstance(groups, Mapping):
        return True
    for raw_group in groups.values():
        if not isinstance(raw_group, Mapping):
            return True
        status = str(raw_group.get("status") or "")
        if (
            status in {"pending_invocation", "invoking"}
            or status.startswith("repairable_paused:")
            or status.startswith("retained_pending")
        ):
            return True
    return False


def _qualification_scheduler_aggregate_runtime(
    *,
    state: Mapping[str, Any],
    context: Mapping[str, Any],
    backlog: Mapping[str, Any],
    routes: Sequence[Mapping[str, Any]],
    source_pending_run_sha256: str,
    source_adjudication_sha256: str,
) -> QualificationRepairRuntimeResult:
    current_runtime_raw = state.get("current_runtime")
    current_runtime = (
        _qualification_runtime_from_projection(current_runtime_raw)
        if isinstance(current_runtime_raw, Mapping)
        else None
    )
    groups_raw = state.get("group_states")
    groups = groups_raw if isinstance(groups_raw, Mapping) else {}
    receipt_by_route: dict[str, dict[str, Any]] = {}
    accepted_count = 0
    affected_problem_ids: set[str] = set()
    downstream_stages: list[str] = []
    downstream_results: list[tuple[int, dict[str, Any]]] = []
    for raw_group in groups.values():
        if not isinstance(raw_group, Mapping):
            continue
        receipts = raw_group.get("latest_route_receipts")
        if isinstance(receipts, list):
            for receipt in receipts:
                if isinstance(receipt, Mapping):
                    route_sha = _coerce_string(receipt.get("route_sha256"))
                    if route_sha is not None:
                        receipt_by_route[route_sha] = dict(receipt)
        if raw_group.get("status") == "accepted":
            accepted_count += len(raw_group.get("route_sha256s", []))
        histories = raw_group.get("runtime_history")
        if isinstance(histories, list):
            for history_index, runtime_history_raw in enumerate(histories):
                if not isinstance(runtime_history_raw, Mapping):
                    continue
                wrapped_runtime = runtime_history_raw.get("runtime")
                runtime_raw = (
                    wrapped_runtime if isinstance(wrapped_runtime, Mapping) else runtime_history_raw
                )
                sequence_raw = runtime_history_raw.get("scheduler_generation")
                sequence = (
                    int(sequence_raw)
                    if isinstance(sequence_raw, int) and not isinstance(sequence_raw, bool)
                    else history_index
                )
                runtime = _qualification_runtime_from_projection(runtime_raw)
                affected_problem_ids.update(runtime.affected_problem_ids)
                for stage in runtime.consumption.get("rerun_downstream_stages", []):
                    if isinstance(stage, str) and stage not in downstream_stages:
                        downstream_stages.append(stage)
                downstream = runtime.consumption.get("downstream_result")
                if isinstance(downstream, Mapping) and downstream:
                    downstream_results.append((sequence, dict(downstream)))
    for route in routes:
        route_sha = str(route.get("route_sha256"))
        if route_sha in receipt_by_route:
            continue
        group = next(
            (
                item
                for item in groups.values()
                if isinstance(item, Mapping) and route_sha in item.get("route_sha256s", [])
            ),
            {},
        )
        receipt = {
            "route_sha256": route_sha,
            "status": str(group.get("status") or "retained_pending_not_invoked"),
            "authored_work_disposition": "retained",
            "attempts": [],
            "assessments": [],
            "invocation_failures": [],
            "current_payload_sha256": None,
            "best_payload_sha256": None,
            "accepted_payload_sha256": None,
            "rerun_downstream_stages": [],
        }
        receipt["content_sha256"] = _qualification_canonical_sha256(receipt)
        receipt_by_route[route_sha] = receipt
    materialized_stage_receipts = next(
        (
            [dict(receipt) for receipt in result["materialized_stage_receipts"]]
            for _sequence, result in sorted(
                downstream_results,
                key=lambda item: item[0],
                reverse=True,
            )
            if isinstance(result.get("materialized_stage_receipts"), list)
            and result.get("materialized_stage_receipts")
            and all(
                isinstance(receipt, Mapping) for receipt in result["materialized_stage_receipts"]
            )
        ),
        [],
    )
    downstream_result = {
        "affected_problem_ids": sorted(affected_problem_ids),
        "requested_downstream_stages": downstream_stages,
        "materialized_stage_receipts": materialized_stage_receipts,
        "superseded_direct_repairs": [
            dict(item)
            for _sequence, result in sorted(downstream_results, key=lambda item: item[0])
            for item in result.get("superseded_direct_repairs", [])
            if isinstance(item, Mapping)
        ],
        "scheduler_group_statuses": {
            str(group_id): str(group.get("status") or "")
            for group_id, group in groups.items()
            if isinstance(group, Mapping)
        },
        "component_results": [
            {
                "scheduler_generation": sequence,
                "result": result,
            }
            for sequence, result in sorted(
                downstream_results,
                key=lambda item: item[0],
            )
        ],
    }
    consumption: dict[str, Any] = {
        "schema_version": 1,
        "contract_kind": "qualification_correction_consumption",
        "source_pending_run_sha256": source_pending_run_sha256,
        "source_adjudication_sha256": source_adjudication_sha256,
        "route_set_sha256": _qualification_canonical_sha256(
            [route.get("route_sha256") for route in routes]
        ),
        "route_receipts": [receipt_by_route[str(route.get("route_sha256"))] for route in routes],
        "accepted_repair_count": accepted_count,
        "accepted_repair_group_count": sum(
            1
            for group in groups.values()
            if isinstance(group, Mapping) and group.get("status") == "accepted"
        ),
        "unresolved_route_count": len(routes) - accepted_count,
        "pending_not_invoked_route_count": sum(
            len(group.get("route_sha256s", []))
            for group in groups.values()
            if isinstance(group, Mapping)
            and str(group.get("status") or "").startswith("retained_pending")
        ),
        "rerun_downstream_stages": downstream_stages,
        "downstream_result": downstream_result,
        "downstream_result_sha256": _qualification_canonical_sha256(downstream_result),
        "same_corpus_feedback_exposed": True,
        "release_qualification_eligible": False,
        "fresh_independent_readjudication_required": True,
    }
    consumption["content_sha256"] = _qualification_canonical_sha256(consumption)
    if current_runtime is None:
        return QualificationRepairRuntimeResult(
            consumption=consumption,
            stage_documents={
                stage: dict(context[key])
                for stage, key in (
                    ("problem_mining", "stage1"),
                    ("problem_prioritization", "stage2"),
                    ("repro_research", "stage3"),
                    ("solution_optioning", "stage4"),
                    ("solution_selection", "stage5"),
                    ("implementation_planning", "stage6"),
                )
            },
            tickets=[
                dict(ticket) for ticket in backlog.get("tickets", []) if isinstance(ticket, Mapping)
            ],
            affected_problem_ids=sorted(affected_problem_ids),
            atoms=[dict(atom) for atom in context["atoms"]],
            case_registry=(
                dict(context["case_registry"])
                if isinstance(context.get("case_registry"), Mapping)
                else None
            ),
        )
    return QualificationRepairRuntimeResult(
        consumption=consumption,
        stage_documents=current_runtime.stage_documents,
        tickets=current_runtime.tickets,
        affected_problem_ids=sorted(affected_problem_ids),
        atoms=current_runtime.atoms,
        case_registry=current_runtime.case_registry,
    )


def _routes_support_exact_session_reconciliation(
    routes: Sequence[Mapping[str, Any]],
) -> bool:
    return bool(routes) and all(
        route.get("route_status") == "same_author_resume"
        and _coerce_string(route.get("agent_session_id")) is not None
        and isinstance(route.get("author_provenance"), Mapping)
        and route["author_provenance"].get("exact_session_continuation") is True
        for route in routes
    )


@contextmanager
def _qualification_execution_lock(path: Path) -> Iterator[bool]:
    """Hold one nonblocking process lock for a correction identity.

    Unlike a persistent claim file, the OS releases this lock when a process exits. That lets
    crash recovery distinguish a live concurrent scorer from an abandoned claimed attempt
    without a wall-clock expiry or a duplicate fresh author invocation.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        acquired = False
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            acquired = True
            try:
                yield True
            finally:
                if acquired:
                    stream.seek(0)
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        # Closing the file descriptor also releases the process lock.
                        pass
            return

        import fcntl

        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        acquired = True
        try:
            yield True
        finally:
            if acquired:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass


def _execute_qualification_correction_locked(
    *,
    repo_root: Path,
    out_json: Path,
    backlog: dict[str, Any],
    context: Mapping[str, Any],
    routes: Sequence[Mapping[str, Any]],
    source_pending_run_sha256: str,
    source_adjudication_sha256: str,
    correction_input_sha256: str,
    completion_path: Path,
    phase1_bundle_sha256: str,
    qualification_manifest_path: Path,
    qualification_manifest_sha256: str,
    qualification_output_adjudication_path: Path | None,
    policy_config: BacklogPolicyConfig,
    policy_config_path: Path,
    export_gate_config_path: Path,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    repo_input: str | None,
    research_config: Mapping[str, Any],
    research_ref: str | None,
    replay_timeout_seconds: float,
) -> dict[str, Any]:
    prior_result = _load_qualification_correction_completion(
        path=completion_path,
        expected_input_sha256=correction_input_sha256,
    )
    if prior_result is not None:
        return {**prior_result, "correction_completion_reused": True}

    artifacts_raw = context.get("artifacts")
    artifacts = dict(artifacts_raw) if isinstance(artifacts_raw, Mapping) else {}
    prompts_dir_raw = _coerce_string(artifacts.get("prompts_dir"))
    if prompts_dir_raw is None:
        raise ValueError("qualification_repair_prompts_dir_missing")
    prompts_dir = Path(prompts_dir_raw)
    if not prompts_dir.is_absolute():
        prompts_dir = repo_root / prompts_dir
    pipeline_manifest = load_pipeline_prompt_manifest(prompts_dir.resolve())
    backlog_input_raw = backlog.get("input")
    backlog_input = backlog_input_raw if isinstance(backlog_input_raw, dict) else {}
    breadth_profile = (
        _coerce_string(artifacts.get("breadth_profile"))
        or _coerce_string(backlog_input.get("breadth_profile"))
        or "standard"
    )
    research_route_present = any(
        _coerce_string(route.get("authoring_stage"))
        in {"problem_mining", "problem_prioritization", "repro_research"}
        for route in routes
    )
    if research_route_present:
        replay_executor, replay_metadata = _configured_replay_executor(
            research_config=dict(research_config),
            repo_root=repo_root,
            repo_input=repo_input,
        )
    else:
        replay_executor = None
        replay_metadata = {}
    manifest = context.get("qualification_manifest")
    state = _load_qualification_scheduler_checkpoint(
        completion_path=completion_path,
        correction_input_sha256=correction_input_sha256,
    )
    if state is None:
        state = _write_qualification_scheduler_checkpoint(
            completion_path=completion_path,
            state=_qualification_scheduler_initial_state(
                correction_input_sha256=correction_input_sha256,
                routes=routes,
                context=context,
            ),
        )
    if state.get("route_sha256s") != [str(route.get("route_sha256")) for route in routes]:
        raise ValueError("qualification_correction_scheduler_routes_changed")
    expected_plan = plan_qualification_repair_route_groups(
        routes,
        stage1=context["stage1"],
        case_registry=(
            context["case_registry"] if isinstance(context.get("case_registry"), Mapping) else None
        ),
    )
    if (
        state.get("plan_sha256") != _qualification_canonical_sha256(expected_plan)
        or state.get("plan") != expected_plan
    ):
        raise ValueError("qualification_correction_scheduler_plan_changed")

    attempted_group_ids: set[str] = set()
    while True:
        groups_raw = state.get("group_states")
        groups = groups_raw if isinstance(groups_raw, Mapping) else {}
        selected_group: dict[str, Any] | None = None
        for plan_record in state.get("plan", []):
            if not isinstance(plan_record, Mapping):
                continue
            group_id = str(plan_record.get("group_id") or "")
            raw_group = groups.get(group_id)
            if not isinstance(raw_group, Mapping) or group_id in attempted_group_ids:
                continue
            status = str(raw_group.get("status") or "")
            if (
                raw_group.get("invocable") is True
                and raw_group.get("disposition") == "selected_causal_frontier"
                and (
                    status in {"pending_invocation", "invoking"}
                    or status.startswith("repairable_paused:")
                )
            ):
                selected_group = dict(raw_group)
                break
        if selected_group is None:
            break
        group_id = str(selected_group["group_id"])
        group_routes = _qualification_scheduler_group_routes(
            selected_group,
            routes=routes,
        )
        if not _routes_support_exact_session_reconciliation(group_routes):
            raise ValueError("qualification_correction_scheduler_invocable_group_invalid")
        active_attempt_raw = selected_group.get("active_attempt")
        active_attempt = (
            dict(active_attempt_raw) if isinstance(active_attempt_raw, Mapping) else None
        )
        invocation_number = (
            int(active_attempt["invocation_number"])
            if active_attempt is not None
            else int(selected_group.get("invocation_count") or 0) + 1
        )
        group_slug = group_id.rsplit(":", 1)[-1]
        claim_root = (
            completion_path.parent / "group_attempts" / group_slug / f"{invocation_number:04d}"
        )
        primary_claim_path = claim_root / "execution_claim.json"
        reconciliation_claim_path = claim_root / "reconciliation_claim.json"
        primary_claim = _load_qualification_execution_claim(
            primary_claim_path,
            correction_input_sha256=correction_input_sha256,
        )
        if primary_claim is None:
            attempt_dir = claim_root / "primary"
            _write_qualification_execution_claim(
                path=primary_claim_path,
                correction_input_sha256=correction_input_sha256,
                phase1_bundle_sha256=phase1_bundle_sha256,
                source_pending_run_sha256=source_pending_run_sha256,
                source_adjudication_sha256=source_adjudication_sha256,
                routes=group_routes,
                attempt_dir=attempt_dir,
                recovery_kind="primary",
            )
            recovery_kind = "primary"
        else:
            reconciliation_claim = _load_qualification_execution_claim(
                reconciliation_claim_path,
                correction_input_sha256=correction_input_sha256,
            )
            if reconciliation_claim is not None:
                return {
                    "status": "repairable_paused:qualification_correction_reconciliation_indeterminate",
                    "authored_work_disposition": "retained",
                    "execution_claim_path": str(primary_claim_path.resolve()),
                    "reconciliation_claim_path": str(reconciliation_claim_path.resolve()),
                    "qualification_scheduler_checkpoint_path": str(
                        _qualification_scheduler_checkpoint_path(
                            completion_path,
                            str(state["content_sha256"]),
                        ).resolve()
                    ),
                    "correction_completion_reused": False,
                    "fresh_author_invocation_suppressed": True,
                }
            attempt_dir = claim_root / "reconciliation"
            _write_qualification_execution_claim(
                path=reconciliation_claim_path,
                correction_input_sha256=correction_input_sha256,
                phase1_bundle_sha256=phase1_bundle_sha256,
                source_pending_run_sha256=source_pending_run_sha256,
                source_adjudication_sha256=source_adjudication_sha256,
                routes=group_routes,
                attempt_dir=attempt_dir,
                recovery_kind="exact_session_reconciliation",
            )
            recovery_kind = "exact_session_reconciliation"

        invoking_state = _qualification_scheduler_next_state(state)
        invoking_groups = invoking_state["group_states"]
        invoking_group = dict(invoking_groups[group_id])
        invoking_group["status"] = "invoking"
        invoking_group["active_attempt"] = {
            "invocation_number": invocation_number,
            "recovery_kind": recovery_kind,
            "execution_claim_path": str(primary_claim_path.resolve()),
            "reconciliation_claim_path": (
                str(reconciliation_claim_path.resolve())
                if recovery_kind == "exact_session_reconciliation"
                else None
            ),
        }
        if recovery_kind == "exact_session_reconciliation":
            invoking_group["reconciliation_count"] = (
                int(invoking_group.get("reconciliation_count") or 0) + 1
            )
        invoking_groups[group_id] = invoking_group
        state = _write_qualification_scheduler_checkpoint(
            completion_path=completion_path,
            state=invoking_state,
        )

        current_runtime_raw = state.get("current_runtime")
        current_runtime = (
            _qualification_runtime_from_projection(current_runtime_raw)
            if isinstance(current_runtime_raw, Mapping)
            else None
        )
        documents = (
            current_runtime.stage_documents
            if current_runtime is not None
            else {
                "problem_mining": context["stage1"],
                "problem_prioritization": context["stage2"],
                "repro_research": context["stage3"],
                "solution_optioning": context["stage4"],
                "solution_selection": context["stage5"],
                "implementation_planning": context["stage6"],
            }
        )
        runtime = run_stage456_qualification_repairs(
            routes=group_routes,
            source_pending_run_sha256=source_pending_run_sha256,
            source_adjudication_sha256=source_adjudication_sha256,
            repo_root=repo_root,
            atoms=(
                list(current_runtime.atoms)
                if current_runtime is not None and current_runtime.atoms is not None
                else list(context["atoms"])
            ),
            stage1=documents["problem_mining"],
            stage2=documents["problem_prioritization"],
            stage3=documents["repro_research"],
            stage4=documents["solution_optioning"],
            stage5=documents["solution_selection"],
            stage6=documents["implementation_planning"],
            pipeline_manifest=pipeline_manifest,
            repair_artifacts_dir=attempt_dir,
            agent=agent,
            model=model,
            cfg=cfg,
            breadth_profile=breadth_profile,
            repo_input=repo_input,
            research_ref=research_ref,
            replay_timeout_seconds=replay_timeout_seconds,
            replay_executor=replay_executor,
            replay_executor_metadata=replay_metadata,
            target_slug=(
                _coerce_string(backlog.get("scope", {}).get("target"))
                if isinstance(backlog.get("scope"), dict)
                else None
            ),
            case_registry=(
                current_runtime.case_registry
                if current_runtime is not None and current_runtime.case_registry is not None
                else context["case_registry"]
            ),
            qualification_manifest=(manifest if isinstance(manifest, dict) else None),
            resume_frontiers=_qualification_scheduler_resume_frontiers(selected_group),
        )
        runtime_external_wait_raw = runtime.consumption.get("external_wait")
        runtime_external_wait = (
            dict(runtime_external_wait_raw)
            if isinstance(runtime_external_wait_raw, Mapping)
            else None
        )
        if runtime_external_wait is not None:
            parked_state = _qualification_scheduler_next_state(state)
            parked_groups = parked_state["group_states"]
            parked_group = dict(parked_groups[group_id])
            parked_group["status"] = "repairable_paused:provider_external_wait"
            parked_group["active_attempt"] = None
            parked_group["external_wait"] = runtime_external_wait
            parked_group["invocation_count"] = invocation_number
            parked_groups[group_id] = parked_group
            parked_state["current_runtime"] = _qualification_runtime_projection(runtime)
            state = _write_qualification_scheduler_checkpoint(
                completion_path=completion_path,
                state=parked_state,
            )
            return {
                "status": "parked_external_wait",
                "authored_work_disposition": "retained",
                "external_wait": runtime_external_wait,
                "qualification_scheduler_checkpoint_path": str(
                    _qualification_scheduler_checkpoint_path(
                        completion_path,
                        str(state["content_sha256"]),
                    ).resolve()
                ),
                "fresh_author_invocation_suppressed": False,
                "api_fallback_allowed": False,
            }
        group_route_hashes = {str(route.get("route_sha256")) for route in group_routes}
        receipts = [
            dict(receipt)
            for receipt in runtime.consumption.get("route_receipts", [])
            if isinstance(receipt, Mapping)
            and str(receipt.get("route_sha256")) in group_route_hashes
        ]
        if {str(receipt.get("route_sha256")) for receipt in receipts} != group_route_hashes:
            raise ValueError("qualification_correction_scheduler_receipts_incomplete")
        completed_state = _qualification_scheduler_next_state(state)
        completed_groups = completed_state["group_states"]
        completed_group = dict(completed_groups[group_id])
        completed_group["status"] = _qualification_scheduler_status_from_receipts(
            receipts,
            accepted_repair_count=int(runtime.consumption.get("accepted_repair_count") or 0),
        )
        completed_group["invocation_count"] = invocation_number
        completed_group["active_attempt"] = None
        completed_group["latest_route_receipts"] = receipts
        completed_group["receipt_history"] = [
            *list(completed_group.get("receipt_history") or []),
            {
                "invocation_number": invocation_number,
                "recovery_kind": recovery_kind,
                "route_receipts": receipts,
            },
        ]
        runtime_projection = _qualification_runtime_projection(runtime)
        completed_group["runtime_history"] = [
            *list(completed_group.get("runtime_history") or []),
            {
                "scheduler_generation": completed_state["generation"],
                "runtime": runtime_projection,
            },
        ]
        completed_groups[group_id] = completed_group
        completed_state["current_runtime"] = runtime_projection
        completed_state["materialization_result"] = None
        if completed_group["status"] == "accepted" and any(
            _coerce_string(route.get("authoring_stage")) == "problem_mining"
            for route in group_routes
        ):
            _qualification_scheduler_apply_current_causal_plan(
                completed_state,
                routes=routes,
                runtime=runtime,
                preserve_group_ids={
                    group_id,
                    *{
                        str(existing_group_id)
                        for existing_group_id, existing_group in completed_groups.items()
                        if isinstance(existing_group, Mapping)
                        and existing_group.get("status")
                        in {
                            "accepted",
                            "terminal_nonprogress",
                        }
                    },
                },
            )
        state = _write_qualification_scheduler_checkpoint(
            completion_path=completion_path,
            state=completed_state,
        )
        attempted_group_ids.add(group_id)

    runtime = _qualification_scheduler_aggregate_runtime(
        state=state,
        context=context,
        backlog=backlog,
        routes=routes,
        source_pending_run_sha256=source_pending_run_sha256,
        source_adjudication_sha256=source_adjudication_sha256,
    )
    consumption_sha256 = str(runtime.consumption["content_sha256"])
    consumption_sidecar = out_json.with_name(
        f"{out_json.stem}.qualification_correction_consumption.{consumption_sha256}.json"
    )
    _write_qualification_json_once(consumption_sidecar, runtime.consumption)
    repair_result = materialize_repaired_shadow_run(
        source_backlog=backlog,
        source_backlog_path=out_json,
        atoms=list(context["atoms"]),
        runtime=runtime,
        repo_root=repo_root,
        repo_input=repo_input,
        policy_config=policy_config,
        policy_config_path=policy_config_path,
        export_gate_config_path=export_gate_config_path,
        qualification_manifest_path=qualification_manifest_path,
        qualification_manifest_sha256=qualification_manifest_sha256,
        qualification_output_adjudication_path=qualification_output_adjudication_path,
        qualification_output_adjudication_sha256=source_adjudication_sha256,
    ) or {
        "correction_consumption_path": str(consumption_sidecar.resolve()),
        "accepted_repair_count": 0,
        "unresolved_route_count": runtime.consumption.get("unresolved_route_count"),
        "fresh_independent_readjudication_required": False,
        "release_qualification_eligible": False,
    }
    pending = _qualification_scheduler_has_pending(state)
    accepted_repair_count = int(runtime.consumption.get("accepted_repair_count") or 0)
    scheduler_result = {
        **repair_result,
        "status": (
            repair_result.get("status")
            or (
                "corrected_pending_independent_readjudication"
                if accepted_repair_count > 0
                else (
                    "repairable_paused:qualification_correction_frontier_retained"
                    if pending
                    else "terminal_nonprogress"
                )
            )
        ),
        "qualification_scheduler_status": ("resumable_pending" if pending else "terminal"),
        "qualification_scheduler_checkpoint_path": str(
            _qualification_scheduler_checkpoint_path(
                completion_path,
                str(state["content_sha256"]),
            ).resolve()
        ),
        "qualification_scheduler_checkpoint_sha256": state["content_sha256"],
        "qualification_scheduler_pending": pending,
        "correction_completion_reused": False,
    }
    if pending:
        return scheduler_result
    completion = _build_qualification_correction_completion(
        correction_input_sha256=correction_input_sha256,
        consumption_path=consumption_sidecar,
        consumption_sha256=consumption_sha256,
        repair_result=scheduler_result,
    )
    terminal_completion_path = completion_path
    if completion_path.is_file():
        terminal_completion_path = completion_path.with_name(
            f"{completion_path.stem}.terminal.{completion['content_sha256']}"
            f"{completion_path.suffix}"
        )
    _write_qualification_json_once(terminal_completion_path, completion)
    return {
        **scheduler_result,
        "correction_completion_path": str(terminal_completion_path.resolve()),
    }


def _execute_qualification_correction(
    *,
    completion_path: Path,
    correction_input_sha256: str,
    **kwargs: Any,
) -> dict[str, Any]:
    lock_path = completion_path.parent / "execution.lock"
    with _qualification_execution_lock(lock_path) as acquired:
        if not acquired:
            return {
                "status": "correction_in_progress",
                "authored_work_disposition": "retained",
                "execution_lock_path": str(lock_path.resolve()),
                "correction_completion_reused": False,
                "fresh_author_invocation_suppressed": True,
            }
        # Completion and claim state are intentionally checked only after acquiring the lock.
        # A second scorer that observed an absent completion before the first scorer published it
        # must not proceed using that stale observation after the first releases the lock.
        return _execute_qualification_correction_locked(
            completion_path=completion_path,
            correction_input_sha256=correction_input_sha256,
            **kwargs,
        )


def _score_materialized_shadow_run(
    *,
    repo_root: Path,
    runs_dir: Path,
    out_json: Path,
    out_md: Path,
    repo_input: str | None,
    shadow_gate_config: Mapping[str, Any],
    qualification_manifest_path: Path | None,
    qualification_output_adjudication_path: Path | None,
    no_actionable_evidence_receipt_path: Path | None,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    research_config: Mapping[str, Any],
    research_ref: str | None,
    replay_timeout_seconds: float,
    qualification_input_bundle_path: Path | None = None,
    qualification_manifest_sha256_expected_override: str | None = None,
    state_path: Path | None = None,
) -> int:
    """Phase two: score an exact phase-one run without invoking model stages."""

    if not out_json.is_file():
        print(f"Pending shadow backlog is missing: {out_json}", file=sys.stderr)
        return 2
    state_path = state_path or shadow_state_path(out_json)
    correction_input_sha256_for_score: str | None = None
    correction_completion_path_for_score: Path | None = None
    correction_pending_path_for_score: Path | None = None
    repaired_child_contract_for_score: dict[str, Any] | None = None
    best_qualified_fallback_for_score: dict[str, Any] | None = None
    try:
        backlog = _load_qualification_json_object(out_json, name="pending_shadow_backlog")
        artifacts_raw = backlog.get("artifacts")
        artifacts = artifacts_raw if isinstance(artifacts_raw, dict) else {}
        qualification_raw = artifacts.get("shadow_qualification")
        qualification_meta = qualification_raw if isinstance(qualification_raw, dict) else {}
        recorded_bundle_path_raw = _coerce_string(
            qualification_meta.get("qualification_input_bundle_path")
        )
        recorded_bundle_sha256 = _coerce_string(
            qualification_meta.get("qualification_input_bundle_sha256")
        )
        repaired_same_corpus = bool(qualification_meta.get("same_corpus_feedback_exposed") is True)
        if recorded_bundle_path_raw is not None:
            if qualification_input_bundle_path is None and not repaired_same_corpus:
                raise ValueError("qualification_input_bundle_required_for_score")
            resolved_bundle_path = (
                qualification_input_bundle_path.resolve()
                if qualification_input_bundle_path is not None
                else Path(recorded_bundle_path_raw).resolve()
            )
            if recorded_bundle_path_raw != str(resolved_bundle_path):
                raise ValueError("qualification_input_bundle_path_changed")
            score_input_bundle = load_qualification_input_bundle(
                resolved_bundle_path,
                verify_files=True,
            )
            score_pipeline_raw = score_input_bundle.get("pipeline")
            score_pipeline = score_pipeline_raw if isinstance(score_pipeline_raw, Mapping) else {}
            score_manifest_raw = score_pipeline.get("files")
            score_manifest = score_manifest_raw if isinstance(score_manifest_raw, Mapping) else {}
            score_runtime_binding_errors = first_party_module_binding_errors(
                modules=sys.modules,
                repo_root=repo_root,
                pipeline_manifest=score_manifest,
            )
            if score_runtime_binding_errors:
                raise ValueError(
                    "qualification_score_runtime_imports_unsealed:"
                    + ",".join(score_runtime_binding_errors)
                )
            if (
                recorded_bundle_sha256 is None
                or score_input_bundle.get("content_sha256") != recorded_bundle_sha256
            ):
                raise ValueError("qualification_input_bundle_hash_changed")
            if repaired_same_corpus:
                repaired_contract_path_raw = _coerce_string(
                    qualification_meta.get("pending_repaired_run_receipt_path")
                )
                if repaired_contract_path_raw is None:
                    raise ValueError("qualification_repair_child_contract_missing")
                repaired_contract = _load_qualification_json_object(
                    Path(repaired_contract_path_raw).resolve(),
                    name="qualification_repair_child_contract",
                )
                repaired_contract_errors = pending_repaired_shadow_run_errors(repaired_contract)
                if repaired_contract_errors:
                    raise ValueError(
                        "qualification_repair_child_contract_invalid:"
                        + ",".join(repaired_contract_errors)
                    )
                repaired_child_contract_for_score = repaired_contract
                if repaired_contract.get("sealed_parent_bound") is not True:
                    raise ValueError("qualification_repair_child_contract_parent_unsealed")
                if (
                    repaired_contract.get("qualification_input_bundle_path")
                    != str(resolved_bundle_path)
                    or repaired_contract.get("qualification_input_bundle_sha256")
                    != recorded_bundle_sha256
                    or repaired_contract.get("repaired_backlog_sha256")
                    != _qualification_file_sha256(out_json)
                    or repaired_contract.get("shared_shadow_state_path")
                    != str(state_path.resolve())
                ):
                    raise ValueError("qualification_repair_child_contract_binding_mismatch")
                parent_contract_path_raw = _coerce_string(
                    repaired_contract.get("parent_cycle_contract_path")
                )
                if parent_contract_path_raw is None:
                    raise ValueError("qualification_repair_parent_contract_missing")
                parent_contract = _load_qualification_json_object(
                    Path(parent_contract_path_raw).resolve(),
                    name="qualification_repair_parent_contract",
                )
                parent_contract_projection = {
                    key: item for key, item in parent_contract.items() if key != "content_sha256"
                }
                if (
                    parent_contract.get("content_sha256")
                    != repaired_contract.get("parent_cycle_contract_sha256")
                    or parent_contract.get("content_sha256")
                    != _qualification_canonical_sha256(parent_contract_projection)
                    or parent_contract.get("qualification_input_bundle_sha256")
                    != recorded_bundle_sha256
                    or parent_contract.get("shadow_state_path") != str(state_path.resolve())
                ):
                    raise ValueError("qualification_repair_parent_contract_binding_mismatch")
                correction_consumption_path_raw = _coerce_string(
                    repaired_contract.get("correction_consumption_path")
                )
                if correction_consumption_path_raw is None:
                    raise ValueError("qualification_repair_consumption_path_missing")
                correction_consumption = _load_qualification_json_object(
                    Path(correction_consumption_path_raw).resolve(),
                    name="qualification_repair_consumption",
                )
                consumption_errors = qualification_correction_consumption_errors(
                    correction_consumption
                )
                if (
                    consumption_errors
                    or correction_consumption.get("content_sha256")
                    != repaired_contract.get("correction_consumption_sha256")
                    or correction_consumption.get("source_pending_run_sha256")
                    != repaired_contract.get("source_pending_run_sha256")
                    or correction_consumption.get("source_adjudication_sha256")
                    != repaired_contract.get("source_adjudication_sha256")
                ):
                    raise ValueError("qualification_repair_consumption_binding_mismatch")
                child_receipts_raw = repaired_contract.get("repaired_artifact_receipts")
                child_receipts = child_receipts_raw if isinstance(child_receipts_raw, list) else []
                for receipt in child_receipts:
                    if not isinstance(receipt, Mapping):
                        raise ValueError("qualification_repair_child_artifact_invalid")
                    source_path_raw = _coerce_string(receipt.get("source_path"))
                    source_path = (
                        Path(source_path_raw).resolve() if source_path_raw is not None else None
                    )
                    if receipt.get("exists") is True:
                        if (
                            source_path is None
                            or not source_path.is_file()
                            or receipt.get("sha256") != _qualification_file_sha256(source_path)
                            or receipt.get("size_bytes") != source_path.stat().st_size
                        ):
                            raise ValueError(
                                "qualification_repair_child_artifact_changed:"
                                + str(receipt.get("name") or "unknown")
                            )
                    elif source_path is not None and source_path.exists():
                        raise ValueError(
                            "qualification_repair_child_absent_artifact_created:"
                            + str(receipt.get("name") or "unknown")
                        )
        elif qualification_input_bundle_path is not None:
            raise ValueError("qualification_input_bundle_unexpected_for_legacy_score")
        if qualification_meta.get("pending_adjudication") is not True:
            completed_input_sha256 = _coerce_string(
                qualification_meta.get("qualification_correction_input_sha256")
            )
            completed_path_raw = _coerce_string(
                qualification_meta.get("qualification_correction_completion_path")
            )
            phase1_bundle_path_raw = _coerce_string(qualification_meta.get("phase1_bundle_path"))
            phase1_bundle_sha256 = _coerce_string(qualification_meta.get("phase1_bundle_sha256"))
            if phase1_bundle_path_raw is None or phase1_bundle_sha256 is None:
                raise ValueError("pending_shadow_run_not_waiting_for_adjudication")
            loaded_phase1_bundle = _load_phase1_qualification_bundle(
                bundle_path=Path(phase1_bundle_path_raw).resolve(),
                expected_content_sha256=phase1_bundle_sha256,
            )
            phase1_bundle = loaded_phase1_bundle["bundle"]
            if completed_input_sha256 is None and completed_path_raw is None:
                print(str(state_path))
                print(
                    json.dumps(
                        {
                            "shadow_invariants_passed": (
                                qualification_meta.get("qualification_passed") is True
                            ),
                            "ready_for_export": False,
                            "failures": list(
                                qualification_meta.get("qualification_failures") or []
                            ),
                            "qualification_correction": None,
                            "score_completion_reused": True,
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
                return 0 if qualification_meta.get("qualification_passed") is True else 3
            if completed_input_sha256 is None or completed_path_raw is None:
                raise ValueError("qualification_correction_identity_incomplete")
            prior_result = _load_qualification_correction_completion(
                path=Path(completed_path_raw).resolve(),
                expected_input_sha256=completed_input_sha256,
            )
            completion_was_reused = prior_result is not None
            if prior_result is None:
                pending_correction_path_raw = _coerce_string(
                    qualification_meta.get("qualification_correction_pending_path")
                )
                if pending_correction_path_raw is None:
                    raise ValueError("qualification_correction_completion_missing")
                pending_correction = _load_qualification_json_object(
                    Path(pending_correction_path_raw).resolve(),
                    name="pending_qualification_correction",
                )
                if (
                    pending_correction.get("contract_kind") != "pending_qualification_correction"
                    or pending_correction.get("correction_input_sha256") != completed_input_sha256
                    or pending_correction.get("content_sha256")
                    != _qualification_canonical_sha256(
                        {
                            key: value
                            for key, value in pending_correction.items()
                            if key != "content_sha256"
                        }
                    )
                ):
                    raise ValueError("pending_qualification_correction_invalid")
                recovery_routes_raw = pending_correction.get("correction_routes")
                recovery_routes = (
                    [item for item in recovery_routes_raw if isinstance(item, Mapping)]
                    if isinstance(recovery_routes_raw, list)
                    else []
                )
                if not recovery_routes:
                    raise ValueError("pending_qualification_correction_routes_missing")
                bundle_artifacts = phase1_bundle.get("artifacts")
                bundle_artifacts = bundle_artifacts if isinstance(bundle_artifacts, Mapping) else {}
                source_artifact_sha256s = phase1_bundle.get("source_artifact_sha256s")
                if not isinstance(source_artifact_sha256s, Mapping):
                    raise ValueError("qualification_recovery_source_hashes_missing")
                if (
                    pending_correction.get("phase1_bundle_path")
                    != str(Path(phase1_bundle_path_raw).resolve())
                    or pending_correction.get("phase1_bundle_sha256") != phase1_bundle_sha256
                    or pending_correction.get("source_pending_run_sha256")
                    != phase1_bundle.get("source_pending_run_sha256")
                    or pending_correction.get("phase1_backlog_snapshot_sha256")
                    != phase1_bundle.get("backlog", {}).get("sha256")
                    or pending_correction.get("source_artifact_sha256s")
                    != dict(source_artifact_sha256s)
                    or pending_correction.get("immutable_pending_run_sha256")
                    != phase1_bundle.get("immutable_pending_run", {}).get("content_sha256")
                ):
                    raise ValueError("qualification_recovery_bundle_binding_mismatch")

                manifest_receipt = bundle_artifacts.get("qualification.corpus_manifest")
                manifest_receipt = manifest_receipt if isinstance(manifest_receipt, Mapping) else {}
                adjudication_receipt = phase1_bundle.get("qualification_output_adjudication")
                adjudication_receipt = (
                    adjudication_receipt if isinstance(adjudication_receipt, Mapping) else {}
                )
                recovery_manifest_path_raw = _coerce_string(manifest_receipt.get("snapshot_path"))
                recovery_adjudication_path_raw = _coerce_string(
                    adjudication_receipt.get("snapshot_path")
                )
                recovery_manifest_sha256 = _coerce_string(manifest_receipt.get("sha256"))
                if (
                    recovery_manifest_path_raw is None
                    or recovery_adjudication_path_raw is None
                    or recovery_manifest_sha256 is None
                    or pending_correction.get("phase1_backlog_snapshot_path")
                    != phase1_bundle.get("backlog", {}).get("snapshot_path")
                    or pending_correction.get("qualification_manifest_snapshot_path")
                    != recovery_manifest_path_raw
                    or pending_correction.get("qualification_output_adjudication_snapshot_path")
                    != recovery_adjudication_path_raw
                    or pending_correction.get("immutable_pending_run_path")
                    != phase1_bundle.get("immutable_pending_run", {}).get("path")
                    or pending_correction.get("qualification_manifest_snapshot_sha256")
                    != recovery_manifest_sha256
                    or pending_correction.get("qualification_output_adjudication_snapshot_sha256")
                    != adjudication_receipt.get("sha256")
                    or pending_correction.get("source_adjudication_sha256")
                    != adjudication_receipt.get("sha256")
                ):
                    raise ValueError("qualification_recovery_snapshot_hash_mismatch")
                expected_correction_identity = _qualification_correction_identity(
                    source_pending_run_sha256=str(pending_correction["source_pending_run_sha256"]),
                    source_adjudication_sha256=str(
                        pending_correction["source_adjudication_sha256"]
                    ),
                    phase1_bundle_sha256=phase1_bundle_sha256,
                    qualification_manifest_sha256=recovery_manifest_sha256,
                    source_artifact_sha256s=source_artifact_sha256s,
                    routes=recovery_routes,
                )
                if expected_correction_identity != completed_input_sha256:
                    raise ValueError("qualification_recovery_identity_mismatch")
                recovery_context = _phase1_bundle_context(loaded_phase1_bundle)
                recovery_backlog = dict(loaded_phase1_bundle["backlog"])
                policy_receipt = bundle_artifacts.get("config.policy")
                export_gate_receipt = bundle_artifacts.get("config.export_gate")
                policy_receipt = policy_receipt if isinstance(policy_receipt, Mapping) else {}
                export_gate_receipt = (
                    export_gate_receipt if isinstance(export_gate_receipt, Mapping) else {}
                )
                recovery_policy_path_raw = _coerce_string(policy_receipt.get("snapshot_path"))
                recovery_export_gate_path_raw = _coerce_string(
                    export_gate_receipt.get("snapshot_path")
                )
                policy_bytes = loaded_phase1_bundle["artifact_bytes"].get("config.policy")
                if (
                    recovery_policy_path_raw is None
                    or recovery_export_gate_path_raw is None
                    or not isinstance(policy_bytes, bytes)
                ):
                    raise ValueError("qualification_recovery_policy_snapshot_missing")
                recovery_policy_loaded = yaml.safe_load(policy_bytes.decode("utf-8"))
                recovery_policy_root = (
                    recovery_policy_loaded if isinstance(recovery_policy_loaded, Mapping) else {}
                )
                prior_result = _execute_qualification_correction(
                    repo_root=repo_root,
                    out_json=out_json,
                    backlog=recovery_backlog,
                    context=recovery_context,
                    routes=recovery_routes,
                    source_pending_run_sha256=str(pending_correction["source_pending_run_sha256"]),
                    source_adjudication_sha256=str(
                        pending_correction["source_adjudication_sha256"]
                    ),
                    correction_input_sha256=completed_input_sha256,
                    completion_path=Path(completed_path_raw).resolve(),
                    phase1_bundle_sha256=phase1_bundle_sha256,
                    qualification_manifest_path=Path(recovery_manifest_path_raw).resolve(),
                    qualification_manifest_sha256=recovery_manifest_sha256,
                    qualification_output_adjudication_path=(
                        Path(recovery_adjudication_path_raw).resolve()
                    ),
                    policy_config=BacklogPolicyConfig.from_dict(
                        recovery_policy_root.get("backlog_policy", {})
                    ),
                    policy_config_path=Path(recovery_policy_path_raw).resolve(),
                    export_gate_config_path=Path(recovery_export_gate_path_raw).resolve(),
                    agent=agent,
                    model=model,
                    cfg=cfg,
                    repo_input=repo_input,
                    research_config=research_config,
                    research_ref=research_ref,
                    replay_timeout_seconds=replay_timeout_seconds,
                )
            print(str(state_path))
            print(
                json.dumps(
                    {
                        "shadow_invariants_passed": (
                            qualification_meta.get("qualification_passed") is True
                        ),
                        "ready_for_export": False,
                        "failures": list(qualification_meta.get("qualification_failures") or []),
                        "qualification_correction": {
                            **prior_result,
                            "correction_completion_reused": (
                                prior_result.get(
                                    "correction_completion_reused",
                                    completion_was_reused,
                                )
                            ),
                        },
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0 if qualification_meta.get("qualification_passed") is True else 3
        sealed_transaction = recorded_bundle_path_raw is not None and not repaired_same_corpus
        if sealed_transaction:
            if (
                _coerce_string(qualification_meta.get("qualification_corpus_manifest_path"))
                is not None
            ):
                raise ValueError("sealed_phase1_manifest_path_was_not_withheld")
            expected_manifest_sha256 = _coerce_string(
                qualification_meta.get("qualification_manifest_sha256_expected")
            )
            if (
                qualification_manifest_path is None
                or expected_manifest_sha256 is None
                or (
                    qualification_manifest_sha256_expected_override is not None
                    and qualification_manifest_sha256_expected_override != expected_manifest_sha256
                )
                or _qualification_file_sha256(qualification_manifest_path)
                != expected_manifest_sha256
            ):
                raise ValueError("qualification_phase2_manifest_digest_mismatch")
        if repaired_child_contract_for_score is not None and (
            qualification_manifest_path is None
            or repaired_child_contract_for_score.get("qualification_manifest_sha256")
            != _qualification_file_sha256(qualification_manifest_path)
        ):
            raise ValueError("qualification_repair_manifest_binding_mismatch")
        configured_paths = {
            "qualification_output_adjudication_path": (qualification_output_adjudication_path),
            "no_actionable_evidence_receipt_path": (no_actionable_evidence_receipt_path),
        }
        if not sealed_transaction:
            configured_paths["qualification_corpus_manifest_path"] = qualification_manifest_path
        for field, path in configured_paths.items():
            recorded = _coerce_string(qualification_meta.get(field))
            current = str(path) if path is not None else None
            if recorded != current:
                raise ValueError(f"pending_shadow_qualification_path_changed:{field}")
        export_contract_raw = artifacts.get("export_contract")
        export_contract = export_contract_raw if isinstance(export_contract_raw, dict) else {}
        policy_path_raw = _coerce_string(export_contract.get("policy_config_path"))
        if policy_path_raw is None:
            raise ValueError("pending_shadow_policy_path_missing")
        policy_config_path = Path(policy_path_raw).resolve()
        policy_root = _load_yaml(policy_config_path).get("backlog_policy", {})
        policy_cfg = BacklogPolicyConfig.from_dict(policy_root)
        artifact_paths = _export_artifact_paths(
            backlog=backlog,
            backlog_path=out_json,
            repo_root=repo_root,
            policy_config_path=policy_config_path,
            export_gate_config_path=repo_root / "configs" / "backlog_export_gate.yaml",
            cli_repo_input=repo_input,
        )
        pending_path_raw = _coerce_string(qualification_meta.get("pending_run_receipt_path"))
        pending_path = (
            Path(pending_path_raw).resolve()
            if pending_path_raw is not None
            else shadow_pending_run_path(out_json)
        )
        pending, pending_errors = validate_pending_shadow_run(
            pending_path=pending_path,
            backlog_path=out_json,
            artifact_paths=artifact_paths,
        )
        if pending_errors or pending is None:
            raise ValueError("pending_shadow_run_invalid:" + ",".join(pending_errors))
        if repaired_child_contract_for_score is not None and (
            repaired_child_contract_for_score.get("repaired_pending_run_sha256")
            != pending.get("content_sha256")
        ):
            raise ValueError("qualification_repair_pending_binding_mismatch")
        phase1_bundle_path, phase1_bundle, loaded_phase1_bundle = (
            _snapshot_phase1_qualification_bundle(
                backlog=backlog,
                backlog_path=out_json,
                repo_root=repo_root,
                pending=pending,
                artifact_paths=artifact_paths,
                qualification_manifest_path=qualification_manifest_path,
                qualification_output_adjudication_path=(qualification_output_adjudication_path),
            )
        )
        # From this point onward no scoring or correction input is reread from its mutable
        # phase-one source path. The copy bytes were reconciled to the validated receipt.
        backlog = dict(loaded_phase1_bundle["backlog"])
        artifacts_raw = backlog.get("artifacts")
        artifacts = artifacts_raw if isinstance(artifacts_raw, dict) else {}
        qualification_raw = artifacts.get("shadow_qualification")
        qualification_meta = qualification_raw if isinstance(qualification_raw, dict) else {}
        phase1_context = _phase1_bundle_context(loaded_phase1_bundle)
        atoms = list(phase1_context["atoms"])
        stage1 = dict(phase1_context["stage1"])
        stage2 = dict(phase1_context["stage2"])
        stage3 = dict(phase1_context["stage3"])
        stage4 = dict(phase1_context["stage4"])
        stage5 = dict(phase1_context["stage5"])
        stage6 = dict(phase1_context["stage6"])
        case_registry = dict(phase1_context["case_registry"])
        phase1_backlog_receipt = phase1_bundle["backlog"]
        phase1_snapshot_path = Path(str(phase1_backlog_receipt["snapshot_path"])).resolve()
        phase1_backlog_sha256 = str(phase1_backlog_receipt["sha256"])

        snapshot_artifacts = phase1_bundle["artifacts"]

        def snapshot_path(name: str, *, required: bool = True) -> Path | None:
            receipt = snapshot_artifacts.get(name)
            raw = (
                _coerce_string(receipt.get("snapshot_path"))
                if isinstance(receipt, Mapping)
                else None
            )
            if raw is None and required:
                raise ValueError(f"qualification_phase1_snapshot_missing:{name}")
            return Path(raw).resolve() if raw is not None else None

        manifest_snapshot_path = snapshot_path(
            "qualification.corpus_manifest",
            required=qualification_manifest_path is not None,
        )
        adjudication_receipt = phase1_bundle["qualification_output_adjudication"]
        adjudication_snapshot_raw = _coerce_string(adjudication_receipt.get("snapshot_path"))
        adjudication_snapshot_path = (
            Path(adjudication_snapshot_raw).resolve()
            if adjudication_snapshot_raw is not None
            else None
        )
        manifest = _phase1_bundle_json_artifact(
            loaded_phase1_bundle,
            name="qualification.corpus_manifest",
            required=False,
        )
        adjudication_bytes = loaded_phase1_bundle.get("adjudication_bytes")
        output_adjudication = (
            _qualification_json_from_bytes(
                adjudication_bytes,
                name="qualification_phase1_output_adjudication",
            )
            if isinstance(adjudication_bytes, bytes)
            else None
        )
        no_actionable_receipt = _phase1_bundle_json_artifact(
            loaded_phase1_bundle,
            name="qualification.no_actionable_receipt",
            required=False,
        )
        policy_config_snapshot_path = snapshot_path("config.policy")
        export_gate_snapshot_path = snapshot_path("config.export_gate")
        policy_bytes = loaded_phase1_bundle["artifact_bytes"].get("config.policy")
        if not isinstance(policy_bytes, bytes):
            raise ValueError("qualification_phase1_policy_snapshot_missing")
        policy_loaded = yaml.safe_load(policy_bytes.decode("utf-8"))
        policy_root = policy_loaded if isinstance(policy_loaded, Mapping) else {}
        policy_cfg = BacklogPolicyConfig.from_dict(policy_root.get("backlog_policy", {}))
        policy_config_path = policy_config_snapshot_path
        owner_roots_raw = qualification_meta.get("model_readable_roots")
        owner_roots = (
            tuple(
                Path(item).resolve()
                for item in owner_roots_raw
                if isinstance(item, str) and item.strip()
            )
            if isinstance(owner_roots_raw, list)
            else (repo_root,)
        )
        implementation_runs_root = inferred_implementation_runs_root(runs_dir)
        trusted_runs_roots = _outcome_trusted_runs_roots(
            primary_runs_dir=runs_dir,
            configured_runs_dir=cfg.runs_dir,
            implementation_runs_root=implementation_runs_root,
        )
        if qualification_meta.get("same_corpus_feedback_exposed") is True:
            source_scored_path_raw = _coerce_string(
                qualification_meta.get("source_scored_backlog_path")
            )
            if source_scored_path_raw is None:
                raise ValueError("qualification_repair_source_score_missing")
            source_scored = _load_qualification_json_object(
                Path(source_scored_path_raw).resolve(),
                name="qualification_repair_source_score",
            )
            source_artifacts_raw = source_scored.get("artifacts")
            source_artifacts = (
                source_artifacts_raw if isinstance(source_artifacts_raw, Mapping) else {}
            )
            source_qualification_raw = source_artifacts.get("shadow_qualification")
            source_qualification = (
                source_qualification_raw if isinstance(source_qualification_raw, Mapping) else {}
            )
            raw_report_path = _coerce_string(source_qualification.get("raw_first_pass_report_path"))
            raw_report_sha256 = _coerce_string(
                source_qualification.get("raw_first_pass_report_sha256")
            )
            if (
                raw_report_path is None
                or raw_report_sha256 is None
                or _qualification_file_sha256(Path(raw_report_path)) != raw_report_sha256
            ):
                raise ValueError("qualification_repair_first_pass_report_invalid")
            qualification_meta["first_pass_diagnostic"] = {
                "report_path": raw_report_path,
                "report_sha256": raw_report_sha256,
                "qualification_status": source_qualification.get("qualification_status"),
                "qualification_passed": source_qualification.get("qualification_passed"),
                "qualification_failures": source_qualification.get("qualification_failures"),
            }
            qualification_meta["raw_first_pass_report_path"] = raw_report_path
            qualification_meta["raw_first_pass_report_sha256"] = raw_report_sha256
            qualification_meta["raw_first_pass_report_content_sha256"] = source_qualification.get(
                "raw_first_pass_report_content_sha256"
            )
            artifacts["shadow_qualification"] = qualification_meta
            backlog["artifacts"] = artifacts
        report = evaluate_shadow_invariants(
            backlog=backlog,
            atoms=atoms,
            stage1=stage1,
            stage2=stage2,
            stage3=stage3,
            stage4=stage4,
            stage5=stage5,
            stage6=stage6,
            case_registry=case_registry,
            trusted_runs_roots=trusted_runs_roots,
            owner_roots=owner_roots,
            qualification_contract=shadow_gate_config,
            qualification_manifest=manifest,
            qualification_manifest_sha256_expected=pending.get(
                "qualification_manifest_sha256_expected"
            ),
            qualification_manifest_sha256_observed=(
                snapshot_artifacts.get("qualification.corpus_manifest", {}).get("sha256")
            ),
            qualification_output_adjudication=output_adjudication,
            qualification_output_adjudication_sha256_pre_run=pending.get(
                "output_adjudication_sha256_pre_run"
            ),
            qualification_output_adjudication_sha256_post_run=(adjudication_receipt.get("sha256")),
            qualification_pending_run_sha256=pending.get("content_sha256"),
            no_actionable_evidence_receipt=no_actionable_receipt,
        )
        export_projection = _build_export_projection(
            backlog=backlog,
            surface_area_high=set(policy_cfg.surface_area_high),
            cli_repo_input=repo_input,
            repo_root=repo_root,
        )
        report["export_projection_sha256"] = export_projection["sha256"]
        scored_correction_routes_raw = report.get("qualification", {}).get("correction_routes")
        all_scored_correction_routes = (
            [item for item in scored_correction_routes_raw if isinstance(item, Mapping)]
            if isinstance(scored_correction_routes_raw, list)
            else []
        )
        # The same author gets an opportunity to improve every adjudicated
        # finding. Aggregate-passing output remains a qualified immutable
        # fallback, so advisory repair cannot deadlock good-dominant throughput.
        scored_correction_routes = all_scored_correction_routes
        advisory_correction = bool(report["passed"] and scored_correction_routes)
        scored_adjudication_sha256 = _coerce_string(adjudication_receipt.get("sha256"))
        scored_manifest_sha256 = _coerce_string(
            snapshot_artifacts.get("qualification.corpus_manifest", {}).get("sha256")
        )
        phase1_source_artifact_sha256s = dict(phase1_bundle["source_artifact_sha256s"])
        if scored_correction_routes and isinstance(scored_adjudication_sha256, str):
            if scored_manifest_sha256 is None:
                raise ValueError("qualification_repair_manifest_binding_missing")
            correction_input_sha256_for_score = _qualification_correction_identity(
                source_pending_run_sha256=str(pending["content_sha256"]),
                source_adjudication_sha256=scored_adjudication_sha256,
                phase1_bundle_sha256=str(phase1_bundle["content_sha256"]),
                qualification_manifest_sha256=scored_manifest_sha256,
                source_artifact_sha256s=phase1_source_artifact_sha256s,
                routes=scored_correction_routes,
            )
            correction_completion_path_for_score = (
                out_json.parent
                / f"{out_json.stem}.qualification_correction_work"
                / correction_input_sha256_for_score
                / "completion.json"
            )
            correction_pending_path_for_score = (
                correction_completion_path_for_score.parent / "pending_correction.json"
            )
            pending_correction: dict[str, Any] = {
                "schema_version": 1,
                "contract_kind": "pending_qualification_correction",
                "correction_input_sha256": correction_input_sha256_for_score,
                "source_pending_run_sha256": pending["content_sha256"],
                "source_adjudication_sha256": scored_adjudication_sha256,
                "phase1_bundle_path": str(phase1_bundle_path.resolve()),
                "phase1_bundle_sha256": phase1_bundle["content_sha256"],
                "phase1_backlog_snapshot_path": str(phase1_snapshot_path.resolve()),
                "phase1_backlog_snapshot_sha256": phase1_backlog_sha256,
                "qualification_manifest_snapshot_path": (
                    str(manifest_snapshot_path.resolve())
                    if manifest_snapshot_path is not None
                    else None
                ),
                "qualification_manifest_snapshot_sha256": scored_manifest_sha256,
                "qualification_output_adjudication_snapshot_path": (
                    str(adjudication_snapshot_path.resolve())
                    if adjudication_snapshot_path is not None
                    else None
                ),
                "qualification_output_adjudication_snapshot_sha256": (scored_adjudication_sha256),
                "source_artifact_sha256s": phase1_source_artifact_sha256s,
                "immutable_pending_run_path": phase1_bundle["immutable_pending_run"]["path"],
                "immutable_pending_run_sha256": phase1_bundle["immutable_pending_run"][
                    "content_sha256"
                ],
                "correction_routes": [dict(route) for route in scored_correction_routes],
            }
            pending_correction["content_sha256"] = _qualification_canonical_sha256(
                pending_correction
            )
            _write_qualification_json_once(
                correction_pending_path_for_score,
                pending_correction,
            )
        raw_first_pass_report_path: Path | None = None
        raw_first_pass_report_sha256: str | None = None
        raw_first_pass_report_content_sha256: str | None = None
        if scored_correction_routes or (repaired_same_corpus and report.get("passed") is not True):
            raw_first_pass_report_body = {
                "schema_version": 1,
                "contract_kind": "qualification_raw_first_pass_report",
                "pending_run_sha256": pending.get("content_sha256"),
                "report": report,
            }
            raw_first_pass_report_content_sha256 = _qualification_canonical_sha256(
                raw_first_pass_report_body
            )
            raw_first_pass_report = {
                **raw_first_pass_report_body,
                "content_sha256": raw_first_pass_report_content_sha256,
            }
            raw_first_pass_report_path = out_json.with_name(
                f"{out_json.stem}.raw_first_pass.{raw_first_pass_report_content_sha256}.json"
            )
            _write_qualification_json_once(
                raw_first_pass_report_path,
                raw_first_pass_report,
            )
            raw_first_pass_report_sha256 = _qualification_file_sha256(raw_first_pass_report_path)
        prior_best_raw = qualification_meta.get("best_qualified_fallback")
        best_qualified_fallback_for_score = select_best_qualified_fallback(
            prior=(prior_best_raw if isinstance(prior_best_raw, Mapping) else None),
            candidate_backlog_path=(phase1_snapshot_path if report.get("passed") is True else None),
            candidate_report_path=(
                raw_first_pass_report_path if report.get("passed") is True else None
            ),
            candidate_output_adjudication_path=(
                adjudication_snapshot_path if report.get("passed") is True else None
            ),
            candidate_phase1_bundle_path=(
                phase1_bundle_path if report.get("passed") is True else None
            ),
        )
        qualification_meta.update(
            {
                "pending_adjudication": False,
                "scored_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "qualification_basis_sha256": report["qualification_basis_sha256"],
                "qualification_status": report["qualification"]["status"],
                "qualification_passed": report["passed"],
                "qualification_failures": report["qualification"]["failures"],
                "advisory_correction_route_count": (
                    len(scored_correction_routes) if advisory_correction else 0
                ),
                "clean_first_pass": bool(
                    report["passed"]
                    and not all_scored_correction_routes
                    and report["qualification"].get("correction_required") is not True
                ),
                "correction_required": bool(
                    all_scored_correction_routes
                    or report["qualification"].get("correction_required") is True
                ),
                "advisory_raw_fallback_eligible": advisory_correction,
                "correction_metrics": report["qualification"].get("correction_metrics", {}),
                "independent_release_evidence": report["qualification"].get(
                    "independent_release_evidence"
                ),
                "useful_output_verified": report["qualification"].get("useful_output_verified"),
                "release_qualification_eligible": bool(
                    report["passed"]
                    and report["qualification"].get("useful_output_verified") is True
                ),
                "best_qualified_fallback": best_qualified_fallback_for_score,
                "raw_first_pass_report_path": (
                    qualification_meta.get("original_first_pass_report_path")
                    or qualification_meta.get("raw_first_pass_report_path")
                    or (
                        str(raw_first_pass_report_path.resolve())
                        if raw_first_pass_report_path is not None
                        else None
                    )
                ),
                "raw_first_pass_report_sha256": (
                    qualification_meta.get("original_first_pass_report_sha256")
                    or qualification_meta.get("raw_first_pass_report_sha256")
                    or raw_first_pass_report_sha256
                ),
                "raw_first_pass_report_content_sha256": (
                    qualification_meta.get("raw_first_pass_report_content_sha256")
                    or raw_first_pass_report_content_sha256
                ),
                "original_first_pass_report_path": (
                    qualification_meta.get("original_first_pass_report_path")
                    or qualification_meta.get("raw_first_pass_report_path")
                    or (
                        str(raw_first_pass_report_path.resolve())
                        if raw_first_pass_report_path is not None
                        else None
                    )
                ),
                "original_first_pass_report_sha256": (
                    qualification_meta.get("original_first_pass_report_sha256")
                    or qualification_meta.get("raw_first_pass_report_sha256")
                    or raw_first_pass_report_sha256
                ),
                "latest_failed_adjudication_report_path": (
                    str(raw_first_pass_report_path.resolve())
                    if raw_first_pass_report_path is not None
                    else qualification_meta.get("latest_failed_adjudication_report_path")
                ),
                "latest_failed_adjudication_report_sha256": (
                    raw_first_pass_report_sha256
                    or qualification_meta.get("latest_failed_adjudication_report_sha256")
                ),
                "qualification_output_adjudication_sha256_post_run": (scored_adjudication_sha256),
                "phase1_bundle_path": str(phase1_bundle_path.resolve()),
                "phase1_bundle_sha256": phase1_bundle["content_sha256"],
                "phase1_backlog_snapshot_path": str(phase1_snapshot_path.resolve()),
                "phase1_backlog_snapshot_sha256": phase1_backlog_sha256,
                "phase1_pending_run_sha256": pending.get("content_sha256"),
                "phase1_source_artifact_sha256s": phase1_source_artifact_sha256s,
                "phase1_immutable_pending_run_path": phase1_bundle["immutable_pending_run"]["path"],
                "phase1_immutable_pending_run_sha256": phase1_bundle["immutable_pending_run"][
                    "content_sha256"
                ],
                "qualification_manifest_sha256_expected": pending.get(
                    "qualification_manifest_sha256_expected"
                ),
                "qualification_manifest_snapshot_path": (
                    str(manifest_snapshot_path.resolve())
                    if manifest_snapshot_path is not None
                    else None
                ),
                "qualification_output_adjudication_snapshot_path": (
                    str(adjudication_snapshot_path.resolve())
                    if adjudication_snapshot_path is not None
                    else None
                ),
                "qualification_correction_input_sha256": (correction_input_sha256_for_score),
                "qualification_correction_completion_path": (
                    str(correction_completion_path_for_score.resolve())
                    if correction_completion_path_for_score is not None
                    else None
                ),
                "qualification_correction_pending_path": (
                    str(correction_pending_path_for_score.resolve())
                    if correction_pending_path_for_score is not None
                    else None
                ),
            }
        )
        artifacts["shadow_qualification"] = qualification_meta
        backlog["artifacts"] = artifacts
        write_backlog(
            backlog,
            out_json_path=out_json,
            out_md_path=out_md,
            title="Usertest Backlog",
        )
        artifact_paths = _export_artifact_paths(
            backlog=backlog,
            backlog_path=out_json,
            repo_root=repo_root,
            policy_config_path=policy_config_path,
            export_gate_config_path=export_gate_snapshot_path,
            cli_repo_input=repo_input,
        )
        regressed_child_has_fallback = bool(
            repaired_same_corpus
            and report.get("passed") is not True
            and best_qualified_fallback_for_score is not None
        )
        if scored_correction_routes or regressed_child_has_fallback:
            # This is an intermediate system state, not the cycle's final output.
            # Its immutable diagnostic is retained above; only the independently
            # re-adjudicated repaired result participates in the release streak.
            state = {
                "ready_for_export": False,
                "consecutive_stable_passes": 0,
                "cycle_finalization_pending": True,
            }
        else:
            state = record_shadow_cycle(
                state_path=state_path,
                backlog_path=out_json,
                invariant_report=report,
                artifact_paths=artifact_paths,
                generated_at=qualification_meta["scored_at"],
                required_consecutive_cycles=shadow_gate_config[
                    "required_consecutive_shadow_cycles"
                ],
                require_exact_export_projection=shadow_gate_config[
                    "require_exact_export_projection"
                ],
            )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"[backlog] ERROR: shadow scoring failed: {exc}", file=sys.stderr)
        return 2

    repair_result: dict[str, Any] | None = None
    correction_routes = [dict(item) for item in scored_correction_routes]
    if correction_routes:
        if (
            correction_input_sha256_for_score is None
            or correction_completion_path_for_score is None
        ):
            raise RuntimeError("qualification_correction_identity_missing_after_score")
        prior_result = _load_qualification_correction_completion(
            path=correction_completion_path_for_score,
            expected_input_sha256=correction_input_sha256_for_score,
        )
        if prior_result is not None:
            repair_result = {
                **prior_result,
                "correction_completion_reused": True,
            }
        else:
            try:
                output_adjudication_sha256 = _coerce_string(
                    phase1_bundle.get("qualification_output_adjudication", {}).get("sha256")
                )
                manifest_sha256 = _coerce_string(
                    phase1_bundle.get("artifacts", {})
                    .get("qualification.corpus_manifest", {})
                    .get("sha256")
                )
                if output_adjudication_sha256 is None:
                    raise ValueError("qualification_repair_adjudication_sha256_missing")
                if (
                    manifest_sha256 is None
                    or manifest_snapshot_path is None
                    or adjudication_snapshot_path is None
                ):
                    raise ValueError("qualification_repair_manifest_binding_missing")
                repair_result = _execute_qualification_correction(
                    repo_root=repo_root,
                    out_json=out_json,
                    backlog=dict(loaded_phase1_bundle["backlog"]),
                    context=phase1_context,
                    routes=correction_routes,
                    source_pending_run_sha256=str(pending["content_sha256"]),
                    source_adjudication_sha256=output_adjudication_sha256,
                    correction_input_sha256=correction_input_sha256_for_score,
                    completion_path=correction_completion_path_for_score,
                    phase1_bundle_sha256=str(phase1_bundle["content_sha256"]),
                    qualification_manifest_path=manifest_snapshot_path,
                    qualification_manifest_sha256=manifest_sha256,
                    qualification_output_adjudication_path=(adjudication_snapshot_path),
                    policy_config=policy_cfg,
                    policy_config_path=policy_config_path,
                    export_gate_config_path=export_gate_snapshot_path,
                    agent=agent,
                    model=model,
                    cfg=cfg,
                    repo_input=repo_input,
                    research_config=research_config,
                    research_ref=research_ref,
                    replay_timeout_seconds=replay_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - preserve score and repair evidence
                failure_payload = {
                    "schema_version": 1,
                    "contract_kind": "qualification_correction_operational_failure",
                    "correction_input_sha256": correction_input_sha256_for_score,
                    "source_pending_run_sha256": pending.get("content_sha256"),
                    "source_adjudication_sha256": phase1_bundle.get(
                        "qualification_output_adjudication", {}
                    ).get("sha256"),
                    "phase1_bundle_sha256": phase1_bundle.get("content_sha256"),
                    "route_sha256s": [route.get("route_sha256") for route in correction_routes],
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "authored_work_disposition": "retained",
                }
                failure_payload["content_sha256"] = sha256(
                    json.dumps(
                        failure_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest()
                failure_path = out_json.with_name(
                    f"{out_json.stem}.qualification_correction_failure."
                    f"{failure_payload['content_sha256']}.json"
                )
                _write_qualification_json_once(failure_path, failure_payload)
                repair_result = {
                    "correction_failure_path": str(failure_path.resolve()),
                    "error": f"{type(exc).__name__}: {exc}",
                    "authored_work_disposition": "retained",
                }
                print(
                    f"[backlog] WARNING: qualification correction paused: {exc}",
                    file=sys.stderr,
                )

    repair_status = _coerce_string(
        repair_result.get("status") if isinstance(repair_result, Mapping) else None
    )
    repaired_backlog_path_raw = _coerce_string(
        repair_result.get("repaired_backlog_path") if isinstance(repair_result, Mapping) else None
    )
    repair_materialized = bool(
        repaired_backlog_path_raw is not None and Path(repaired_backlog_path_raw).is_file()
    )
    correction_in_progress = repair_status == "correction_in_progress"
    best_fallback_raw = qualification_meta.get("best_qualified_fallback")
    best_fallback = best_fallback_raw if isinstance(best_fallback_raw, Mapping) else None
    fallback_selected_path: Path | None = None
    should_use_best_fallback = bool(
        best_fallback is not None
        and not repair_materialized
        and not correction_in_progress
        and (advisory_correction or (repaired_same_corpus and report.get("passed") is not True))
    )
    if should_use_best_fallback and best_fallback is not None:
        try:
            (
                fallback_metrics,
                fallback_consumption_path,
                fallback_consumption_sha256,
            ) = _qualification_correction_metrics(
                routes=correction_routes,
                result=repair_result,
            )
            state, report, fallback_selected_path = _record_best_qualified_fallback(
                binding=best_fallback,
                current_qualification_meta=qualification_meta,
                round_metrics=fallback_metrics,
                result_status=(
                    repair_status
                    or ("no_correctable_route" if not correction_routes else "operational_failure")
                ),
                correction_consumption_path=fallback_consumption_path,
                correction_consumption_sha256=fallback_consumption_sha256,
                correction_failure_path=(
                    _coerce_string(repair_result.get("correction_failure_path"))
                    if isinstance(repair_result, Mapping)
                    else None
                ),
                out_json=out_json,
                repo_root=repo_root,
                repo_input=repo_input,
                state_path=state_path,
                policy_config_path=policy_config_path,
                export_gate_config_path=export_gate_snapshot_path,
                shadow_gate_config=shadow_gate_config,
            )
            repair_result = {
                **(dict(repair_result) if isinstance(repair_result, Mapping) else {}),
                "status": "best_qualified_ancestor_retained",
                "best_qualified_fallback_content_sha256": best_fallback.get("content_sha256"),
                "selected_backlog_path": str(fallback_selected_path.resolve()),
            }
        except (OSError, ValueError, yaml.YAMLError) as exc:
            print(
                f"[backlog] ERROR: best qualified fallback finalization failed: {exc}",
                file=sys.stderr,
            )
            return 2

    print(str(state_path))
    print(
        json.dumps(
            {
                "shadow_invariants_passed": report["passed"],
                "ready_for_export": state["ready_for_export"],
                "failures": report["failures"],
                "qualification_correction": repair_result,
                "selected_backlog_path": (
                    str(fallback_selected_path.resolve())
                    if fallback_selected_path is not None
                    else str(out_json.resolve())
                ),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] else 3


def _release_qualification_bundle_from_state(state_path: Path) -> Path:
    """Recover the release anchor's sealed runtime contract from shared custody."""

    state = _load_qualification_json_object(
        state_path,
        name="operational_release_shadow_state",
    )
    cycles_raw = state.get("cycles")
    cycles = cycles_raw if isinstance(cycles_raw, list) else []
    for cycle in reversed(cycles):
        if not isinstance(cycle, Mapping):
            continue
        qualification = cycle.get("qualification")
        if (
            cycle.get("cycle_mode") != "release"
            or cycle.get("passed") is not True
            or not isinstance(qualification, Mapping)
            or qualification.get("status") != "verified"
            or qualification.get("qualification_class") != "positive_throughput"
        ):
            continue
        receipts_raw = cycle.get("artifact_receipts")
        receipts = receipts_raw if isinstance(receipts_raw, list) else []
        receipt = next(
            (
                item
                for item in receipts
                if isinstance(item, Mapping)
                and item.get("name") == "qualification.input_bundle"
                and item.get("exists") is True
            ),
            None,
        )
        if receipt is None:
            continue
        path_raw = receipt.get("snapshot_path") or receipt.get("source_path")
        path = Path(path_raw).resolve() if isinstance(path_raw, str) and path_raw.strip() else None
        if path is not None and path.is_file():
            return path
    raise ValueError("operational_release_qualification_bundle_missing")


def _score_materialized_operational_shadow_run(
    *,
    repo_root: Path,
    runs_dir: Path,
    out_json: Path,
    repo_input: str | None,
    shadow_gate_config: Mapping[str, Any],
    state_path: Path,
) -> int:
    """Record fresh operational artifacts without self-certifying a release."""

    if not out_json.is_file():
        print(f"Pending operational backlog is missing: {out_json}", file=sys.stderr)
        return 2
    try:
        release_bundle_path = _release_qualification_bundle_from_state(state_path)
        release_bundle = load_qualification_input_bundle(
            release_bundle_path,
            verify_files=False,
        )
        runtime_compatibility_errors = qualification_runtime_compatibility_errors(
            release_bundle,
            repo_root=repo_root,
        )
        if runtime_compatibility_errors:
            raise ValueError(
                "operational_pipeline_runtime_changed_since_release:"
                + ",".join(runtime_compatibility_errors)
            )
        backlog = _load_qualification_json_object(out_json, name="pending_operational_backlog")
        artifacts_raw = backlog.get("artifacts")
        artifacts = artifacts_raw if isinstance(artifacts_raw, dict) else {}
        operational_raw = artifacts.get("operational_shadow")
        operational = operational_raw if isinstance(operational_raw, dict) else {}
        if operational.get("pending_internal_validation") is not True:
            raise ValueError("pending_operational_shadow_not_waiting_for_validation")

        export_contract_raw = artifacts.get("export_contract")
        export_contract = export_contract_raw if isinstance(export_contract_raw, dict) else {}
        policy_path_raw = _coerce_string(export_contract.get("policy_config_path"))
        if policy_path_raw is None:
            raise ValueError("pending_operational_shadow_policy_path_missing")
        policy_config_path = Path(policy_path_raw).resolve()
        policy_root = _load_yaml(policy_config_path).get("backlog_policy", {})
        policy_cfg = BacklogPolicyConfig.from_dict(policy_root)
        artifact_paths = _export_artifact_paths(
            backlog=backlog,
            backlog_path=out_json,
            repo_root=repo_root,
            policy_config_path=policy_config_path,
            export_gate_config_path=repo_root / "configs" / "backlog_export_gate.yaml",
            cli_repo_input=repo_input,
        )
        artifact_paths["qualification.input_bundle"] = release_bundle_path
        pending_path_raw = _coerce_string(operational.get("pending_run_receipt_path"))
        pending_path = (
            Path(pending_path_raw).resolve()
            if pending_path_raw is not None
            else operational_shadow_pending_run_path(out_json)
        )
        pending, pending_errors = validate_pending_operational_shadow_run(
            pending_path=pending_path,
            backlog_path=out_json,
            artifact_paths=artifact_paths,
        )
        if pending_errors or pending is None:
            raise ValueError("pending_operational_shadow_run_invalid:" + ",".join(pending_errors))

        pipeline_raw = artifacts.get("six_stage_pipeline")
        pipeline = pipeline_raw if isinstance(pipeline_raw, dict) else {}

        def artifact_path(value: Any, *, name: str) -> Path:
            raw = _coerce_string(value)
            if raw is None:
                raise ValueError(f"pending_operational_artifact_path_missing:{name}")
            path = Path(raw)
            if not path.is_absolute():
                path = repo_root / path
            path = path.resolve()
            if not path.is_file():
                raise ValueError(f"pending_operational_artifact_missing:{name}:{path}")
            return path

        atoms = _load_qualification_atoms(artifact_path(artifacts.get("atoms_jsonl"), name="atoms"))
        stage1 = _load_qualification_json_object(
            artifact_path(pipeline.get("problem_records_json"), name="problem_records"),
            name="problem_records",
        )
        stage2 = _load_qualification_json_object(
            artifact_path(pipeline.get("prioritized_problems_json"), name="prioritized_problems"),
            name="prioritized_problems",
        )
        stage3 = _load_qualification_json_object(
            artifact_path(pipeline.get("research_json"), name="research"),
            name="research",
        )
        stage4 = _load_qualification_json_object(
            artifact_path(pipeline.get("solution_options_json"), name="solution_options"),
            name="solution_options",
        )
        stage5 = _load_qualification_json_object(
            artifact_path(pipeline.get("solution_selection_json"), name="solution_selection"),
            name="solution_selection",
        )
        stage6 = _load_qualification_json_object(
            artifact_path(pipeline.get("change_plans_json"), name="change_plans"),
            name="change_plans",
        )
        case_registry = _load_qualification_json_object(
            artifact_path(
                pipeline.get("case_registry_json") or artifacts.get("case_registry_json"),
                name="case_registry",
            ),
            name="case_registry",
        )
        owner_roots_raw = operational.get("model_readable_roots")
        owner_roots = (
            tuple(
                Path(item).resolve()
                for item in owner_roots_raw
                if isinstance(item, str) and item.strip()
            )
            if isinstance(owner_roots_raw, list)
            else (repo_root,)
        )
        implementation_runs_root = inferred_implementation_runs_root(runs_dir)
        trusted_runs_roots = _outcome_trusted_runs_roots(
            primary_runs_dir=runs_dir,
            configured_runs_dir=runs_dir,
            implementation_runs_root=implementation_runs_root,
        )
        report = evaluate_shadow_invariants(
            backlog=backlog,
            atoms=atoms,
            stage1=stage1,
            stage2=stage2,
            stage3=stage3,
            stage4=stage4,
            stage5=stage5,
            stage6=stage6,
            case_registry=case_registry,
            trusted_runs_roots=trusted_runs_roots,
            owner_roots=owner_roots,
            qualification_contract=shadow_gate_config,
            cycle_mode="operational",
        )
        export_projection = _build_export_projection(
            backlog=backlog,
            surface_area_high=set(policy_cfg.surface_area_high),
            cli_repo_input=repo_input,
            repo_root=repo_root,
        )
        report["export_projection_sha256"] = export_projection["sha256"]
        scored_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = record_shadow_cycle(
            state_path=state_path,
            backlog_path=out_json,
            invariant_report=report,
            artifact_paths=artifact_paths,
            generated_at=scored_at,
            required_consecutive_cycles=shadow_gate_config["required_consecutive_shadow_cycles"],
            require_exact_export_projection=shadow_gate_config["require_exact_export_projection"],
        )
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"[backlog] ERROR: operational shadow scoring failed: {exc}", file=sys.stderr)
        return 2

    print(str(state_path))
    print(
        json.dumps(
            {
                "operational_invariants_passed": report["passed"],
                "ready_for_export": state["ready_for_export"],
                "activation_mode": state.get("activation_mode"),
                "release_anchor_cycle_ids": state.get("release_anchor_cycle_ids"),
                "failures": report["failures"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] and state["ready_for_export"] else 3


def _cmd_reports_backlog(args: argparse.Namespace) -> int:
    """Execute the `reports backlog` command handler.

    Parameters
    ----------
    args:
        Parsed command-line arguments namespace.

    Returns
    -------
    int
        Process exit code.
    """
    shadow = bool(getattr(args, "shadow", False))
    score_shadow = bool(getattr(args, "score_shadow", False))
    operational_shadow = bool(getattr(args, "operational_shadow", False))
    score_operational_shadow = bool(getattr(args, "score_operational_shadow", False))
    qualification_prepare_out_raw = getattr(args, "qualification_prepare_out", None)
    qualification_prepare = isinstance(qualification_prepare_out_raw, Path)
    qualification_input_bundle_raw = getattr(args, "qualification_input_bundle", None)
    qualification_input_bundle_path = (
        qualification_input_bundle_raw.expanduser().resolve()
        if isinstance(qualification_input_bundle_raw, Path)
        else None
    )
    qualification_manifest_sha256_override = _coerce_string(
        getattr(args, "qualification_manifest_sha256", None)
    )
    qualification_cycle_root_raw = getattr(args, "qualification_cycle_root", None)
    qualification_cycle_root = (
        qualification_cycle_root_raw.expanduser().resolve()
        if isinstance(qualification_cycle_root_raw, Path)
        else None
    )
    shadow_state_raw = getattr(args, "shadow_state", None)
    explicit_shadow_state_path = (
        shadow_state_raw.expanduser().resolve() if isinstance(shadow_state_raw, Path) else None
    )
    qualification_path_overrides = {
        "qualification_corpus_manifest_path": getattr(args, "qualification_corpus_manifest", None),
        "qualification_output_adjudication_path": getattr(
            args, "qualification_output_adjudication", None
        ),
        "no_actionable_evidence_receipt_path": getattr(
            args, "no_actionable_evidence_receipt", None
        ),
    }
    non_exporting_shadow = shadow or operational_shadow or qualification_prepare
    if score_shadow and not shadow:
        print("--score-shadow must be combined with --shadow.", file=sys.stderr)
        return 2
    if score_operational_shadow and not operational_shadow:
        print(
            "--score-operational-shadow must be combined with --operational-shadow.",
            file=sys.stderr,
        )
        return 2
    if shadow and operational_shadow:
        print("Cannot combine release and operational shadow modes.", file=sys.stderr)
        return 2
    if qualification_prepare and (
        shadow or operational_shadow or bool(getattr(args, "dry_run", False))
    ):
        print(
            "Qualification preparation is model-free and cannot combine with run modes.",
            file=sys.stderr,
        )
        return 2
    if qualification_input_bundle_path is not None and not shadow:
        print("--qualification-input-bundle requires --shadow.", file=sys.stderr)
        return 2
    if bool(getattr(args, "force", False)) and (
        qualification_input_bundle_path is not None or (shadow and score_shadow)
    ):
        print(
            "--force cannot replace or score an authored sealed qualification cycle; "
            "resume the same cycle so same-author correction history is retained.",
            file=sys.stderr,
        )
        return 2
    if qualification_input_bundle_path is not None:
        if qualification_cycle_root is None or explicit_shadow_state_path is None:
            print(
                "Sealed release qualification requires --qualification-cycle-root and "
                "--shadow-state.",
                file=sys.stderr,
            )
            return 2
        if (
            not score_shadow
            and qualification_path_overrides["qualification_corpus_manifest_path"] is not None
        ):
            print(
                "Sealed phase one receives only --qualification-manifest-sha256; "
                "supply the manifest path to --score-shadow.",
                file=sys.stderr,
            )
            return 2
        if not score_shadow and not _qualification_valid_sha256(
            qualification_manifest_sha256_override
        ):
            print(
                "Sealed phase one requires a valid --qualification-manifest-sha256.",
                file=sys.stderr,
            )
            return 2
    if any(path is not None for path in qualification_path_overrides.values()) and not shadow:
        print(
            "External qualification path overrides are release-shadow-only.",
            file=sys.stderr,
        )
        return 2
    if non_exporting_shadow and bool(args.dry_run):
        print("Cannot combine shadow modes with --dry-run.", file=sys.stderr)
        return 2
    agent_preflight_error = _live_agent_preflight_error(
        agent=str(args.agent),
        dry_run=bool(args.dry_run) or qualification_prepare,
        score_shadow=score_shadow or score_operational_shadow,
    )
    if agent_preflight_error is not None:
        print(agent_preflight_error, file=sys.stderr)
        return 2

    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)
    requested_agent = str(args.agent)
    stage3_resume_compatibility_contract = stage3_research_compatibility_contract(
        agent=requested_agent
    )
    requested_model = (
        str(args.model) if isinstance(args.model, str) and args.model.strip() else None
    )
    research_config_path = repo_root / "configs" / "backlog_research.yaml"
    research_config: dict[str, Any] = {}
    if research_config_path.exists():
        research_config_raw = _load_yaml(research_config_path).get("backlog_research", {})
        if not isinstance(research_config_raw, dict):
            print(f"Invalid backlog research config: {research_config_path}", file=sys.stderr)
            return 2
        research_config = research_config_raw
    research_ref = _coerce_string(getattr(args, "research_ref", None)) or _coerce_string(
        research_config.get("source_ref")
    )
    replay_timeout_raw = research_config.get("clean_replay_timeout_seconds")
    replay_timeout_seconds = (
        float(replay_timeout_raw)
        if isinstance(replay_timeout_raw, (int, float))
        and not isinstance(replay_timeout_raw, bool)
        and float(replay_timeout_raw) > 0
        else 10800.0
    )
    shadow_gate_config = normalize_shadow_gate_config(None)
    qualification_manifest_path: Path | None = None
    qualification_output_adjudication_path: Path | None = None
    no_actionable_evidence_receipt_path: Path | None = None
    qualification_manifest_sha256_expected: str | None = None
    qualification_output_adjudication_sha256_pre_run: str | None = None
    qualification_input_bundle: dict[str, Any] | None = None
    if qualification_input_bundle_path is not None:
        try:
            qualification_input_bundle = load_qualification_input_bundle(
                qualification_input_bundle_path,
                verify_files=True,
            )
        except (OSError, ValueError) as exc:
            print(f"Invalid qualification input bundle: {exc}", file=sys.stderr)
            return 2
        bundle_pipeline_raw = qualification_input_bundle.get("pipeline")
        bundle_pipeline = bundle_pipeline_raw if isinstance(bundle_pipeline_raw, Mapping) else {}
        bundle_files_raw = bundle_pipeline.get("files")
        bundle_files = bundle_files_raw if isinstance(bundle_files_raw, Mapping) else {}
        if _coerce_string(bundle_files.get("repo_root")) != str(repo_root.resolve()):
            print(
                "Qualification input bundle pipeline root does not match --repo-root.",
                file=sys.stderr,
            )
            return 2
        runtime_binding_errors = first_party_module_binding_errors(
            modules=sys.modules,
            repo_root=repo_root,
            pipeline_manifest=bundle_files,
        )
        if runtime_binding_errors:
            print(
                "Qualification runtime imports are absent from the sealed pipeline manifest: "
                + ",".join(runtime_binding_errors),
                file=sys.stderr,
            )
            return 2
    if non_exporting_shadow:
        shadow_gate_config_path = repo_root / "configs" / "backlog_export_gate.yaml"
        if not shadow_gate_config_path.is_file():
            print(
                f"Missing backlog export gate config: {shadow_gate_config_path}",
                file=sys.stderr,
            )
            return 2
        try:
            shadow_gate_raw = _load_yaml(shadow_gate_config_path).get("backlog_export_gate", {})
            shadow_gate_config = normalize_shadow_gate_config(shadow_gate_raw)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            print(
                f"Invalid backlog export gate: {shadow_gate_config_path}: {exc}",
                file=sys.stderr,
            )
            return 2
        if shadow_gate_config["enabled"] is not True:
            print(
                f"Backlog export gate is disabled: {shadow_gate_config_path}",
                file=sys.stderr,
            )
            return 2
        if shadow:

            def qualification_path(field: str) -> Path | None:
                override = qualification_path_overrides[field]
                if isinstance(override, Path):
                    return (
                        _resolve_optional_path(repo_root, override)
                        or override.expanduser().resolve()
                    )
                return _qualification_artifact_path(
                    repo_root,
                    shadow_gate_config[field],
                )

            qualification_manifest_path = qualification_path("qualification_corpus_manifest_path")
            qualification_output_adjudication_path = qualification_path(
                "qualification_output_adjudication_path"
            )
            no_actionable_evidence_receipt_path = qualification_path(
                "no_actionable_evidence_receipt_path"
            )
            # A sealed phase-one run receives only the externally computed byte
            # digest. The actual held-out manifest is supplied after model output.
            if qualification_input_bundle is not None and not score_shadow:
                qualification_manifest_path = None
                qualification_manifest_sha256_expected = qualification_manifest_sha256_override
            else:
                qualification_manifest_sha256_expected = _qualification_file_sha256(
                    qualification_manifest_path
                )
                if qualification_manifest_sha256_override is not None and (
                    qualification_manifest_sha256_expected != qualification_manifest_sha256_override
                ):
                    print(
                        "Qualification manifest bytes do not match "
                        "--qualification-manifest-sha256.",
                        file=sys.stderr,
                    )
                    return 2
            qualification_output_adjudication_sha256_pre_run = _qualification_file_sha256(
                qualification_output_adjudication_path
            )

    runs_dir = args.runs_dir.resolve() if args.runs_dir is not None else cfg.runs_dir
    stage_runs_raw = getattr(args, "stage_runs_dir", None)
    stage_runs_dir = (
        stage_runs_raw.expanduser().resolve() if isinstance(stage_runs_raw, Path) else runs_dir
    )
    qualification_cycle_contract_value: dict[str, Any] | None = None
    qualification_cycle_contract_path: Path | None = None
    if qualification_input_bundle is not None:
        bundle_source = qualification_input_bundle.get("source_inputs")
        bundle_source = bundle_source if isinstance(bundle_source, Mapping) else {}
        bundle_runs = bundle_source.get("source_runs")
        bundle_runs = bundle_runs if isinstance(bundle_runs, Mapping) else {}
        bundle_scope = qualification_input_bundle.get("scope")
        bundle_scope = bundle_scope if isinstance(bundle_scope, Mapping) else {}
        if _coerce_string(bundle_runs.get("root")) != str(runs_dir.resolve()):
            print("Qualification input bundle source-runs path mismatch.", file=sys.stderr)
            return 2
        if stage_runs_raw is None or stage_runs_dir == runs_dir:
            print(
                "Sealed qualification requires an isolated --stage-runs-dir distinct "
                "from frozen --runs-dir.",
                file=sys.stderr,
            )
            return 2
        if _coerce_string(bundle_scope.get("research_ref")) != research_ref:
            print("Qualification input bundle research-ref mismatch.", file=sys.stderr)
            return 2
    # Source evidence stays frozen while agent/research output is appended to a
    # cycle-local destination. Legacy runs retain the historical shared root.
    cfg = replace(cfg, runs_dir=stage_runs_dir)
    implementation_runs_root = inferred_implementation_runs_root(runs_dir)
    outcome_trusted_runs_roots = _outcome_trusted_runs_roots(
        primary_runs_dir=runs_dir,
        configured_runs_dir=cfg.runs_dir,
        implementation_runs_root=implementation_runs_root,
    )
    qualification_additional_source_roots: tuple[Path, ...] = ()
    if qualification_input_bundle is not None:
        bundle_source_raw = qualification_input_bundle.get("source_inputs")
        bundle_source = bundle_source_raw if isinstance(bundle_source_raw, Mapping) else {}
        qualification_additional_source_roots = _qualification_additional_source_roots(
            bundle_source
        )
        outcome_trusted_runs_roots = tuple(
            sorted(
                {
                    *outcome_trusted_runs_roots,
                    *qualification_additional_source_roots,
                },
                key=lambda path: str(path),
            )
        )
    target_slug: str | None = None
    if isinstance(args.target, str) and args.target.strip():
        target_slug = str(args.target).strip()
    repo_input = (
        str(args.repo_input).strip()
        if isinstance(args.repo_input, str) and args.repo_input.strip()
        else None
    )
    if qualification_input_bundle is not None:
        bundle_scope = qualification_input_bundle.get("scope")
        bundle_scope = bundle_scope if isinstance(bundle_scope, Mapping) else {}
        expected_repo_input = _coerce_string(bundle_scope.get("repo_input"))
        observed_repo_input = (
            str(Path(repo_input).expanduser().resolve()) if repo_input is not None else None
        )
        if expected_repo_input != observed_repo_input or bundle_scope.get("target") != target_slug:
            print("Qualification input bundle scope mismatch.", file=sys.stderr)
            return 2
    default_name = slugify(repo_input) if repo_input is not None else (target_slug or "all")

    if args.out_json is not None:
        out_json = _resolve_optional_path(repo_root, args.out_json) or args.out_json.resolve()
    elif qualification_cycle_root is not None:
        out_json = qualification_cycle_root / f"{default_name}.backlog.json"
    else:
        if target_slug is not None:
            out_json = runs_dir / target_slug / "_compiled" / f"{default_name}.backlog.json"
        else:
            out_json = runs_dir / "_compiled" / f"{default_name}.backlog.json"

    if args.out_md is not None:
        out_md = _resolve_optional_path(repo_root, args.out_md) or args.out_md.resolve()
    else:
        out_md = out_json.with_suffix(".md")

    if qualification_input_bundle is not None:
        assert qualification_cycle_root is not None
        try:
            out_json.resolve().relative_to(qualification_cycle_root)
            out_md.resolve().relative_to(qualification_cycle_root)
        except ValueError:
            print(
                "Qualification outputs must remain inside --qualification-cycle-root.",
                file=sys.stderr,
            )
            return 2
        initial_readable_roots = [repo_root, runs_dir, stage_runs_dir]
        if repo_input is not None and _looks_like_local_repo_input(repo_input):
            resolved_bound_input = _resolve_local_repo_root(repo_root, repo_input)
            if resolved_bound_input is not None:
                initial_readable_roots.append(resolved_bound_input)
        initial_custody_errors = _qualification_custody_errors(
            custody_paths={
                "cycle_root": qualification_cycle_root,
                "shared_state": explicit_shadow_state_path,
                "corpus_manifest": qualification_manifest_path,
                "output_adjudication": qualification_output_adjudication_path,
                "no_actionable_receipt": no_actionable_evidence_receipt_path,
            },
            model_readable_roots=initial_readable_roots,
        )
        if initial_custody_errors:
            print(
                "Qualification custody is not isolated: " + ",".join(initial_custody_errors),
                file=sys.stderr,
            )
            return 2
        cycle_breadth_profile = _normalize_breadth_profile(getattr(args, "breadth_profile", None))
        (
            cycle_prompts_dir,
            cycle_policy_config_path,
            _cycle_profile_warnings,
        ) = _resolve_breadth_profile_paths(
            repo_root=repo_root,
            breadth_profile=cycle_breadth_profile,
            prompts_dir_arg=args.prompts_dir,
            policy_config_arg=args.policy_config,
        )
        cycle_execution_profile = _normalized_qualification_execution_profile(
            args=args,
            agent=requested_agent,
            model=requested_model,
            breadth_profile=cycle_breadth_profile,
            prompts_dir=cycle_prompts_dir,
            policy_config_path=cycle_policy_config_path,
        )
        cycle_manifest_sha256 = (
            qualification_manifest_sha256_override or qualification_manifest_sha256_expected
        )
        if not _qualification_valid_sha256(cycle_manifest_sha256):
            print(
                "Qualification cycle manifest digest is missing or invalid.",
                file=sys.stderr,
            )
            return 2
        cycle_contract = _qualification_cycle_contract(
            bundle_path=qualification_input_bundle_path,
            bundle_sha256=str(qualification_input_bundle["content_sha256"]),
            manifest_sha256=str(cycle_manifest_sha256),
            cycle_root=qualification_cycle_root,
            source_runs_dir=runs_dir,
            stage_runs_dir=stage_runs_dir,
            out_json=out_json,
            out_md=out_md,
            state_path=(explicit_shadow_state_path or shadow_state_path(out_json)),
            repo_root=repo_root,
            repo_input=repo_input,
            target=target_slug,
            research_ref=research_ref,
            breadth_profile=cycle_breadth_profile,
            execution_profile=cycle_execution_profile,
            owned_names=(
                out_json.name,
                out_md.name,
                out_json.stem,
                f"{default_name}.backlog_artifacts",
                f"{default_name}.case_registry.json",
            ),
        )
        try:
            _prepare_or_validate_qualification_cycle_namespace(
                contract=cycle_contract,
                resume=bool(args.resume),
                score=score_shadow,
            )
        except (OSError, ValueError) as exc:
            print(f"Qualification cycle namespace is invalid: {exc}", file=sys.stderr)
            return 2
        qualification_cycle_contract_value = cycle_contract
        qualification_cycle_contract_path = _qualification_cycle_marker_paths(
            cycle_root=qualification_cycle_root,
            stage_runs_dir=stage_runs_dir,
        )[0]

    if score_shadow:
        score_state_path = explicit_shadow_state_path
        if score_state_path is None and out_json.is_file():
            try:
                score_backlog = _load_qualification_json_object(
                    out_json,
                    name="score_shadow_backlog",
                )
            except ValueError:
                score_backlog = {}
            score_artifacts_raw = score_backlog.get("artifacts")
            score_artifacts = (
                score_artifacts_raw if isinstance(score_artifacts_raw, Mapping) else {}
            )
            score_export_raw = score_artifacts.get("export_contract")
            score_export = score_export_raw if isinstance(score_export_raw, Mapping) else {}
            recorded_state_path = _coerce_string(score_export.get("shadow_state_path"))
            if recorded_state_path is not None:
                score_state_path = Path(recorded_state_path).resolve()
        return _score_materialized_shadow_run(
            repo_root=repo_root,
            runs_dir=runs_dir,
            out_json=out_json,
            out_md=out_md,
            repo_input=repo_input,
            shadow_gate_config=shadow_gate_config,
            qualification_manifest_path=qualification_manifest_path,
            qualification_output_adjudication_path=(qualification_output_adjudication_path),
            no_actionable_evidence_receipt_path=(no_actionable_evidence_receipt_path),
            agent=requested_agent,
            model=requested_model,
            cfg=cfg,
            research_config=research_config,
            research_ref=research_ref,
            replay_timeout_seconds=replay_timeout_seconds,
            qualification_input_bundle_path=qualification_input_bundle_path,
            qualification_manifest_sha256_expected_override=(
                qualification_manifest_sha256_override
            ),
            state_path=(score_state_path or shadow_state_path(out_json)),
        )
    if score_operational_shadow:
        return _score_materialized_operational_shadow_run(
            repo_root=repo_root,
            runs_dir=runs_dir,
            out_json=out_json,
            repo_input=repo_input,
            shadow_gate_config=shadow_gate_config,
            state_path=(explicit_shadow_state_path or shadow_state_path(out_json)),
        )

    atom_actions_arg: Path | None = args.atom_actions_yaml
    requested_atom_actions_path = (
        _resolve_optional_path(repo_root, atom_actions_arg) or atom_actions_arg.resolve()
        if atom_actions_arg is not None
        else repo_root / "configs" / "backlog_atom_actions.yaml"
    )
    if qualification_input_bundle is not None:
        bundle_source_raw = qualification_input_bundle.get("source_inputs")
        bundle_source = bundle_source_raw if isinstance(bundle_source_raw, Mapping) else {}
        atom_actions_receipt_raw = bundle_source.get("atom_actions")
        atom_actions_receipt = (
            atom_actions_receipt_raw if isinstance(atom_actions_receipt_raw, Mapping) else {}
        )
        sealed_atom_actions_raw = _coerce_string(atom_actions_receipt.get("path"))
        if sealed_atom_actions_raw is None:
            print(
                "Qualification input bundle copied-ledger path is missing.",
                file=sys.stderr,
            )
            return 2
        atom_actions_path = Path(sealed_atom_actions_raw).resolve()
        if atom_actions_arg is not None and requested_atom_actions_path != atom_actions_path:
            print(
                "--atom-actions-yaml differs from the sealed copied ledger.",
                file=sys.stderr,
            )
            return 2
    else:
        atom_actions_path = requested_atom_actions_path

    breadth_profile = _normalize_breadth_profile(getattr(args, "breadth_profile", None))
    prompts_dir, policy_config_default_path, breadth_profile_warnings = (
        _resolve_breadth_profile_paths(
            repo_root=repo_root,
            breadth_profile=breadth_profile,
            prompts_dir_arg=args.prompts_dir,
            policy_config_arg=args.policy_config,
        )
    )
    for warning_text in breadth_profile_warnings:
        print(f"[backlog] NOTE: {warning_text}", file=sys.stderr)

    atoms_jsonl = out_json.parent / f"{default_name}.backlog.atoms.jsonl"
    agent_last_message_atoms_jsonl = (
        out_json.parent / f"{default_name}.backlog.atoms.agent_last_message_artifact.jsonl"
    )
    artifacts_dir = out_json.parent / f"{default_name}.backlog_artifacts"
    case_registry_json = out_json.parent / f"{default_name}.case_registry.json"
    retained_research_path = out_json.parent / f"{default_name}.research.json"
    preexisting_stage3_resume_document: dict[str, Any] | None = None
    completed_stage3_resume_candidate: dict[str, Any] | None = None
    if bool(args.resume) and retained_research_path.is_file():
        try:
            retained_research_raw = json.loads(retained_research_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            print(
                "[backlog] ERROR: retained Stage-3 artifact is unreadable; refusing to "
                f"overwrite possible authored work during resume: {type(exc).__name__}",
                file=sys.stderr,
            )
            return 2
        if not isinstance(retained_research_raw, dict):
            print(
                "[backlog] ERROR: retained Stage-3 artifact is not an object; refusing to "
                "overwrite possible authored work during resume.",
                file=sys.stderr,
            )
            return 2
        retained_meta_raw = retained_research_raw.get("input_meta")
        retained_meta = retained_meta_raw if isinstance(retained_meta_raw, Mapping) else {}
        retained_checkpoint_raw = retained_meta.get("external_wait")
        retained_checkpoint = (
            retained_checkpoint_raw if isinstance(retained_checkpoint_raw, Mapping) else {}
        )
        parked_resume_intent = (
            retained_meta.get("stage_status") == "parked_external_wait"
            or retained_checkpoint.get("status") == "parked_external_wait"
        )
        verified_wait = _stage3_provider_external_wait(retained_research_raw)
        progress_resume_intent = retained_meta.get("stage_status") == "checkpointed_progress"
        verified_progress = _stage3_completed_progress(
            retained_research_raw,
            expected_compatibility_contract=stage3_resume_compatibility_contract,
        )
        completed_checkpoint_raw = retained_meta.get("completed_stage_checkpoint")
        completed_resume_intent = retained_meta.get("stage_status") == "completed" and isinstance(
            completed_checkpoint_raw, Mapping
        )
        verified_completed = (
            _stage3_completed_stage(
                retained_research_raw,
                expected_compatibility_contract=stage3_resume_compatibility_contract,
            )
            if completed_resume_intent
            else None
        )
        if parked_resume_intent and verified_wait is None:
            print(
                "[backlog] ERROR: retained Stage-3 provider-wait checkpoint failed "
                "integrity validation; refusing to restart or overwrite it.",
                file=sys.stderr,
            )
            return 2
        if progress_resume_intent and verified_progress is None:
            print(
                "[backlog] ERROR: retained Stage-3 progress checkpoint failed "
                "integrity validation; refusing to restart or overwrite it.",
                file=sys.stderr,
            )
            return 2
        if completed_resume_intent and verified_completed is None:
            print(
                "[backlog] NOTE: retained completed Stage-3 checkpoint is not compatible "
                "with this invocation and will be treated as a cache miss. In-progress "
                "and provider-wait checkpoints remain protected from overwrite.",
                file=sys.stderr,
            )
        if verified_wait is not None or verified_progress is not None:
            preexisting_stage3_resume_document = retained_research_raw
        elif verified_completed is not None:
            # A completed proof is a reusable cache, not an in-progress author frontier.
            # Validate it against the newly materialized Stage-1/2 inputs below; changed
            # evidence must cause a normal fresh Stage 3 rather than aborting the new cycle.
            completed_stage3_resume_candidate = retained_research_raw
    qualification_source_snapshot: dict[str, Any] | None = None
    qualification_additional_evidence_runs_dirs: list[Path] = []
    if qualification_prepare:
        seed_raw = getattr(args, "qualification_case_registry_seed", None)
        protected_raw = getattr(args, "qualification_protected_path", [])
        additional_evidence_raw = getattr(
            args,
            "qualification_additional_evidence_runs_dir",
            [],
        )
        protected_paths = (
            [path for path in protected_raw if isinstance(path, Path)]
            if isinstance(protected_raw, list)
            else []
        )
        qualification_additional_evidence_runs_dirs = (
            [path for path in additional_evidence_raw if isinstance(path, Path)]
            if isinstance(additional_evidence_raw, list)
            else []
        )
        if len(qualification_additional_evidence_runs_dirs) != len(
            additional_evidence_raw if isinstance(additional_evidence_raw, list) else []
        ):
            print(
                "Qualification additional evidence roots must be filesystem paths.",
                file=sys.stderr,
            )
            return 2
        if repo_input is None or not isinstance(seed_raw, Path):
            print(
                "Qualification preparation requires local --repo-input and an explicit "
                "case-registry seed.",
                file=sys.stderr,
            )
            return 2
        try:
            qualification_source_snapshot = capture_qualification_preparation_snapshot(
                repo_root=repo_root,
                repo_input=Path(repo_input),
                research_ref=research_ref or "",
                source_runs_dir=runs_dir,
                atom_actions_path=atom_actions_path,
                case_registry_seed_path=seed_raw,
                target=target_slug,
                additional_evidence_runs_dirs=(
                    qualification_additional_evidence_runs_dirs
                ),
                protected_paths=protected_paths,
            )
            qualification_snapshot_sources_raw = qualification_source_snapshot.get(
                "source_inputs"
            )
            qualification_snapshot_sources = (
                qualification_snapshot_sources_raw
                if isinstance(qualification_snapshot_sources_raw, Mapping)
                else {}
            )
            outcome_trusted_runs_roots = _outcome_trusted_runs_roots(
                primary_runs_dir=runs_dir,
                configured_runs_dir=cfg.runs_dir,
                implementation_runs_root=implementation_runs_root,
                additional_runs_roots=_qualification_additional_source_roots(
                    qualification_snapshot_sources
                ),
            )
        except (OSError, ValueError) as exc:
            print(
                f"[backlog] ERROR: qualification input snapshot failed: {exc}",
                file=sys.stderr,
            )
            return 2
    try:
        registry_seed_raw = getattr(args, "qualification_case_registry_seed", None)
        if qualification_input_bundle is not None:
            # A sealed execution must begin with the sealed historical graph.  Outcome
            # reconciliation may then materialize authenticated cases that are absent
            # from that older seed.  Loading the seed only after reconciliation would
            # silently discard those cases immediately before atom-lineage restoration.
            registry_source = _qualification_case_registry_seed_path(
                qualification_input_bundle
            )
        elif qualification_prepare and isinstance(registry_seed_raw, Path):
            registry_source = registry_seed_raw.expanduser().resolve()
        else:
            registry_source = case_registry_json
        case_registry = load_case_registry(registry_source)
    except ValueError as exc:
        print(f"[backlog] ERROR: {exc}", file=sys.stderr)
        return 2

    records = list(
        iter_report_history(
            runs_dir,
            target_slug=target_slug,
            repo_input=repo_input,
            embed="none",
        )
    )
    atoms_doc_raw = extract_backlog_atoms(records, repo_root=repo_root)
    atoms_raw = atoms_doc_raw.get("atoms")
    extracted_atoms = (
        [item for item in atoms_raw if isinstance(item, dict)]
        if isinstance(atoms_raw, list)
        else []
    )
    primary_raw_atoms = normalize_atom_lineage(
        extracted_atoms,
        case_registry=case_registry,
        strict_new_output=True,
    )
    primary_derived_evidence = annotate_primary_derived_evidence(
        records,
        primary_raw_atoms,
        source_root=runs_dir,
        case_registry=case_registry,
    )
    primary_raw_atoms = primary_derived_evidence.atoms
    additional_evidence_metadata: list[dict[str, Any]] = []
    if qualification_prepare and qualification_source_snapshot is not None:
        source_inputs_raw = qualification_source_snapshot.get("source_inputs")
        source_inputs = (
            source_inputs_raw if isinstance(source_inputs_raw, Mapping) else {}
        )
        manifests_raw = source_inputs.get("additional_evidence_runs")
        manifests = manifests_raw if isinstance(manifests_raw, list) else []
        for manifest_raw in manifests:
            if not isinstance(manifest_raw, Mapping):
                raise ValueError("qualification_retained_evidence_manifest_invalid")
            manifest = dict(manifest_raw)
            root_raw = _coerce_string(manifest.get("root"))
            if root_raw is None:
                raise ValueError("qualification_retained_evidence_manifest_invalid")
            source_root = Path(root_raw).resolve()
            retained_records = list(
                iter_report_history(
                    source_root,
                    target_slug=target_slug,
                    repo_input=repo_input,
                    embed="none",
                )
            )
            if (
                manifest.get("manifest_kind")
                == SEMANTIC_RUN_EVIDENCE_MANIFEST_KIND
            ):
                preview_doc = extract_backlog_atoms(retained_records, repo_root=repo_root)
                preview_raw = preview_doc.get("atoms")
                preview_atoms = (
                    [item for item in preview_raw if isinstance(item, dict)]
                    if isinstance(preview_raw, list)
                    else []
                )
                # Retained-record identity is derived from the finalized per-run
                # semantic receipt.  Preview extraction discovers attachment refs;
                # the ordinary namespaced extraction below remains the authoritative
                # atom production pass.
                manifest = extend_semantic_manifest_atom_closure(
                    manifest,
                    atoms=preview_atoms,
                    repo_root=repo_root,
                )
            retained_records = _prepare_qualification_retained_records(
                retained_records,
                source_manifest=manifest,
            )
            retained_doc = extract_backlog_atoms(retained_records, repo_root=repo_root)
            retained_atoms_raw = retained_doc.get("atoms")
            retained_atoms = (
                [item for item in retained_atoms_raw if isinstance(item, dict)]
                if isinstance(retained_atoms_raw, list)
                else []
            )
            retained_atoms = normalize_atom_lineage(
                retained_atoms,
                case_registry=case_registry,
                strict_new_output=True,
            )
            retained_derived = annotate_primary_derived_evidence(
                retained_records,
                retained_atoms,
                source_root=source_root,
                case_registry=case_registry,
            )
            retained_atoms = _annotate_qualification_retained_atoms(
                retained_derived.atoms,
                records=retained_records,
            )
            records.extend(retained_records)
            primary_raw_atoms.extend(retained_atoms)
            additional_evidence_metadata.append(
                {
                    "kind": "retained_usertest",
                    "path": str(source_root),
                    "source_root_sha256": manifest.get("entries_sha256"),
                    "records_seen": len(retained_records),
                    "atoms_ingested": len(retained_atoms),
                    "derived_records": retained_derived.metadata.get(
                        "derived_records",
                        0,
                    ),
                    "derived_atoms": retained_derived.metadata.get("derived_atoms", 0),
                }
            )
    primary_atom_ids = {
        atom_id
        for atom in primary_raw_atoms
        for atom_id in [_coerce_string(atom.get("atom_id"))]
        if atom_id is not None
    }

    try:
        atom_actions = _load_atom_actions_yaml(atom_actions_path)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    plan_sync_meta: dict[str, Any] | None = None
    plan_sync_at: str | None = None
    candidate_roots: list[Path] = [repo_root]
    if repo_input is not None and _looks_like_local_repo_input(repo_input):
        resolved_repo_input = _resolve_local_repo_root(repo_root, repo_input)
        if resolved_repo_input is not None:
            candidate_roots.append(resolved_repo_input)
    for record in records:
        target_ref = record.get("target_ref")
        if not isinstance(target_ref, dict):
            continue
        repo_input_from_record = _coerce_string(target_ref.get("repo_input"))
        if repo_input_from_record is None:
            continue
        if not _looks_like_local_repo_input(repo_input_from_record):
            continue
        resolved = _resolve_local_repo_root(repo_root, repo_input_from_record)
        if resolved is not None:
            candidate_roots.append(resolved)
    for entry in atom_actions.values():
        roots_raw = entry.get("queue_owner_roots")
        roots = (
            [item for item in roots_raw if isinstance(item, str) and item.strip()]
            if isinstance(roots_raw, list)
            else []
        )
        for root_s in roots:
            if not _looks_like_local_repo_input(root_s):
                continue
            resolved = _resolve_local_repo_root(repo_root, root_s)
            if resolved is not None:
                candidate_roots.append(resolved)
    owner_roots = sorted({p.resolve() for p in candidate_roots}, key=lambda p: str(p))
    if qualification_prepare:
        assert qualification_source_snapshot is not None
        seed_raw = getattr(args, "qualification_case_registry_seed", None)
        protected_raw = getattr(args, "qualification_protected_path", [])
        protected_paths = (
            [path for path in protected_raw if isinstance(path, Path)]
            if isinstance(protected_raw, list)
            else []
        )
        assert isinstance(seed_raw, Path)
        assert repo_input is not None
        try:
            qualification_source_snapshot = extend_qualification_preparation_snapshot(
                qualification_source_snapshot,
                repo_root=repo_root,
                repo_input=Path(repo_input),
                research_ref=research_ref or "",
                source_runs_dir=runs_dir,
                atom_actions_path=atom_actions_path,
                case_registry_seed_path=seed_raw,
                target=target_slug,
                additional_evidence_runs_dirs=(
                    qualification_additional_evidence_runs_dirs
                ),
                atoms=primary_raw_atoms,
                protected_paths=protected_paths,
                owner_roots=owner_roots,
            )
        except (OSError, ValueError) as exc:
            print(
                f"[backlog] ERROR: qualification input snapshot changed: {exc}",
                file=sys.stderr,
            )
            return 2
    if qualification_input_bundle is not None:
        bound_source_raw = qualification_input_bundle.get("source_inputs")
        bound_source = bound_source_raw if isinstance(bound_source_raw, Mapping) else {}
        bound_owner_roots_raw = bound_source.get("owner_roots")
        bound_owner_roots = (
            {
                Path(item).resolve()
                for item in bound_owner_roots_raw
                if isinstance(item, str) and item.strip()
            }
            if isinstance(bound_owner_roots_raw, list)
            else set()
        )
        if bound_owner_roots != set(owner_roots):
            print(
                "Qualification owner roots differ from the sealed transaction.",
                file=sys.stderr,
            )
            return 2
    if shadow:
        model_readable_roots = [
            *owner_roots,
            runs_dir,
            stage_runs_dir,
            artifacts_dir,
            *qualification_additional_source_roots,
        ]
        prior_label_paths = (
            _prior_qualification_label_paths(explicit_shadow_state_path)
            if qualification_input_bundle is not None and explicit_shadow_state_path is not None
            else {}
        )
        exposure_errors = _qualification_workspace_exposure_errors(
            artifact_paths={
                "qualification_corpus_manifest": qualification_manifest_path,
                "qualification_output_adjudication": (qualification_output_adjudication_path),
                "no_actionable_evidence_receipt": (no_actionable_evidence_receipt_path),
                **prior_label_paths,
            },
            model_readable_roots=model_readable_roots,
        )
        if qualification_input_bundle is not None:
            exposure_errors.extend(
                _qualification_custody_errors(
                    custody_paths={
                        "cycle_root": qualification_cycle_root,
                        "shared_state": explicit_shadow_state_path,
                        "corpus_manifest": qualification_manifest_path,
                        "output_adjudication": (qualification_output_adjudication_path),
                        "no_actionable_receipt": (no_actionable_evidence_receipt_path),
                        **prior_label_paths,
                    },
                    model_readable_roots=model_readable_roots,
                )
            )
        if exposure_errors:
            print(
                "[backlog] ERROR: independent qualification labels are model-readable: "
                + ",".join(exposure_errors),
                file=sys.stderr,
            )
            return 2

    if not bool(getattr(args, "skip_plan_folder_sync", False)):
        sync_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        plan_sync_at = sync_at
        plan_sync_meta = _reconcile_atom_actions_from_plan_folders(
            atom_actions=atom_actions,
            owner_roots=owner_roots,
            generated_at=sync_at,
        )

    backfill_at = plan_sync_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    backfill_meta = _backfill_failure_event_atoms_from_legacy_entries(
        atom_actions=atom_actions,
        generated_at=backfill_at,
    )
    if plan_sync_meta is not None:
        plan_sync_meta["failure_event_backfill"] = backfill_meta
        if not non_exporting_shadow and preexisting_stage3_resume_document is None:
            _write_atom_actions_yaml(atom_actions_path, atom_actions)

    case_outcome_sync = _sync_case_registry_outcomes(
        case_registry=case_registry,
        atom_actions=atom_actions,
        trusted_runs_roots=outcome_trusted_runs_roots,
        owner_roots=tuple(owner_roots),
    )
    stale_actioned_reset = _reset_stale_unproven_actioned_atoms(
        atom_actions=atom_actions,
        case_registry=case_registry,
        current_plan_sync_at=plan_sync_at,
        generated_at=backfill_at,
    )
    if (
        not non_exporting_shadow
        and stale_actioned_reset["reset_to_new"]
        and preexisting_stage3_resume_document is None
    ):
        _write_atom_actions_yaml(atom_actions_path, atom_actions)
    try:
        # Lifecycle evidence is durable independently of whether a later mining stage
        # succeeds; do not wait for relation review to persist validated outcomes.
        if preexisting_stage3_resume_document is None:
            write_case_registry(case_registry_json, case_registry)
    except (OSError, ValueError) as exc:
        print(f"[backlog] ERROR: failed to persist case outcomes: {exc}", file=sys.stderr)
        return 2

    derived_scope_repo_root = repo_root
    if repo_input is not None and _looks_like_local_repo_input(repo_input):
        resolved_scope_repo_root = _resolve_local_repo_root(repo_root, repo_input)
        if resolved_scope_repo_root is not None:
            derived_scope_repo_root = resolved_scope_repo_root
    orphan_history_records, orphan_history_recovery_meta = recover_orphan_implementation_history(
        runs_dir,
        target_slug=target_slug,
        scoped_repo_root=derived_scope_repo_root,
    )
    derived_history_records_unfiltered = [
        *iter_report_history(
            implementation_runs_root,
            target_slug=target_slug,
            repo_input=None,
            embed="none",
        ),
        *orphan_history_records,
    ]
    derived_history_records, derived_scope_filter_meta = filter_derived_history_records(
        derived_history_records_unfiltered,
        target_slug=target_slug,
        repo_input=repo_input,
        repo_root=derived_scope_repo_root,
        git_remote_urls=sorted(_git_remote_urls(derived_scope_repo_root)),
    )
    derived_ingestion = ingest_derived_evidence_records(
        derived_history_records,
        source_root=implementation_runs_root,
        repo_root=repo_root,
        atom_actions=atom_actions,
        case_registry=case_registry,
    )
    operational_failure_candidates = build_operational_failure_candidates(
        [*records, *derived_ingestion.records],
        [*primary_raw_atoms, *derived_ingestion.atoms],
        parent_bindings_by_run={
            **primary_derived_evidence.parent_bindings_by_run,
            **derived_ingestion.parent_bindings_by_run,
        },
    )
    operational_failure_candidates = annotate_operational_failure_candidates(
        operational_failure_candidates,
        records=[*records, *derived_ingestion.records],
        source_atoms=[*primary_raw_atoms, *derived_ingestion.atoms],
        primary_source_root=runs_dir,
    )
    derived_evidence_meta = with_operational_candidate_metadata(
        derived_ingestion.metadata,
        operational_failure_candidates,
    )
    derived_evidence_meta["scope_filter"] = derived_scope_filter_meta
    derived_evidence_meta["orphan_history_recovery"] = orphan_history_recovery_meta
    derived_evidence_meta["primary_derived_evidence"] = primary_derived_evidence.metadata
    derived_evidence_meta["source_roots"] = [
        {
            "kind": "usertest",
            "path": str(runs_dir.resolve()),
            "records_seen": len(records),
            "derived_records": primary_derived_evidence.metadata["derived_records"],
        },
        *additional_evidence_metadata,
        *derived_evidence_meta["source_roots"],
    ]
    raw_atoms = [
        *primary_raw_atoms,
        *derived_ingestion.atoms,
        *operational_failure_candidates,
    ]

    carryover_meta: dict[str, Any] | None = None
    if bool(getattr(args, "carryover_actioned_only", False)):
        if args.exclude_atom_status:
            print(
                "Cannot combine --carryover-actioned-only with --exclude-atom-status.",
                file=sys.stderr,
            )
            return 2
        carryover_at = plan_sync_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        demoted_atoms = 0
        demoted_status_counts: dict[str, int] = {}
        for entry in atom_actions.values():
            status = _normalize_atom_status(_coerce_string(entry.get("status")))
            if status in ("new", "actioned"):
                continue
            entry["status"] = "new"
            entry["carryover_reset_at"] = carryover_at
            demoted_atoms += 1
            demoted_status_counts[status] = demoted_status_counts.get(status, 0) + 1
        carryover_meta = {
            "mode": "actioned_only",
            "reset_at": carryover_at,
            "demoted_atoms": demoted_atoms,
            "demoted_status_counts": demoted_status_counts,
        }

    # A queue/ticket/action label records workflow movement, not resolution.  The
    # default filter suppresses these atoms only when a provenance-verified terminal
    # outcome exists. Active canonical cases remain attached to their work unit, while
    # unmapped or unproven evidence fails open for another complete mining pass. An
    # explicit --exclude-atom-status remains an operator override.
    default_status_filter = not bool(args.exclude_atom_status)
    exclude_atom_statuses = args.exclude_atom_status or ["ticketed", "queued", "actioned"]
    exclude_atom_status_set = {
        _normalize_atom_status(_coerce_string(status))
        for status in exclude_atom_statuses
        if _coerce_string(status) is not None
    }
    excluded_atoms: list[dict[str, Any]] = []
    atoms: list[dict[str, Any]] = []
    agent_last_message_atoms: list[dict[str, Any]] = []
    excluded_status_counts: dict[str, int] = {}
    reopened_atoms: list[dict[str, Any]] = []
    reopened_status_counts: dict[str, int] = {}
    reopened_reason_counts: dict[str, int] = {}
    preserved_open_case_status_counts: dict[str, int] = {}
    reopened_case_identity: dict[str, str | None] = {}
    for atom in raw_atoms:
        atom_id = _coerce_string(atom.get("atom_id"))
        atom_status = "new"
        if atom_id is not None:
            existing = atom_actions.get(atom_id)
            if isinstance(existing, dict):
                atom_status = _normalize_atom_status(_coerce_string(existing.get("status")))
        action_entry = atom_actions.get(atom_id) if atom_id is not None else None
        stale_reset_status = (
            _normalize_atom_status(
                _coerce_string(action_entry.get("stale_actioned_previous_status"))
            )
            if isinstance(action_entry, dict)
            and _coerce_string(action_entry.get("stale_actioned_reset_at")) == backfill_at
            else None
        )
        historical_status = stale_reset_status or atom_status
        action_case_id = (
            _coerce_string(action_entry.get("case_id")) if isinstance(action_entry, dict) else None
        )
        if isinstance(action_entry, dict):
            explicit_disposition = _coerce_string(action_entry.get("disposition"))
            if explicit_disposition in ATOM_DISPOSITIONS:
                atom = dict(atom)
                novel_rationale = _coerce_string(action_entry.get("novel_case_rationale"))
                if novel_rationale is not None:
                    atom["novel_case_rationale"] = novel_rationale
                if explicit_disposition == "supports_case" and action_case_id is not None:
                    atom["case_id"] = action_case_id
                    atom["supporting_case_ids"] = [action_case_id]
                    if _coerce_string(atom.get("evidence_role")) in {
                        "research",
                        "implementation",
                        "verification",
                    }:
                        atom["parent_case_id"] = action_case_id
                decision_rationale = _coerce_string(action_entry.get("disposition_rationale")) or (
                    novel_rationale if explicit_disposition == "novel_case" else None
                )
                if (
                    decision_rationale is None
                    and explicit_disposition == "supports_case"
                    and action_case_id is not None
                ):
                    decision_rationale = (
                        "The durable atom action ledger explicitly attaches this atom to "
                        f"{action_case_id}."
                    )
                derived_role = _coerce_string(atom.get("evidence_role")) in {
                    "research",
                    "implementation",
                    "verification",
                }
                decision_error = None
                if decision_rationale is None:
                    decision_error = "atom_action_disposition_rationale_missing"
                elif explicit_disposition == "supports_case" and action_case_id is None:
                    decision_error = "atom_action_supports_case_id_missing"
                elif (
                    explicit_disposition == "novel_case"
                    and derived_role
                    and _coerce_string(atom.get("parent_case_id")) is None
                ):
                    decision_error = "atom_action_novel_parent_case_id_missing"
                if decision_error is None:
                    atom = apply_atom_disposition_decision(
                        atom,
                        disposition=explicit_disposition,
                        source="atom_action_ledger",
                        rationale=decision_rationale,
                    )
                else:
                    atom["disposition_decision_error"] = decision_error
        registry_case_id = _registry_case_id_for_atom(case_registry, atom_id)
        atom_case_id = _coerce_string(atom.get("case_id")) or action_case_id or registry_case_id
        case_state = _case_state_from_registry(case_registry, atom_case_id)
        keep_for_open_case = case_state is not None and case_state not in TERMINAL_CASE_STATES
        proven_terminal_outcome = _case_has_proven_terminal_outcome(
            case_registry,
            atom_case_id,
        )
        idea_originated = atom_is_idea_originated(atom) or (
            isinstance(action_entry, dict) and atom_is_idea_originated(action_entry)
        )
        if idea_originated:
            excluded_idea = dict(atom)
            excluded_idea["idea_originated"] = True
            excluded_idea["idea_origin_provenance"] = (
                "atom_action_ledger"
                if isinstance(action_entry, dict) and atom_is_idea_originated(action_entry)
                else "atom"
            )
            excluded_atoms.append(excluded_idea)
            excluded_status_counts["idea_originated"] = (
                excluded_status_counts.get("idea_originated", 0) + 1
            )
            continue
        reopen_unproven = bool(
            default_status_filter
            and historical_status in exclude_atom_status_set
            and not idea_originated
            and not keep_for_open_case
            and not proven_terminal_outcome
        )
        if keep_for_open_case and atom_status in exclude_atom_status_set:
            preserved_open_case_status_counts[atom_status] = (
                preserved_open_case_status_counts.get(atom_status, 0) + 1
            )
        if reopen_unproven:
            reason = (
                "canonical_case_missing" if case_state is None else "terminal_outcome_not_proven"
            )
            stale_previous_disposition = (
                _coerce_string(action_entry.get("stale_actioned_previous_disposition"))
                if isinstance(action_entry, dict)
                else None
            )
            prior_disposition = stale_previous_disposition or _coerce_string(
                atom.get("disposition")
            )
            prior_supporting = (
                list(atom.get("supporting_case_ids"))
                if isinstance(atom.get("supporting_case_ids"), list)
                else []
            )
            reopen_audit = {
                "previous_status": historical_status,
                "previous_case_id": atom_case_id,
                "previous_disposition": prior_disposition,
                "previous_supporting_case_ids": prior_supporting,
                "reason": reason,
                "reopened_at": backfill_at,
            }
            atom = dict(atom)
            atom["status_reopen_audit"] = reopen_audit
            atom["disposition"] = "unresolved"
            atom["disposition_status"] = "pending"
            atom["disposition_receipt"] = None
            atom.pop("disposition_decision_error", None)
            atom["supporting_case_ids"] = []
            if case_state is None:
                atom["case_id"] = None
            else:
                # Retain identity so canonicalization updates/reopens this case rather
                # than minting a wording-derived replacement.
                atom["case_id"] = atom_case_id
            if isinstance(action_entry, dict):
                action_entry["reopened_previous_status"] = historical_status
                action_entry["reopened_previous_case_id"] = atom_case_id
                action_entry["reopened_previous_disposition"] = (
                    stale_previous_disposition or _coerce_string(action_entry.get("disposition"))
                )
                action_entry["reopened_previous_supporting_case_ids"] = list(prior_supporting)
                action_entry["reopened_at"] = backfill_at
                action_entry["reopened_reason"] = reason
                action_entry["status"] = "new"
                action_entry["disposition"] = "unresolved"
                action_entry["disposition_status"] = "pending"
                action_entry["disposition_rationale"] = (
                    "A queue/ticket/action label lacked a live canonical case or a "
                    "provenance-verified terminal outcome, so the evidence was reopened."
                )
                action_entry.pop("disposition_receipt", None)
                action_entry.pop("supporting_case_ids", None)
                if case_state is None:
                    action_entry.pop("case_id", None)
                else:
                    action_entry["case_id"] = atom_case_id
            if atom_id is not None:
                reopened_case_identity[atom_id] = atom_case_id if case_state is not None else None
            reopened_atoms.append(atom)
            reopened_status_counts[historical_status] = (
                reopened_status_counts.get(historical_status, 0) + 1
            )
            reopened_reason_counts[reason] = reopened_reason_counts.get(reason, 0) + 1
        if (
            atom_status in exclude_atom_status_set
            and not keep_for_open_case
            and not reopen_unproven
        ):
            excluded_atoms.append(atom)
            excluded_status_counts[atom_status] = excluded_status_counts.get(atom_status, 0) + 1
            continue
        if _coerce_string(atom.get("source")) == "agent_last_message_artifact":
            # Retain the diagnostic mirror, but do not remove this available observation
            # from problem mining. Complete bounded stage-1 review now handles the noise
            # explicitly instead of silently discarding potentially unique caveats.
            agent_last_message_atoms.append(atom)
        atoms.append(atom)

    if reopened_atoms and not non_exporting_shadow and preexisting_stage3_resume_document is None:
        # Persist immediately so a later stage failure cannot let the monotonic action
        # updater reapply a stale supports_case/ticketed row on the next cycle.
        _write_atom_actions_yaml(atom_actions_path, atom_actions)

    eligible_atoms_trackable = len(atoms)
    pipeline_batch_breadth = compute_batch_breadth(atoms)
    # Aggregate metrics may originate a problem, so their source population must be
    # constrained by runner-authored lineage rather than by mere retention.  Derived
    # research/implementation/verification runs remain useful on their parent cases,
    # but must not be recycled into fresh observation aggregates.
    eligible_run_rels = {
        run_rel
        for atom in atoms
        for atom_id in [_coerce_string(atom.get("atom_id"))]
        for run_rel in [_coerce_string(atom.get("run_rel"))]
        if atom_id in primary_atom_ids
        and run_rel is not None
        and _coerce_string(atom.get("evidence_role")) == "observation"
        and _coerce_string(atom.get("lineage_mining_blocker")) is None
    }
    aggregate_run_id_prefix = (
        "__aggregate__/"
        + (target_slug or "all")
        + "/"
        + (slugify(repo_input) if repo_input is not None else "all")
    )
    aggregate_atoms = build_aggregate_metrics_atoms(
        records,
        eligible_run_rels,
        run_id_prefix=aggregate_run_id_prefix,
    )
    atoms.extend(aggregate_atoms)
    atoms = add_atom_links(atoms)
    agent_last_message_atoms = add_atom_links(agent_last_message_atoms)
    atoms = normalize_atom_lineage(
        atoms,
        case_registry=case_registry,
        strict_new_output=True,
    )
    if reopened_case_identity:
        reopened_normalized: list[dict[str, Any]] = []
        for atom in atoms:
            atom_id = _coerce_string(atom.get("atom_id"))
            if atom_id not in reopened_case_identity:
                reopened_normalized.append(atom)
                continue
            reopened = dict(atom)
            retained_case_id = reopened_case_identity[atom_id]
            reopened["disposition"] = "unresolved"
            reopened["disposition_status"] = "pending"
            reopened["disposition_receipt"] = None
            reopened["case_id"] = retained_case_id
            reopened["supporting_case_ids"] = []
            reopened_normalized.append(reopened)
        atoms = reopened_normalized
    agent_last_message_atoms = normalize_atom_lineage(
        agent_last_message_atoms,
        case_registry=case_registry,
        strict_new_output=True,
    )

    if qualification_prepare:
        seed_raw = getattr(args, "qualification_case_registry_seed", None)
        if repo_input is None or not isinstance(seed_raw, Path):
            print(
                "Qualification preparation requires local --repo-input and an explicit "
                "case-registry seed.",
                file=sys.stderr,
            )
            return 2
        protected_raw = getattr(args, "qualification_protected_path", [])
        protected_paths = (
            [path for path in protected_raw if isinstance(path, Path)]
            if isinstance(protected_raw, list)
            else []
        )
        try:
            assert qualification_source_snapshot is not None
            qualification_source_snapshot = extend_qualification_preparation_snapshot(
                qualification_source_snapshot,
                repo_root=repo_root,
                repo_input=Path(repo_input),
                research_ref=research_ref or "",
                source_runs_dir=runs_dir,
                atom_actions_path=atom_actions_path,
                case_registry_seed_path=seed_raw,
                target=target_slug,
                additional_evidence_runs_dirs=(
                    qualification_additional_evidence_runs_dirs
                ),
                atoms=atoms,
                protected_paths=protected_paths,
                owner_roots=owner_roots,
            )
            bundle = build_qualification_input_bundle(
                atoms=atoms,
                repo_root=repo_root,
                repo_input=Path(repo_input),
                research_ref=research_ref or "",
                source_runs_dir=runs_dir,
                atom_actions_path=atom_actions_path,
                case_registry_seed_path=seed_raw,
                target=target_slug,
                breadth_profile=breadth_profile,
                additional_evidence_runs_dirs=(
                    qualification_additional_evidence_runs_dirs
                ),
                protected_paths=protected_paths,
                owner_roots=owner_roots,
                extraction_metadata={
                    "atom_filter": {
                        "exclude_statuses": sorted(exclude_atom_status_set),
                        "eligible_atoms": len(atoms),
                        "excluded_atoms": len(excluded_atoms),
                        "excluded_status_counts": excluded_status_counts,
                        "reopened_unproven_atoms": len(reopened_atoms),
                        "reopened_status_counts": reopened_status_counts,
                    },
                    "source_record_count": len(records),
                    "additional_evidence_ingestion": additional_evidence_metadata,
                    "derived_evidence_ingestion": derived_evidence_meta,
                },
                preparation_input_snapshot=qualification_source_snapshot,
            )
            bundle_path = write_qualification_input_bundle(
                bundle,
                output_root=qualification_prepare_out_raw,
            )
        except (OSError, ValueError) as exc:
            print(f"[backlog] ERROR: qualification preparation failed: {exc}", file=sys.stderr)
            return 2
        print(bundle_path)
        print(
            json.dumps(
                {
                    "qualification_input_bundle_sha256": bundle["content_sha256"],
                    "eligible_atom_count": bundle["atom_corpus"]["count"],
                    "model_invocations": 0,
                    "ticket_mutations": 0,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if qualification_input_bundle is not None:
        bundle_scope = qualification_input_bundle.get("scope")
        bundle_scope = bundle_scope if isinstance(bundle_scope, Mapping) else {}
        if _coerce_string(bundle_scope.get("breadth_profile")) != breadth_profile:
            print("Qualification input bundle breadth-profile mismatch.", file=sys.stderr)
            return 2
        bundled_atoms_raw = qualification_input_bundle.get("atoms")
        bundled_atoms = (
            [dict(atom) for atom in bundled_atoms_raw if isinstance(atom, Mapping)]
            if isinstance(bundled_atoms_raw, list)
            else []
        )
        try:
            registry_seed_path = _qualification_case_registry_seed_path(
                qualification_input_bundle
            )
            if registry_seed_path != registry_source:
                raise ValueError("qualification registry seed changed during execution")
        except ValueError as exc:
            print(f"[backlog] ERROR: qualification registry seed invalid: {exc}", file=sys.stderr)
            return 2
        # The qualification bundle deliberately stores decision-free evidence so its
        # identity cannot be changed by a prior nondeterministic mining turn.  The
        # copied case registry is a separately sealed input, however, and must be
        # applied again at execution time.  Otherwise observations already attached
        # to canonical cases are presented to Stage 1 as novel evidence and duplicate
        # cases are mined from historical prose.
        atoms = _restore_sealed_qualification_lineage(
            bundled_atoms,
            case_registry=case_registry,
        )
        agent_last_message_atoms = [
            atom
            for atom in atoms
            if _coerce_string(atom.get("source")) == "agent_last_message_artifact"
        ]
        eligible_atoms_trackable = len(atoms)

    atom_totals = _summarize_atoms_for_totals(atoms)
    atoms_doc = dict(atoms_doc_raw)
    atoms_doc["atoms"] = atoms
    atoms_doc["derived_evidence_ingestion"] = derived_evidence_meta
    totals_raw = atoms_doc_raw.get("totals")
    totals_dict = dict(totals_raw) if isinstance(totals_raw, dict) else {}
    totals_dict.update(atom_totals)
    atoms_doc["totals"] = totals_dict
    atoms_doc["atom_filter"] = {
        "exclude_statuses": sorted(exclude_atom_status_set),
        "carryover": carryover_meta,
        "eligible_atoms": len(atoms),
        "eligible_atoms_trackable": eligible_atoms_trackable,
        "excluded_sources": [],
        "excluded_source_counts": {},
        "source_roots": {
            "primary": str(runs_dir.resolve()),
            "derived": [str(implementation_runs_root.resolve())],
        },
        "primary_records": len(records),
        "derived_records": derived_evidence_meta["records_ingested"],
        "derived_atoms": derived_evidence_meta["atoms_ingested"],
        "derived_binding_status_counts": derived_evidence_meta["binding_atom_status_counts"],
        "operational_failure_candidates": derived_evidence_meta["operational_failure_candidates"],
        "mirrored_diagnostic_sources": ["agent_last_message_artifact"],
        "mirrored_source_counts": {"agent_last_message_artifact": len(agent_last_message_atoms)},
        "mirrored_source_atoms_jsonl": str(agent_last_message_atoms_jsonl),
        "synthetic_atoms_added": len(aggregate_atoms),
        "excluded_atoms": len(excluded_atoms),
        "excluded_status_counts": excluded_status_counts,
        "default_status_filter": default_status_filter,
        "reopened_unproven_atoms": len(reopened_atoms),
        "reopened_status_counts": reopened_status_counts,
        "reopened_reason_counts": reopened_reason_counts,
        "reopened_atom_ids_preview": [
            atom_id
            for atom in reopened_atoms[:200]
            for atom_id in [_coerce_string(atom.get("atom_id"))]
            if atom_id is not None
        ],
        "preserved_open_case_status_counts": preserved_open_case_status_counts,
        "plan_folder_sync": plan_sync_meta,
        "case_outcome_sync": case_outcome_sync,
        "stale_actioned_reset": stale_actioned_reset,
        "excluded_atom_ids_preview": [
            atom_id
            for atom in excluded_atoms[:200]
            for atom_id in [_coerce_string(atom.get("atom_id"))]
            if atom_id is not None
        ],
    }
    if preexisting_stage3_resume_document is None:
        write_backlog_atoms(atoms_doc, atoms_jsonl)
        write_backlog_atoms({"atoms": agent_last_message_atoms}, agent_last_message_atoms_jsonl)

    sample_size = int(args.sample_size)
    if sample_size < 0:
        raise ValueError("--sample-size must be >= 0")
    sample_size_semantics = "all_atoms" if sample_size == 0 else "fixed_sample"
    seed = int(args.seed)
    resume = bool(args.resume)
    force = bool(args.force)
    dry_run = bool(args.dry_run)
    agent = str(args.agent)
    model = str(args.model) if isinstance(args.model, str) and args.model.strip() else None

    legacy_one_pass_flags: list[str] = []
    if int(args.miners) != 10:
        legacy_one_pass_flags.append(f"--miners={int(args.miners)}")
    if int(args.sample_size) != 120:
        legacy_one_pass_flags.append(f"--sample-size={int(args.sample_size)}")
    if int(args.coverage_miners) != 3:
        legacy_one_pass_flags.append(f"--coverage-miners={int(args.coverage_miners)}")
    if args.bagging_miners is not None:
        legacy_one_pass_flags.append(f"--bagging-miners={int(args.bagging_miners)}")
    if int(args.max_tickets_per_miner) != 12:
        legacy_one_pass_flags.append(f"--max-tickets-per-miner={int(args.max_tickets_per_miner)}")
    if int(args.orphan_pass) != 1:
        legacy_one_pass_flags.append(f"--orphan-pass={int(args.orphan_pass)}")
    if seed != 0:
        legacy_one_pass_flags.append(f"--seed={seed}")
    if not resume:
        legacy_one_pass_flags.append("--no-resume")
    if force:
        legacy_one_pass_flags.append("--force")
    if bool(args.no_merge):
        legacy_one_pass_flags.append("--no-merge")
    merge_candidate_threshold = float(args.merge_candidate_threshold)
    if not (0.0 <= merge_candidate_threshold <= 1.0):
        raise ValueError("--merge-candidate-threshold must be in [0, 1]")
    if merge_candidate_threshold != 0.65:
        legacy_one_pass_flags.append(f"--merge-candidate-threshold={merge_candidate_threshold:g}")
    if bool(args.merge_keep_anchor_pairs):
        legacy_one_pass_flags.append("--merge-keep-anchor-pairs")
    if int(args.labelers) != 3:
        legacy_one_pass_flags.append(f"--labelers={int(args.labelers)}")

    if legacy_one_pass_flags:
        print(
            "[backlog] NOTE: legacy one-pass knobs are ignored by the six-stage pipeline: "
            + " ".join(legacy_one_pass_flags),
            file=sys.stderr,
        )

    policy_cfg: BacklogPolicyConfig | None = None
    policy_config_path: Path | None = policy_config_default_path
    if not bool(args.no_policy) and policy_config_path is not None and policy_config_path.exists():
        policy_root = _load_yaml(policy_config_path).get("backlog_policy")
        if policy_root is None:
            raise ValueError(f"Expected backlog_policy key in {policy_config_path}")
        if not isinstance(policy_root, dict):
            raise ValueError(
                f"Expected mapping at backlog_policy in {policy_config_path}, got "
                f"{type(policy_root).__name__}"
            )
        policy_cfg = BacklogPolicyConfig.from_dict(policy_root)
    if non_exporting_shadow and (policy_cfg is None or policy_config_path is None):
        print(
            "Shadow backlog cycles require an enabled, explicit backlog policy config.",
            file=sys.stderr,
        )
        return 2

    # ---------------------------------------------------------------------------
    # Six-stage backlog pipeline (canonical, milestone 6).
    # ---------------------------------------------------------------------------
    pipeline_manifest_path = prompts_dir / "pipeline_manifest.json"
    if not pipeline_manifest_path.exists():
        print(
            f"Missing six-stage pipeline manifest: {pipeline_manifest_path} "
            "(expected under --prompts-dir).",
            file=sys.stderr,
        )
        return 2

    try:
        pipeline_manifest = load_pipeline_prompt_manifest(prompts_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    problem_records_json = out_json.parent / f"{default_name}.problem_records.json"
    problem_records_md = out_json.parent / f"{default_name}.problem_records.md"
    problem_mining_evidence_json = problem_records_json.with_name(
        f"{problem_records_json.stem}.evidence_receipt.json"
    )
    prioritized_json = out_json.parent / f"{default_name}.prioritized_problems.json"
    prioritized_md = out_json.parent / f"{default_name}.prioritized_problems.md"
    research_json = out_json.parent / f"{default_name}.research.json"
    research_md = out_json.parent / f"{default_name}.research.md"
    solution_options_json = out_json.parent / f"{default_name}.solution_options.json"
    solution_options_md = out_json.parent / f"{default_name}.solution_options.md"
    solution_selection_json = out_json.parent / f"{default_name}.solution_selection.json"
    solution_selection_md = out_json.parent / f"{default_name}.solution_selection.md"
    change_plans_json = out_json.parent / f"{default_name}.change_plans.json"
    change_plans_md = out_json.parent / f"{default_name}.change_plans.md"

    try:
        stage1_relation_resume = (
            _load_stage1_relation_resume(
                problem_records_path=problem_records_json,
                artifacts_dir=artifacts_dir,
            )
            if resume and preexisting_stage3_resume_document is None
            else None
        )
        stage3_resume_document: dict[str, Any] | None = None
        retained_stage1: dict[str, Any] | None = None
        retained_stage2: dict[str, Any] | None = None
        if preexisting_stage3_resume_document is not None:
            retained_stage1, retained_stage2, case_registry = _load_stage3_resume_upstream(
                stage3_document=preexisting_stage3_resume_document,
                expected_paths={
                    "atoms": atoms_jsonl,
                    "problem_records": problem_records_json,
                    "problem_mining_evidence": problem_mining_evidence_json,
                    "prioritized_problems": prioritized_json,
                    "case_registry": case_registry_json,
                },
                target_slug=target_slug,
                repo_input=repo_input,
                research_ref=research_ref,
                current_atoms=atoms,
            )
            stage3_resume_document = preexisting_stage3_resume_document

        stage1_guidance = pipeline_manifest.load_stage_guidance("problem_mining")
        stage1_doc = (
            retained_stage1
            if retained_stage1 is not None
            else stage1_relation_resume["stage_doc"]
            if stage1_relation_resume is not None
            else _run_problem_mining_stage(
                repo_root=repo_root,
                atoms=atoms,
                pipeline_manifest=pipeline_manifest,
                artifacts_dir=artifacts_dir,
                out_json=problem_records_json,
                out_md=problem_records_md,
                agent=agent,
                model=model,
                cfg=cfg,
                dry_run=dry_run,
                stage_guidance_text=stage1_guidance,
                case_registry=case_registry,
            )
        )

        items1_raw = stage1_doc.get("items") if isinstance(stage1_doc, dict) else None
        newly_mined_problem_records = (
            [item for item in items1_raw if isinstance(item, dict)]
            if isinstance(items1_raw, list)
            else []
        )

        current_case_ids = {
            case_id
            for item in newly_mined_problem_records
            for case_id in [_coerce_string(item.get("case_id"))]
            if case_id is not None
        }
        carried_problem_records: list[dict[str, Any]] = []
        for historical_case in problem_case_records_from_registry(case_registry):
            historical_case_id = _coerce_string(historical_case.get("case_id"))
            state = _coerce_string(historical_case.get("case_state")) or "active"
            if historical_case_id in current_case_ids:
                continue
            if state in TERMINAL_CASE_STATES:
                continue
            carried = dict(historical_case)
            carried["_carried_forward_case"] = True
            carried_problem_records.append(carried)
        if retained_stage1 is not None:
            # A provider-wait resume reuses the exact sealed Stage-1 payload.  Recomputing
            # carry-forward metadata here would create a new upstream document even though no
            # mining or relation decision was rerun.
            problem_records = [dict(item) for item in newly_mined_problem_records]
        else:
            problem_records = [*newly_mined_problem_records, *carried_problem_records]
            stage1_doc = dict(stage1_doc)
            stage1_doc["items"] = problem_records
            stage1_meta_raw = stage1_doc.get("input_meta")
            stage1_meta = dict(stage1_meta_raw) if isinstance(stage1_meta_raw, dict) else {}
            stage1_meta.update(
                {
                    "newly_mined_case_count": len(newly_mined_problem_records),
                    "carried_forward_active_case_count": len(carried_problem_records),
                }
            )
            stage1_doc["input_meta"] = stage1_meta

        if stage3_resume_document is None:
            stage1_doc, problem_records, atoms, case_registry = _run_problem_case_relation_review(
                stage_doc=stage1_doc,
                problem_records=problem_records,
                atoms=atoms,
                pipeline_manifest=pipeline_manifest,
                artifacts_dir=artifacts_dir,
                out_json=problem_records_json,
                out_md=problem_records_md,
                case_registry_path=case_registry_json,
                previous_case_registry=case_registry,
                agent=agent,
                model=model,
                cfg=cfg,
                dry_run=dry_run,
                stage_guidance_text=stage1_guidance,
                relation_decisions_override=(
                    stage1_relation_resume["decisions"]
                    if stage1_relation_resume is not None
                    else None
                ),
                relation_review_batches_override=(
                    stage1_relation_resume["batches"]
                    if stage1_relation_resume is not None
                    else None
                ),
                relation_manifest_refs=(
                    stage1_relation_resume["manifest_refs"]
                    if stage1_relation_resume is not None
                    else None
                ),
            )
        problem_records = _attach_current_case_registry_context(
            problem_records,
            case_registry=case_registry,
        )
        if stage3_resume_document is None:
            stage1_doc = dict(stage1_doc)
            stage1_doc["items"] = problem_records
            stage1_doc["item_count"] = len(problem_records)
            problem_records_json.write_text(
                json.dumps(stage1_doc, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        _require_stage_model_invocation_provenance(stage1_doc)
        atoms_doc["atoms"] = atoms
        atoms_doc["atom_dispositions"] = atom_disposition_summary(atoms)
        if stage3_resume_document is None:
            write_backlog_atoms(atoms_doc, atoms_jsonl)
        if stage3_resume_document is None:
            case_registry = _persist_case_registry_stage_lineage(
                case_registry=case_registry,
                case_registry_path=case_registry_json,
                stage_doc=stage1_doc,
            )

        stage2_guidance = pipeline_manifest.load_stage_guidance("problem_prioritization")
        stage2_doc = (
            retained_stage2
            if retained_stage2 is not None
            else _run_problem_prioritization_stage(
                atoms=atoms,
                problem_records=problem_records,
                pipeline_manifest=pipeline_manifest,
                artifacts_dir=artifacts_dir,
                out_json=prioritized_json,
                out_md=prioritized_md,
                agent=agent,
                model=model,
                cfg=cfg,
                dry_run=dry_run,
                stage_guidance_text=stage2_guidance,
            )
        )
        _require_stage_model_invocation_provenance(stage2_doc)

        items2_raw = stage2_doc.get("items") if isinstance(stage2_doc, dict) else None
        priority_decisions = (
            [item for item in items2_raw if isinstance(item, dict)]
            if isinstance(items2_raw, list)
            else []
        )
        records_by_problem_id = {
            str(record["problem_id"]): record
            for record in problem_records
            if isinstance(record, dict) and isinstance(record.get("problem_id"), str)
        }
        reused_downstream_chains_by_problem_id: dict[str, dict[str, Any]] = {}
        for decision in priority_decisions:
            route = _coerce_string(decision.get("research_route"))
            if route not in {"continue_downstream", "await_outcome"}:
                continue
            problem_id = _coerce_string(decision.get("problem_id"))
            record = records_by_problem_id.get(problem_id or "")
            if record is None:
                raise ValueError(
                    f"retained_downstream_problem_record_missing:{problem_id or '(missing)'}"
                )
            if route == "await_outcome":
                chain, chain_errors = hydrate_retained_downstream_chain(record)
                if chain is not None and not chain_errors:
                    reused_downstream_chains_by_problem_id[str(problem_id)] = chain
                    continue
                # A stale downstream cache is not a case failure. Reuse research when it is
                # still current and let the ordinary Stage 4-6 path rebuild the chain.
                dossier, research_errors = hydrate_retained_research_proof(record)
                if dossier is not None and not research_errors:
                    decision.update(
                        {
                            "research_route": "continue_downstream",
                            "selected_for_research": False,
                            "eligible_for_downstream": True,
                            "route_reason": (
                                "The retained downstream chain changed before consumption; "
                                "research remains current and the normal downstream path will "
                                "self-heal it. First chain result: "
                                + (chain_errors[0] if chain_errors else "chain_unavailable")
                                + "."
                            ),
                        }
                    )
                else:
                    decision.update(
                        {
                            "research_route": "research_update",
                            "selected_for_research": True,
                            "eligible_for_downstream": True,
                            "route_reason": (
                                "The retained research and downstream chain changed before "
                                "consumption; fresh research is required. First research result: "
                                + (
                                    research_errors[0]
                                    if research_errors
                                    else "research_unavailable"
                                )
                                + "."
                            ),
                        }
                    )
            elif route == "continue_downstream":
                dossier, research_errors = hydrate_retained_research_proof(record)
                if dossier is None or research_errors:
                    decision.update(
                        {
                            "research_route": "research_update",
                            "selected_for_research": True,
                            "eligible_for_downstream": True,
                            "route_reason": (
                                "The retained research changed before consumption; fresh "
                                "research is required. First result: "
                                + (
                                    research_errors[0]
                                    if research_errors
                                    else "research_unavailable"
                                )
                                + "."
                            ),
                        }
                    )
        if stage3_resume_document is None:
            stage2_doc = dict(stage2_doc)
            stage2_doc["items"] = priority_decisions
            stage2_doc["item_count"] = len(priority_decisions)
            stage2_doc, priority_decisions = _persist_downstream_case_lineage(
                stage_doc=stage2_doc,
                out_json=prioritized_json,
                problem_cases=problem_records,
            )
            case_registry = _persist_case_registry_stage_lineage(
                case_registry=case_registry,
                case_registry_path=case_registry_json,
                stage_doc=stage2_doc,
            )
        selected_priority = sorted(
            (dec for dec in priority_decisions if dec.get("selected_for_research") is True),
            key=_research_dispatch_sort_key,
        )
        reused_research_dossiers: list[dict[str, Any]] = []
        for decision in priority_decisions:
            route = decision.get("research_route")
            if route not in {"continue_downstream", "await_outcome"}:
                continue
            problem_id = _coerce_string(decision.get("problem_id"))
            record = records_by_problem_id.get(problem_id or "")
            if record is None:
                raise ValueError(
                    f"stage3_retained_research_problem_record_missing:{problem_id or '(missing)'}"
                )
            chain = reused_downstream_chains_by_problem_id.get(problem_id or "")
            dossier = (
                dict(chain["research_dossier"])
                if isinstance(chain, Mapping) and isinstance(chain.get("research_dossier"), Mapping)
                else None
            )
            hydration_errors: list[str] = []
            if dossier is None:
                dossier, hydration_errors = hydrate_retained_research_proof(record)
            if dossier is None or hydration_errors:
                raise ValueError(
                    "stage3_retained_research_hydration_changed:"
                    + problem_id
                    + ":"
                    + ",".join(hydration_errors or ["proof_unavailable"])
                )
            reused_research_dossiers.append(dossier)

        resolved_repo_input = repo_input
        if resolved_repo_input is None:
            raw_repo_inputs: list[str] = []
            for record in records:
                if not isinstance(record, dict):
                    continue
                target_ref = record.get("target_ref")
                if not isinstance(target_ref, dict):
                    continue
                candidate = _coerce_string(target_ref.get("repo_input"))
                if candidate is not None:
                    raw_repo_inputs.append(candidate)

            normalized_repo_inputs: dict[str, str] = {}
            for candidate in sorted(set(raw_repo_inputs)):
                resolved = candidate.strip()
                norm_key = resolved
                if _looks_like_local_repo_input(resolved):
                    resolved_path = (
                        _resolve_local_repo_root(repo_root, resolved) or Path(resolved).expanduser()
                    )
                    try:
                        resolved_path = resolved_path.resolve()
                    except OSError:
                        pass
                    resolved = str(resolved_path)
                    norm_key = os.path.normcase(os.path.normpath(resolved))
                if norm_key not in normalized_repo_inputs:
                    normalized_repo_inputs[norm_key] = resolved

            if len(normalized_repo_inputs) == 1:
                resolved_repo_input = next(iter(normalized_repo_inputs.values()))
                print(
                    f"[stage3] inferred repo_input from run history: {resolved_repo_input}",
                    file=sys.stderr,
                )
            elif len(normalized_repo_inputs) > 1:
                preview = ", ".join(list(normalized_repo_inputs.values())[:4])
                suffix = " …" if len(normalized_repo_inputs) > 4 else ""
                print(
                    "[stage3] WARNING: multiple repo_inputs found in run history; "
                    "provide --repo-input to enable stage 3 repro+research. "
                    f"(unique_after_normalization={len(normalized_repo_inputs)} preview={preview}{suffix})",
                    file=sys.stderr,
                )

        if selected_priority and not resolved_repo_input and not dry_run:
            print(
                "[stage3] Missing repo_input for repro+research. Provide --repo-input "
                "or ensure run history contains exactly one local repo_input.",
                file=sys.stderr,
            )
            return 2
        if selected_priority and not research_ref and not dry_run:
            print(
                "[stage3] Missing source-of-truth research ref. Pass --research-ref or "
                "configure backlog_research.source_ref.",
                file=sys.stderr,
            )
            return 2

        if dry_run or not selected_priority:
            # Stages 1-2 and empty/dry-run stage 3 do not execute experiments.
            # Do not reject those useful mining runs merely because a
            # repository-backed replay boundary is not needed yet.  A real
            # selected research case still fails above without a repository.
            replay_executor = BlockedReplayExecutor(reason="stage3_repository_not_required")
            replay_executor_metadata = {
                "executor": "blocked",
                "reason": "stage3_repository_not_required",
            }
        else:
            try:
                replay_executor, replay_executor_metadata = _configured_replay_executor(
                    research_config=research_config,
                    repo_root=repo_root,
                    repo_input=resolved_repo_input,
                )
            except ValueError as exc:
                print(
                    f"Invalid backlog research replay config: {exc}",
                    file=sys.stderr,
                )
                return 2

        stage3_resume_upstream = _stage3_resume_upstream_contract(
            paths={
                "atoms": atoms_jsonl,
                "problem_records": problem_records_json,
                "problem_mining_evidence": problem_mining_evidence_json,
                "prioritized_problems": prioritized_json,
                "case_registry": case_registry_json,
            },
            source_atoms=atoms,
            target_slug=target_slug,
            repo_input=repo_input,
            research_ref=research_ref,
            selected_problem_ids=[
                str(item["problem_id"])
                for item in selected_priority
                if isinstance(item.get("problem_id"), str)
            ],
        )
        if stage3_resume_document is None and completed_stage3_resume_candidate is not None:
            try:
                _load_stage3_resume_upstream(
                    stage3_document=completed_stage3_resume_candidate,
                    expected_paths={
                        "atoms": atoms_jsonl,
                        "problem_records": problem_records_json,
                        "problem_mining_evidence": problem_mining_evidence_json,
                        "prioritized_problems": prioritized_json,
                        "case_registry": case_registry_json,
                    },
                    target_slug=target_slug,
                    repo_input=repo_input,
                    research_ref=research_ref,
                    current_atoms=atoms,
                )
                candidate_meta_raw = completed_stage3_resume_candidate.get("input_meta")
                candidate_meta = (
                    candidate_meta_raw if isinstance(candidate_meta_raw, Mapping) else {}
                )
                if candidate_meta.get("resume_upstream") != stage3_resume_upstream:
                    raise ValueError("stage3_completed_resume_upstream_contract_changed")
            except ValueError as exc:
                print(
                    "[stage3] NOTE: completed research cache invalidated by current "
                    f"upstream inputs; running Stage 3 normally ({exc}).",
                    file=sys.stderr,
                )
            else:
                stage3_resume_document = completed_stage3_resume_candidate
        stage3_doc = _run_repro_research_stage(
            repo_root=repo_root,
            repo_input=resolved_repo_input,
            repo_ref=research_ref,
            target_slug=target_slug,
            selected_priority_decisions=selected_priority,
            problem_records=problem_records,
            atoms=atoms,
            artifacts_dir=artifacts_dir,
            out_json=research_json,
            out_md=research_md,
            agent=agent,
            model=model,
            cfg=cfg,
            dry_run=dry_run,
            replay_timeout_seconds=replay_timeout_seconds,
            replay_executor=replay_executor,
            replay_executor_metadata=replay_executor_metadata,
            resume_stage_document=stage3_resume_document,
            reused_research_dossiers=reused_research_dossiers,
            resume_upstream_contract=stage3_resume_upstream,
        )

        items3_raw = stage3_doc.get("items") if isinstance(stage3_doc, dict) else None
        research_dossiers = (
            [item for item in items3_raw if isinstance(item, dict)]
            if isinstance(items3_raw, list)
            else []
        )
        stage3_doc, research_dossiers = _persist_downstream_case_lineage(
            stage_doc=stage3_doc,
            out_json=research_json,
            problem_cases=problem_records,
        )
        case_registry = _persist_case_registry_stage_lineage(
            case_registry=case_registry,
            case_registry_path=case_registry_json,
            stage_doc=stage3_doc,
        )
        stage3_external_wait = _stage3_provider_external_wait(stage3_doc)
        if stage3_external_wait is not None:
            stage3_resume_upstream = _stage3_resume_upstream_contract(
                paths={
                    "atoms": atoms_jsonl,
                    "problem_records": problem_records_json,
                    "problem_mining_evidence": problem_mining_evidence_json,
                    "prioritized_problems": prioritized_json,
                    "case_registry": case_registry_json,
                },
                source_atoms=atoms,
                target_slug=target_slug,
                repo_input=repo_input,
                research_ref=research_ref,
                selected_problem_ids=[
                    str(item["problem_id"])
                    for item in selected_priority
                    if isinstance(item.get("problem_id"), str)
                ],
            )
            stage3_doc = dict(stage3_doc)
            stage3_meta_raw = stage3_doc.get("input_meta")
            stage3_meta = dict(stage3_meta_raw) if isinstance(stage3_meta_raw, dict) else {}
            stage3_meta["resume_upstream"] = stage3_resume_upstream
            stage3_doc["input_meta"] = stage3_meta
            _atomic_write_research_json(research_json, stage3_doc)
            print(
                "[stage3] PARKED: signed-in Codex subscription usage limit; "
                "Stages 4-6 were not dispatched. Resume this same pipeline invocation after the "
                "provider reset to continue the retained author/session frontier. API billing "
                "fallback remains disabled. "
                f"checkpoint={stage3_external_wait.get('checkpoint_sha256')}",
                file=sys.stderr,
            )
            return 2

        post_research_split_dir = artifacts_dir / "repro_research" / "post_research_case_splits_001"
        post_research_splits = apply_post_research_relation_assessments(
            problem_records=problem_records,
            priority_decisions=priority_decisions,
            research_dossiers=research_dossiers,
            atoms=atoms,
            receipt_dir=post_research_split_dir,
        )
        post_research_split_groups = post_research_splits["split_groups"]
        split_parent_dossiers = post_research_splits["split_parent_dossiers"]
        if post_research_split_groups:
            problem_records = post_research_splits["problem_records"]
            priority_decisions = post_research_splits["priority_decisions"]
            research_dossiers = post_research_splits["research_dossiers"]
            atoms = post_research_splits["atoms"]
            atoms_doc["atoms"] = atoms
            atoms_doc["atom_dispositions"] = atom_disposition_summary(atoms)
            write_backlog_atoms(atoms_doc, atoms_jsonl)
            case_registry = build_case_registry(
                problem_records,
                previous=case_registry,
                supporting_atoms=atoms,
            )
            write_case_registry(case_registry_json, case_registry)

        post_research_relations = collapse_post_research_verified_mechanisms(
            problem_records=problem_records,
            priority_decisions=priority_decisions,
            research_dossiers=research_dossiers,
            case_registry=case_registry,
        )
        post_research_groups = post_research_relations["groups"]
        response_path: Path | None = None
        relation_receipt_path: Path | None = None
        if post_research_groups:
            problem_records = post_research_relations["problem_records"]
            priority_decisions = post_research_relations["priority_decisions"]
            research_dossiers = post_research_relations["research_dossiers"]
            case_registry = build_case_registry(
                problem_records,
                previous=case_registry,
                supporting_atoms=atoms,
            )
            relation_dir = (
                artifacts_dir / "repro_research" / "post_research_verified_mechanism_relations_001"
            )
            relation_dir.mkdir(parents=True, exist_ok=True)
            response_path = relation_dir / (
                "post_research_verified_mechanism_relations_001.response.txt"
            )
            response_path.write_text(
                json.dumps(post_research_groups, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            post_research_canonical_case_ids = {
                str(group["canonical_case_id"])
                for group in post_research_groups
                if isinstance(group, dict) and group.get("canonical_case_id")
            }
            _, relation_receipt_path = _persist_canonical_relation_receipts(
                canonical_records=[
                    record
                    for record in problem_records
                    if record.get("case_id") in post_research_canonical_case_ids
                ],
                registry=case_registry,
                review_response_path=response_path,
                receipt_path=relation_dir
                / "post_research_verified_mechanism_relations_001.relations.json",
                stage="repro_research",
            )
            write_case_registry(case_registry_json, case_registry)

        if post_research_groups or post_research_split_groups:
            stage3_artifact_updates: dict[str, Any] = {}
            if post_research_split_groups:
                stage3_artifact_updates["post_research_split_receipt_dir"] = str(
                    post_research_split_dir
                )
            if response_path is not None and relation_receipt_path is not None:
                stage3_artifact_updates.update(
                    {
                        "post_research_relation_response": str(response_path),
                        "post_research_relation_receipt": str(relation_receipt_path),
                    }
                )
            stage3_doc = _annotate_completed_stage3_document(
                stage3_doc,
                input_meta_updates={
                    "post_research_relation_review": (
                        "runner_authenticated_relation_assessment_v1;"
                        "runner_verified_mechanism_identity_v2"
                    ),
                    "post_research_split_groups": post_research_split_groups,
                    "post_research_split_receipts": post_research_splits["split_receipts"],
                    "post_research_split_parent_count": len(split_parent_dossiers),
                    "post_research_relation_groups": post_research_groups,
                    "post_research_case_aliases": post_research_relations["case_aliases"],
                    "post_research_canonical_case_count": len(problem_records),
                    "post_research_canonical_research_count": len(research_dossiers),
                },
                artifact_updates=stage3_artifact_updates,
            )
            research_json.write_text(
                json.dumps(stage3_doc, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            case_registry = _persist_case_registry_stage_lineage(
                case_registry=case_registry,
                case_registry_path=case_registry_json,
                stage_doc=stage3_doc,
            )

        # Stage-lineage persistence updates the case registry after the core Stage-3
        # document is built. Refresh the upstream receipt to the exact durable files a
        # later completed-cache resume will read; otherwise a crash immediately before
        # Stage 4 would reject its own successfully persisted research.
        stage3_resume_upstream = _stage3_resume_upstream_contract(
            paths={
                "atoms": atoms_jsonl,
                "problem_records": problem_records_json,
                "problem_mining_evidence": problem_mining_evidence_json,
                "prioritized_problems": prioritized_json,
                "case_registry": case_registry_json,
            },
            source_atoms=atoms,
            target_slug=target_slug,
            repo_input=repo_input,
            research_ref=research_ref,
            selected_problem_ids=[
                str(item["problem_id"])
                for item in selected_priority
                if isinstance(item.get("problem_id"), str)
            ],
        )
        stage3_doc = dict(stage3_doc)
        stage3_meta_raw = stage3_doc.get("input_meta")
        stage3_meta = dict(stage3_meta_raw) if isinstance(stage3_meta_raw, Mapping) else {}
        stage3_meta["resume_upstream"] = stage3_resume_upstream
        stage3_doc["input_meta"] = stage3_meta
        _atomic_write_research_json(research_json, stage3_doc)

        research_by_problem_id = {
            str(dossier["problem_id"]): dossier
            for dossier in research_dossiers
            if isinstance(dossier, dict) and isinstance(dossier.get("problem_id"), str)
        }
        current_records_by_problem_id = {
            str(record["problem_id"]): record
            for record in problem_records
            if isinstance(record, dict) and isinstance(record.get("problem_id"), str)
        }
        valid_reused_chains_by_problem_id: dict[str, dict[str, Any]] = {}
        for problem_id, chain in reused_downstream_chains_by_problem_id.items():
            dossier = research_by_problem_id.get(problem_id)
            record = current_records_by_problem_id.get(problem_id)
            if (
                dossier is not None
                and record is not None
                and _coerce_string(record.get("case_id")) == _coerce_string(chain.get("case_id"))
                and chain_matches_research_dossier(chain, dossier)
            ):
                valid_reused_chains_by_problem_id[problem_id] = chain
                continue
            # Post-research canonicalization changed the causal work unit. Preserve the
            # current research result, but rebuild options/plans for the new unit.
            for decision in priority_decisions:
                if _coerce_string(decision.get("problem_id")) == problem_id:
                    decision["research_route"] = "continue_downstream"
                    decision["selected_for_research"] = False
                    decision["eligible_for_downstream"] = True
                    decision["route_reason"] = (
                        "Post-research canonicalization changed the retained chain identity; "
                        "the normal downstream path will rebuild it from current research."
                    )
        await_outcome_problem_ids = set(valid_reused_chains_by_problem_id)
        reused_chains = [
            valid_reused_chains_by_problem_id[problem_id]
            for problem_id in sorted(valid_reused_chains_by_problem_id)
        ]
        fresh_problem_records = [
            record
            for record in problem_records
            if _coerce_string(record.get("problem_id")) not in await_outcome_problem_ids
        ]
        fresh_priority_decisions = [
            decision
            for decision in priority_decisions
            if _coerce_string(decision.get("problem_id")) not in await_outcome_problem_ids
        ]
        fresh_research_dossiers = [
            dossier
            for dossier in research_dossiers
            if _coerce_string(dossier.get("problem_id")) not in await_outcome_problem_ids
        ]

        target_repo_roots_by_problem: dict[str, Path] = {}
        for dossier in fresh_research_dossiers:
            research_ready, _research_blockers = assess_research_readiness(dossier)
            if not research_ready:
                continue
            receipt_ready, receipt_blockers = verify_persisted_research_evidence(dossier)
            if not receipt_ready:
                raise ValueError(
                    "planning_research_receipt_invalid: "
                    f"problem_id={dossier.get('problem_id')!r} "
                    f"reasons={','.join(receipt_blockers)}"
                )
            pid = _coerce_string(dossier.get("problem_id"))
            workspace_raw = _coerce_string(dossier.get("repo_workspace"))
            research_revision = _coerce_string(dossier.get("repo_revision"))
            verification_raw = dossier.get("evidence_verification")
            verification = verification_raw if isinstance(verification_raw, dict) else {}
            attested_workspace = _coerce_string(verification.get("planning_workspace_dir"))
            attested_head = _coerce_string(verification.get("planning_workspace_head"))
            if (
                pid is None
                or workspace_raw is None
                or research_revision is None
                or verification.get("status") != "verified"
                or verification.get("planning_workspace_clean") is not True
                or attested_workspace != workspace_raw
                or attested_head != research_revision
            ):
                raise ValueError(
                    "planning_target_workspace_unverified: "
                    f"problem_id={pid!r} workspace={workspace_raw!r} "
                    f"revision={research_revision!r}"
                )
            workspace = Path(workspace_raw).expanduser().resolve()
            if not workspace.is_dir():
                raise ValueError(
                    "planning_target_workspace_missing: "
                    f"problem_id={pid!r} workspace={str(workspace)!r}"
                )
            target_repo_roots_by_problem[pid] = workspace

        taxonomy = pipeline_manifest.load_taxonomy()
        families_raw = taxonomy.get("solution_families")
        families = (
            [family for family in families_raw if isinstance(family, dict)]
            if isinstance(families_raw, list)
            else []
        )
        family_order: list[str] = []
        family_labels_by_id: dict[str, str] = {}
        for family in families:
            family_id = _coerce_string(family.get("family_id"))
            if family_id is None:
                continue
            family_order.append(family_id)
            family_labels_by_id[family_id] = _coerce_string(family.get("label")) or family_id
        problem_records_by_id = {
            str(record["problem_id"]): record
            for record in problem_records
            if isinstance(record.get("problem_id"), str)
        }

        reused_solution_options = flatten_chain_items(reused_chains, "solution_options")
        stage4_fresh_doc = _run_fresh_downstream_stage(
            fresh_problem_records=fresh_problem_records,
            reused_chains=reused_chains,
            run_stage=lambda: _run_solution_optioning_stage(
                repo_root=repo_root,
                target_repo_roots_by_problem=target_repo_roots_by_problem,
                atoms=atoms,
                problem_records=fresh_problem_records,
                priority_decisions=fresh_priority_decisions,
                research_dossiers=fresh_research_dossiers,
                pipeline_manifest=pipeline_manifest,
                artifacts_dir=artifacts_dir,
                out_json=solution_options_json,
                out_md=solution_options_md,
                agent=agent,
                model=model,
                cfg=cfg,
                dry_run=dry_run,
                breadth_profile=breadth_profile,
                stage_guidance_text=pipeline_manifest.load_stage_guidance("solution_optioning"),
            ),
        )
        if stage4_fresh_doc is not None:
            _require_stage_model_invocation_provenance(stage4_fresh_doc)
        stage4_doc = _merge_reused_downstream_stage_document(
            stage="solution_optioning",
            stage_doc=stage4_fresh_doc,
            reused_items=reused_solution_options,
            agent=agent,
            dry_run=dry_run,
            artifacts={
                "solution_options_json": str(solution_options_json),
                "solution_options_md": str(solution_options_md),
            },
            count_updates={
                "problem_record_count": len(problem_records),
                "priority_decision_count": len(priority_decisions),
                "research_dossier_count": len(research_dossiers),
                "fresh_problem_record_count": len(fresh_problem_records),
                "reused_case_count": len(reused_chains),
                "solution_optioning_status": "ok",
            },
        )
        _require_stage_model_invocation_provenance(stage4_doc)
        stage4_doc, solution_options = _persist_downstream_case_lineage(
            stage_doc=stage4_doc,
            out_json=solution_options_json,
            problem_cases=problem_records,
        )
        stage4_meta_raw = stage4_doc.get("input_meta")
        stage4_meta = stage4_meta_raw if isinstance(stage4_meta_raw, dict) else {}
        optioning_outcomes_raw = stage4_meta.get("optioning_outcomes")
        optioning_outcomes = (
            [item for item in optioning_outcomes_raw if isinstance(item, dict)]
            if isinstance(optioning_outcomes_raw, list)
            else []
        )
        solution_options_md.parent.mkdir(parents=True, exist_ok=True)
        solution_options_md.write_text(
            _render_solution_options_markdown(
                solution_options,
                problem_records_by_id=problem_records_by_id,
                family_order=family_order,
                family_labels_by_id=family_labels_by_id,
                optioning_outcomes_by_id={
                    str(item["problem_id"]): item
                    for item in optioning_outcomes
                    if isinstance(item.get("problem_id"), str)
                },
                title=(
                    f"{solution_options_json.stem.removesuffix('.solution_options')} "
                    "- Solution Options"
                ),
            ),
            encoding="utf-8",
        )
        case_registry = _persist_case_registry_stage_lineage(
            case_registry=case_registry,
            case_registry_path=case_registry_json,
            stage_doc=stage4_doc,
        )

        fresh_solution_options = [
            option
            for option in solution_options
            if _coerce_string(option.get("problem_id")) not in await_outcome_problem_ids
        ]
        stage4_for_selection = None
        if stage4_fresh_doc is not None:
            stage4_for_selection = dict(stage4_fresh_doc)
            stage4_for_selection["items"] = fresh_solution_options
            stage4_for_selection["item_count"] = len(fresh_solution_options)

        reused_selection_decisions = flatten_chain_items(
            reused_chains,
            "selection_decisions",
        )
        stage5_fresh_doc = _run_fresh_downstream_stage(
            fresh_problem_records=fresh_problem_records,
            reused_chains=reused_chains,
            run_stage=lambda: _run_solution_selection_stage(
                repo_root=repo_root,
                target_repo_roots_by_problem=target_repo_roots_by_problem,
                atoms=atoms,
                problem_records=fresh_problem_records,
                research_dossiers=fresh_research_dossiers,
                solution_options=fresh_solution_options,
                solution_optioning_stage_doc=stage4_for_selection,
                pipeline_manifest=pipeline_manifest,
                artifacts_dir=artifacts_dir,
                out_json=solution_selection_json,
                out_md=solution_selection_md,
                agent=agent,
                model=model,
                cfg=cfg,
                dry_run=dry_run,
                breadth_profile=breadth_profile,
                stage_guidance_text=pipeline_manifest.load_stage_guidance("solution_selection"),
            ),
        )
        if stage5_fresh_doc is not None:
            _require_stage_model_invocation_provenance(stage5_fresh_doc)

        stage5_meta_raw = (
            stage5_fresh_doc.get("input_meta") if isinstance(stage5_fresh_doc, dict) else None
        )
        stage5_meta = dict(stage5_meta_raw) if isinstance(stage5_meta_raw, dict) else {}
        option_revisions_raw = stage5_meta.get("option_revisions")
        option_revisions = (
            [item for item in option_revisions_raw if isinstance(item, dict)]
            if isinstance(option_revisions_raw, list)
            else []
        )
        for revision in option_revisions:
            revised_problem_id = _coerce_string(revision.get("problem_id"))
            revised_raw = revision.get("options")
            revised = (
                [item for item in revised_raw if isinstance(item, dict)]
                if isinstance(revised_raw, list)
                else []
            )
            if revised_problem_id is None or not revised:
                continue
            revised = propagate_case_lineage(
                revised,
                problem_records,
                strict_new_output=True,
            )
            revision["options"] = revised
            solution_options = [
                option
                for option in solution_options
                if _coerce_string(option.get("problem_id")) != revised_problem_id
            ]
            solution_options.extend(revised)
        if stage5_fresh_doc is not None:
            stage5_meta["option_revisions"] = option_revisions
            stage5_fresh_doc["input_meta"] = stage5_meta

        # A Stage-5 correction changes the option-set content consumed by the selected
        # mechanism. Persist it into the Stage-4 artifact before recording selection so
        # the next cycle can hydrate one exact, internally consistent chain.
        if option_revisions:
            stage4_doc["items"] = solution_options
            stage4_doc["item_count"] = len(solution_options)
            stage4_meta_raw = stage4_doc.get("input_meta")
            stage4_meta = dict(stage4_meta_raw) if isinstance(stage4_meta_raw, dict) else {}
            stage4_meta["stage5_option_revision_count"] = len(option_revisions)
            stage4_doc["input_meta"] = stage4_meta
            stage4_doc, solution_options = _persist_downstream_case_lineage(
                stage_doc=stage4_doc,
                out_json=solution_options_json,
                problem_cases=problem_records,
            )
            solution_options_md.write_text(
                _render_solution_options_markdown(
                    solution_options,
                    problem_records_by_id=problem_records_by_id,
                    family_order=family_order,
                    family_labels_by_id=family_labels_by_id,
                    optioning_outcomes_by_id={
                        str(item["problem_id"]): item
                        for item in optioning_outcomes
                        if isinstance(item.get("problem_id"), str)
                    },
                    title=(
                        f"{solution_options_json.stem.removesuffix('.solution_options')} "
                        "- Solution Options"
                    ),
                ),
                encoding="utf-8",
            )
            case_registry = _persist_case_registry_stage_lineage(
                case_registry=case_registry,
                case_registry_path=case_registry_json,
                stage_doc=stage4_doc,
            )

        stage5_doc = _merge_reused_downstream_stage_document(
            stage="solution_selection",
            stage_doc=stage5_fresh_doc,
            reused_items=reused_selection_decisions,
            agent=agent,
            dry_run=dry_run,
            artifacts={
                "solution_selection_json": str(solution_selection_json),
                "solution_selection_md": str(solution_selection_md),
            },
            count_updates={
                "problem_record_count": len(problem_records),
                "research_dossier_count": len(research_dossiers),
                "option_count": len(solution_options),
                "fresh_problem_record_count": len(fresh_problem_records),
                "reused_case_count": len(reused_chains),
                "solution_selection_status": "ok",
            },
        )
        _require_stage_model_invocation_provenance(stage5_doc)
        stage5_doc, selection_decisions = _persist_downstream_case_lineage(
            stage_doc=stage5_doc,
            out_json=solution_selection_json,
            problem_cases=problem_records,
        )
        solution_selection_md.parent.mkdir(parents=True, exist_ok=True)
        solution_selection_md.write_text(
            _render_solution_selection_markdown(
                selection_decisions,
                problem_records_by_id=problem_records_by_id,
                family_labels_by_id=family_labels_by_id,
                title=(
                    f"{solution_selection_json.stem.removesuffix('.solution_selection')} "
                    "- Solution Selection"
                ),
            ),
            encoding="utf-8",
        )
        case_registry = _persist_case_registry_stage_lineage(
            case_registry=case_registry,
            case_registry_path=case_registry_json,
            stage_doc=stage5_doc,
        )

        fresh_solution_options = [
            option
            for option in solution_options
            if _coerce_string(option.get("problem_id")) not in await_outcome_problem_ids
        ]
        fresh_selection_decisions = [
            decision
            for decision in selection_decisions
            if _coerce_string(decision.get("problem_id")) not in await_outcome_problem_ids
        ]
        reused_change_plans = flatten_chain_items(reused_chains, "change_plans")
        stage6_fresh_doc = _run_fresh_downstream_stage(
            fresh_problem_records=fresh_problem_records,
            reused_chains=reused_chains,
            run_stage=lambda: _run_implementation_planning_stage(
                repo_root=repo_root,
                target_repo_roots_by_problem=target_repo_roots_by_problem,
                problem_records=fresh_problem_records,
                research_dossiers=fresh_research_dossiers,
                solution_options=fresh_solution_options,
                selection_decisions=fresh_selection_decisions,
                pipeline_manifest=pipeline_manifest,
                artifacts_dir=artifacts_dir,
                out_json=change_plans_json,
                out_md=change_plans_md,
                agent=agent,
                model=model,
                cfg=cfg,
                dry_run=dry_run,
                stage_guidance_text=pipeline_manifest.load_stage_guidance(
                    "implementation_planning"
                ),
            ),
        )
        if stage6_fresh_doc is not None:
            _require_stage_model_invocation_provenance(stage6_fresh_doc)
        stage6_doc = _merge_reused_downstream_stage_document(
            stage="implementation_planning",
            stage_doc=stage6_fresh_doc,
            reused_items=reused_change_plans,
            agent=agent,
            dry_run=dry_run,
            artifacts={
                "change_plans_json": str(change_plans_json),
                "change_plans_md": str(change_plans_md),
            },
            count_updates={
                "problem_record_count": len(problem_records),
                "research_dossier_count": len(research_dossiers),
                "option_count": len(solution_options),
                "decision_count": len(selection_decisions),
                "change_plan_count": len(reused_change_plans)
                + (
                    len(stage6_fresh_doc.get("items", []))
                    if isinstance(stage6_fresh_doc, dict)
                    and isinstance(stage6_fresh_doc.get("items"), list)
                    else 0
                ),
                "fresh_problem_record_count": len(fresh_problem_records),
                "reused_case_count": len(reused_chains),
                "implementation_planning_status": "ok",
            },
        )
        _require_stage_model_invocation_provenance(stage6_doc)
        stage6_doc, change_plans = _persist_downstream_case_lineage(
            stage_doc=stage6_doc,
            out_json=change_plans_json,
            problem_cases=problem_records,
        )
        change_plans_md.parent.mkdir(parents=True, exist_ok=True)
        change_plans_md.write_text(
            _render_change_plans_markdown(
                change_plans,
                problem_records_by_id=problem_records_by_id,
                title=(f"{change_plans_json.stem.removesuffix('.change_plans')} - Change Plans"),
            ),
            encoding="utf-8",
        )
        case_registry = _persist_case_registry_stage_lineage(
            case_registry=case_registry,
            case_registry_path=case_registry_json,
            stage_doc=stage6_doc,
        )
    except BacklogProviderExternalWait as exc:
        external_wait_path = out_json.parent / f"{default_name}.backlog_external_wait.json"
        external_wait_temp = external_wait_path.with_name(
            f".{external_wait_path.name}.{uuid4().hex}.tmp"
        )
        try:
            external_wait_temp.write_text(
                json.dumps(exc.external_wait, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            external_wait_temp.replace(external_wait_path)
        finally:
            external_wait_temp.unlink(missing_ok=True)
        print(
            "[backlog] PARKED: signed-in Codex subscription usage limit; no later model "
            "stage was dispatched and API billing fallback remains disabled. "
            f"checkpoint={exc.external_wait.get('checkpoint_sha256')}",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[backlog] ERROR: six-stage backlog pipeline failed: {exc}", file=sys.stderr)
        return 2

    try:
        tickets = assemble_backlog_tickets(
            problem_records=problem_records,
            priority_decisions=priority_decisions,
            research_dossiers=research_dossiers,
            solution_option_sets=solution_options,
            selection_decisions=selection_decisions,
            change_plans=change_plans,
        )
    except ValueError as exc:
        print(f"[backlog] ERROR: ticket assembly failed: {exc}", file=sys.stderr)
        return 2

    eligible_atom_ids = {
        atom_id
        for atom in atoms
        for atom_id in [_coerce_string(atom.get("atom_id"))]
        if atom_id is not None
    }
    dropped_tickets_excluded_atoms = 0
    filtered_tickets: list[dict[str, Any]] = []
    for ticket in tickets:
        evidence_ids = _coerce_string_list(ticket.get("evidence_atom_ids"))
        filtered_ids = [atom_id for atom_id in evidence_ids if atom_id in eligible_atom_ids]
        if not filtered_ids:
            dropped_tickets_excluded_atoms += 1
            continue
        updated = dict(ticket)
        updated["evidence_atom_ids"] = filtered_ids
        filtered_tickets.append(updated)
    tickets = filtered_tickets

    summary = build_backlog_document(
        atoms_doc=atoms_doc,
        tickets=tickets,
        input_meta={
            "runs_dir": str(runs_dir),
            "implementation_runs_root": str(implementation_runs_root),
            "primary_record_count": len(records),
            "derived_record_count": derived_evidence_meta["records_ingested"],
            "target": target_slug,
            "repo_input": repo_input,
            "agent": agent,
            "model": model,
            "breadth_profile": breadth_profile,
            "dry_run": dry_run,
            "resume": resume,
            "force": force,
            "seed": seed,
            "sample_size": sample_size,
            "sample_size_semantics": sample_size_semantics,
            "exclude_atom_statuses": sorted(exclude_atom_status_set),
            "batch_breadth": pipeline_batch_breadth,
            "derived_evidence_ingestion": derived_evidence_meta,
            "pipeline_manifest_path": str(pipeline_manifest_path),
            "pipeline_manifest_version": int(getattr(pipeline_manifest, "version", 2)),
            "breadth_profile_warnings": breadth_profile_warnings,
        },
        artifacts={
            "atoms_jsonl": str(atoms_jsonl),
            "atoms_agent_last_message_artifact_jsonl": str(agent_last_message_atoms_jsonl),
            "artifacts_dir": str(artifacts_dir),
            "case_registry_json": str(case_registry_json),
            "prompts_dir": str(prompts_dir),
            "breadth_profile": breadth_profile,
            "batch_breadth": pipeline_batch_breadth,
            "atom_filter": {
                **(atoms_doc.get("atom_filter") or {}),
                "dropped_tickets_excluded_atoms": dropped_tickets_excluded_atoms,
            },
            "six_stage_pipeline": {
                "problem_records_json": str(problem_records_json),
                "problem_mining_evidence_json": str(problem_mining_evidence_json),
                "prioritized_problems_json": str(prioritized_json),
                "research_json": str(research_json),
                "solution_options_json": str(solution_options_json),
                "solution_selection_json": str(solution_selection_json),
                "change_plans_json": str(change_plans_json),
                "case_registry_json": str(case_registry_json),
            },
        },
        miners_meta={},
    )
    summary["scope"] = {
        "target": target_slug,
        "repo_input": repo_input,
    }

    if policy_cfg is not None:
        tickets_raw = summary.get("tickets")
        tickets_list = (
            [item for item in tickets_raw if isinstance(item, dict)]
            if isinstance(tickets_raw, list)
            else []
        )
        if tickets_list:
            updated_tickets, policy_meta = apply_backlog_policy(tickets_list, config=policy_cfg)
            summary["tickets"] = updated_tickets
            artifacts = summary.get("artifacts")
            artifacts_dict = artifacts if isinstance(artifacts, dict) else {}
            artifacts_dict["policy"] = {
                "config_path": str(policy_config_path) if policy_config_path is not None else None,
                "breadth_profile": breadth_profile,
                "warnings": breadth_profile_warnings,
                "meta": policy_meta,
            }
            summary["artifacts"] = artifacts_dict

    generated_at = _coerce_string(summary.get("generated_at_utc")) or datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    tickets_for_atoms_raw = summary.get("tickets")
    tickets_for_atoms = (
        [item for item in tickets_for_atoms_raw if isinstance(item, dict)]
        if isinstance(tickets_for_atoms_raw, list)
        else []
    )
    try:
        ticket_lineage_doc = _ticket_lineage_stage_document(
            tickets=tickets_for_atoms,
            problem_cases=problem_records,
            generated_at=generated_at,
            backlog_json_path=out_json,
            backlog_md_path=out_md,
        )
        case_registry = _persist_case_registry_stage_lineage(
            case_registry=case_registry,
            case_registry_path=case_registry_json,
            stage_doc=ticket_lineage_doc,
        )
    except (OSError, ValueError) as exc:
        print(
            f"[backlog] ERROR: failed to persist ticket case lineage: {exc}",
            file=sys.stderr,
        )
        return 2
    atom_status_meta = _update_atom_actions_from_backlog(
        atom_actions=atom_actions,
        atoms=atoms,
        tickets=tickets_for_atoms,
        generated_at=generated_at,
        backlog_json_path=out_json,
    )
    if not non_exporting_shadow:
        _write_atom_actions_yaml(atom_actions_path, atom_actions)

    artifacts = summary.get("artifacts")
    artifacts_dict = artifacts if isinstance(artifacts, dict) else {}
    artifacts_dict["atom_actions"] = {
        "path": str(atom_actions_path),
        "meta": atom_status_meta,
    }
    summary["artifacts"] = artifacts_dict

    scope_bits = []
    if target_slug is not None:
        scope_bits.append(f"target={target_slug}")
    if repo_input is not None:
        scope_bits.append(f"repo_input={repo_input}")
    title_suffix = f" ({', '.join(scope_bits)})" if scope_bits else ""

    export_projection: dict[str, Any] | None = None
    qualification_manifest_sha256_observed: str | None = None
    if non_exporting_shadow:
        assert policy_cfg is not None
        assert policy_config_path is not None
        if shadow:
            qualification_manifest_sha256_observed = _qualification_file_sha256(
                qualification_manifest_path
            )
            pending_path = shadow_pending_run_path(out_json)
            artifacts_dict["shadow_qualification"] = {
                "schema_version": 1,
                "qualification_corpus_manifest_path": (
                    str(qualification_manifest_path)
                    if qualification_manifest_path is not None
                    else None
                ),
                "qualification_manifest_sha256_expected": (qualification_manifest_sha256_expected),
                "qualification_manifest_sha256_observed": (qualification_manifest_sha256_observed),
                "qualification_input_bundle_path": (
                    str(qualification_input_bundle_path)
                    if qualification_input_bundle_path is not None
                    else None
                ),
                "qualification_input_bundle_sha256": (
                    qualification_input_bundle.get("content_sha256")
                    if qualification_input_bundle is not None
                    else None
                ),
                "qualification_cycle_root": (
                    str(qualification_cycle_root) if qualification_cycle_root is not None else None
                ),
                "qualification_cycle_contract_path": (
                    str(qualification_cycle_contract_path)
                    if qualification_cycle_contract_path is not None
                    else None
                ),
                "qualification_cycle_contract_sha256": (
                    qualification_cycle_contract_value.get("content_sha256")
                    if qualification_cycle_contract_value is not None
                    else None
                ),
                "stage_runs_dir": str(stage_runs_dir),
                "shadow_state_path": str(explicit_shadow_state_path or shadow_state_path(out_json)),
                "qualification_output_adjudication_path": (
                    str(qualification_output_adjudication_path)
                    if qualification_output_adjudication_path is not None
                    else None
                ),
                "qualification_output_adjudication_sha256_pre_run": (
                    qualification_output_adjudication_sha256_pre_run
                ),
                "no_actionable_evidence_receipt_path": (
                    str(no_actionable_evidence_receipt_path)
                    if no_actionable_evidence_receipt_path is not None
                    else None
                ),
                "pending_run_receipt_path": str(pending_path),
                "pending_adjudication": True,
                "model_readable_roots": [str(path) for path in owner_roots],
                "labels_supplied_to_model_stages": False,
            }
        else:
            pending_path = operational_shadow_pending_run_path(out_json)
            artifacts_dict["operational_shadow"] = {
                "schema_version": 1,
                "pending_run_receipt_path": str(pending_path),
                "pending_internal_validation": True,
                "model_readable_roots": [str(path) for path in owner_roots],
                "held_out_labels_required": False,
                "release_qualification_earned": False,
            }
        summary["artifacts"] = artifacts_dict
        export_projection = _build_export_projection(
            backlog=summary,
            surface_area_high=set(policy_cfg.surface_area_high),
            cli_repo_input=repo_input,
            repo_root=repo_root,
        )
        export_contract_raw = artifacts_dict.get("export_contract")
        export_contract = dict(export_contract_raw) if isinstance(export_contract_raw, dict) else {}
        export_contract.update(
            {
                "schema_version": 1,
                "projection_sha256": export_projection["sha256"],
                "policy_config_path": str(policy_config_path.resolve()),
                "ux_review_json_path": str(_ux_review_path_for_backlog(out_json).resolve()),
                "shadow_state_path": str(explicit_shadow_state_path or shadow_state_path(out_json)),
            }
        )
        artifacts_dict["export_contract"] = export_contract
        summary["artifacts"] = artifacts_dict

    write_backlog(
        summary,
        out_json_path=out_json,
        out_md_path=out_md,
        title=f"Usertest Backlog{title_suffix}",
    )

    shadow_state: dict[str, Any] | None = None
    if non_exporting_shadow:
        assert export_projection is not None
        assert policy_config_path is not None
        export_artifact_paths = _export_artifact_paths(
            backlog=summary,
            backlog_path=out_json,
            repo_root=repo_root,
            policy_config_path=policy_config_path,
            export_gate_config_path=repo_root / "configs" / "backlog_export_gate.yaml",
            cli_repo_input=repo_input,
        )
        if shadow:
            pending_path = shadow_pending_run_path(out_json)
            pending = write_pending_shadow_run(
                pending_path=pending_path,
                backlog_path=out_json,
                artifact_paths=export_artifact_paths,
                qualification_manifest_sha256_expected=(qualification_manifest_sha256_expected),
                output_adjudication_sha256_pre_run=(
                    qualification_output_adjudication_sha256_pre_run
                ),
                generated_at=generated_at,
            )
        else:
            pending_path = operational_shadow_pending_run_path(out_json)
            pending = write_pending_operational_shadow_run(
                pending_path=pending_path,
                backlog_path=out_json,
                artifact_paths=export_artifact_paths,
                generated_at=generated_at,
            )
        print(str(pending_path))
        print(
            json.dumps(
                {
                    "shadow_materialized": True,
                    "shadow_mode": "release" if shadow else "operational",
                    "pending_independent_adjudication": shadow,
                    "pending_internal_validation": operational_shadow,
                    "pending_run_sha256": pending["content_sha256"],
                    "score_command": (
                        "usertest-backlog reports backlog --shadow --score-shadow"
                        if shadow
                        else (
                            "usertest-backlog reports backlog --operational-shadow "
                            "--score-operational-shadow"
                        )
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    print(str(out_json))
    print(str(out_md))
    print(str(atoms_jsonl))
    print(str(agent_last_message_atoms_jsonl))
    print(json.dumps(summary.get("totals", {}), indent=2, ensure_ascii=False))
    print(json.dumps(summary.get("coverage", {}), indent=2, ensure_ascii=False))

    if shadow_state is not None and not shadow_state["cycles"][-1]["passed"]:
        return 3
    return 0


__all__ = [name for name in globals() if not name.startswith("__")]
