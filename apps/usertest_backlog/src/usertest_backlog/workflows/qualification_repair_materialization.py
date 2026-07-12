"""Materialize repaired qualification output without mutating the scored source run."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from backlog_core import BacklogPolicyConfig, apply_backlog_policy
from backlog_core.backlog import build_backlog_document, write_backlog

from usertest_backlog.commands.export_tickets import (
    _build_export_projection,
    _export_artifact_paths,
)
from usertest_backlog.workflows.qualification_healing import (
    build_pending_repaired_shadow_run,
    pending_repaired_shadow_run_errors,
    qualification_correction_consumption_errors,
)
from usertest_backlog.workflows.qualification_repair_runtime import (
    QualificationRepairRuntimeResult,
)
from usertest_backlog.workflows.shadow_validation import (
    shadow_pending_run_path,
    write_pending_shadow_run,
)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _canonical_hash(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _qualification_error_count(report: Mapping[str, Any]) -> int | None:
    qualification_raw = report.get("qualification")
    qualification = (
        qualification_raw if isinstance(qualification_raw, Mapping) else {}
    )
    counts_raw = qualification.get("counts")
    counts = counts_raw if isinstance(counts_raw, Mapping) else {}
    fields = (
        "accepted_bad",
        "accepted_unknown",
        "false_rejected_good",
        "undispositioned_actionable_cases",
    )
    values: list[int] = []
    for field in fields:
        value = counts.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        values.append(value)
    return sum(values)


def best_qualified_fallback_errors(
    value: Any,
    *,
    verify_files: bool = True,
) -> list[str]:
    """Validate the immutable best independently-qualified ancestor binding."""

    if not isinstance(value, Mapping):
        return ["best_qualified_fallback_invalid"]
    errors: list[str] = []
    if value.get("schema_version") != 1:
        errors.append("best_qualified_fallback_schema_invalid")
    if value.get("contract_kind") != "best_qualified_fallback":
        errors.append("best_qualified_fallback_kind_invalid")
    projected = {key: item for key, item in value.items() if key != "content_sha256"}
    if _text(value.get("content_sha256")) != _canonical_hash(projected):
        errors.append("best_qualified_fallback_content_hash_invalid")
    error_count = value.get("qualification_error_count")
    if isinstance(error_count, bool) or not isinstance(error_count, int) or error_count < 0:
        errors.append("best_qualified_fallback_error_count_invalid")
    for field in (
        "backlog_path",
        "backlog_sha256",
        "report_path",
        "report_sha256",
        "phase1_bundle_path",
        "phase1_bundle_sha256",
        "pending_run_sha256",
        "qualification_basis_sha256",
        "qualification_stability_sha256",
        "output_adjudication_path",
        "output_adjudication_sha256",
    ):
        if _text(value.get(field)) is None:
            errors.append(f"best_qualified_fallback_field_missing:{field}")
    if errors or not verify_files:
        return list(dict.fromkeys(errors))

    backlog_path = Path(str(value["backlog_path"])).resolve()
    report_path = Path(str(value["report_path"])).resolve()
    phase1_bundle_path = Path(str(value["phase1_bundle_path"])).resolve()
    adjudication_path = Path(str(value["output_adjudication_path"])).resolve()
    try:
        if not backlog_path.is_file() or _file_sha256(backlog_path) != value.get(
            "backlog_sha256"
        ):
            errors.append("best_qualified_fallback_backlog_changed")
        if not report_path.is_file() or _file_sha256(report_path) != value.get(
            "report_sha256"
        ):
            errors.append("best_qualified_fallback_report_changed")
        if not phase1_bundle_path.is_file():
            errors.append("best_qualified_fallback_phase1_bundle_changed")
        if not adjudication_path.is_file() or _file_sha256(adjudication_path) != value.get(
            "output_adjudication_sha256"
        ):
            errors.append("best_qualified_fallback_adjudication_changed")
        report_wrapper = _read_json_object(report_path)
        phase1_bundle = _read_json_object(phase1_bundle_path)
        adjudication = _read_json_object(adjudication_path)
    except (OSError, ValueError):
        errors.append("best_qualified_fallback_artifact_unreadable")
        return list(dict.fromkeys(errors))
    wrapper_projection = {
        key: item for key, item in report_wrapper.items() if key != "content_sha256"
    }
    if report_wrapper.get("content_sha256") != _canonical_hash(wrapper_projection):
        errors.append("best_qualified_fallback_report_content_hash_invalid")
    bundle_projection = {
        key: item for key, item in phase1_bundle.items() if key != "content_sha256"
    }
    bundle_backlog_raw = phase1_bundle.get("backlog")
    bundle_backlog = (
        bundle_backlog_raw if isinstance(bundle_backlog_raw, Mapping) else {}
    )
    bundle_adjudication_raw = phase1_bundle.get(
        "qualification_output_adjudication"
    )
    bundle_adjudication = (
        bundle_adjudication_raw
        if isinstance(bundle_adjudication_raw, Mapping)
        else {}
    )
    if (
        phase1_bundle.get("content_sha256") != _canonical_hash(bundle_projection)
        or phase1_bundle.get("content_sha256") != value.get("phase1_bundle_sha256")
        or phase1_bundle.get("source_pending_run_sha256")
        != value.get("pending_run_sha256")
        or bundle_backlog.get("snapshot_path") != str(backlog_path)
        or bundle_backlog.get("sha256") != value.get("backlog_sha256")
        or bundle_adjudication.get("snapshot_path") != str(adjudication_path)
        or bundle_adjudication.get("sha256")
        != value.get("output_adjudication_sha256")
        or report_wrapper.get("pending_run_sha256")
        != value.get("pending_run_sha256")
        or adjudication.get("pending_run_sha256") != value.get("pending_run_sha256")
    ):
        errors.append("best_qualified_fallback_transaction_binding_invalid")
    report_raw = report_wrapper.get("report")
    report = report_raw if isinstance(report_raw, Mapping) else {}
    qualification_raw = report.get("qualification")
    qualification = (
        qualification_raw if isinstance(qualification_raw, Mapping) else {}
    )
    if (
        report.get("passed") is not True
        or qualification.get("status") != "verified"
        or qualification.get("useful_output_verified") is not True
        or report.get("qualification_basis_sha256")
        != value.get("qualification_basis_sha256")
        or qualification.get("stability_sha256")
        != value.get("qualification_stability_sha256")
        or _qualification_error_count(report) != value.get("qualification_error_count")
    ):
        errors.append("best_qualified_fallback_report_not_qualified")
    return list(dict.fromkeys(errors))


def select_best_qualified_fallback(
    *,
    prior: Mapping[str, Any] | None,
    candidate_backlog_path: Path | None,
    candidate_report_path: Path | None,
    candidate_output_adjudication_path: Path | None,
    candidate_phase1_bundle_path: Path | None,
) -> dict[str, Any] | None:
    """Retain the qualified candidate only when its measured error count improves.

    Nondeterministic correction may exchange one error for another.  Fewer errors is
    real progress; equal error count is not enough to discard the already-bound best
    ancestor.
    """

    retained: dict[str, Any] | None = None
    if prior is not None:
        prior_errors = best_qualified_fallback_errors(prior, verify_files=True)
        if prior_errors:
            raise ValueError(
                "best_qualified_fallback_prior_invalid:" + ",".join(prior_errors)
            )
        retained = dict(prior)
    if (
        candidate_backlog_path is None
        or candidate_report_path is None
        or candidate_output_adjudication_path is None
        or candidate_phase1_bundle_path is None
    ):
        return retained
    try:
        wrapper = _read_json_object(candidate_report_path.resolve())
        phase1_bundle = _read_json_object(candidate_phase1_bundle_path.resolve())
        adjudication = _read_json_object(candidate_output_adjudication_path.resolve())
    except ValueError:
        return retained
    wrapper_projection = {
        key: item for key, item in wrapper.items() if key != "content_sha256"
    }
    report_raw = wrapper.get("report")
    report = report_raw if isinstance(report_raw, Mapping) else {}
    qualification_raw = report.get("qualification")
    qualification = (
        qualification_raw if isinstance(qualification_raw, Mapping) else {}
    )
    error_count = _qualification_error_count(report)
    phase1_projection = {
        key: item for key, item in phase1_bundle.items() if key != "content_sha256"
    }
    bundle_backlog_raw = phase1_bundle.get("backlog")
    bundle_backlog = (
        bundle_backlog_raw if isinstance(bundle_backlog_raw, Mapping) else {}
    )
    bundle_adjudication_raw = phase1_bundle.get(
        "qualification_output_adjudication"
    )
    bundle_adjudication = (
        bundle_adjudication_raw
        if isinstance(bundle_adjudication_raw, Mapping)
        else {}
    )
    pending_run_sha256 = _text(phase1_bundle.get("source_pending_run_sha256"))
    if (
        wrapper.get("content_sha256") != _canonical_hash(wrapper_projection)
        or report.get("passed") is not True
        or qualification.get("status") != "verified"
        or qualification.get("useful_output_verified") is not True
        or error_count is None
        or not candidate_backlog_path.is_file()
        or not candidate_output_adjudication_path.is_file()
        or phase1_bundle.get("content_sha256") != _canonical_hash(phase1_projection)
        or pending_run_sha256 is None
        or wrapper.get("pending_run_sha256") != pending_run_sha256
        or adjudication.get("pending_run_sha256") != pending_run_sha256
        or bundle_backlog.get("snapshot_path")
        != str(candidate_backlog_path.resolve())
        or bundle_backlog.get("sha256") != _file_sha256(candidate_backlog_path)
        or bundle_adjudication.get("snapshot_path")
        != str(candidate_output_adjudication_path.resolve())
        or bundle_adjudication.get("sha256")
        != _file_sha256(candidate_output_adjudication_path)
    ):
        return retained
    if retained is not None and error_count >= int(retained["qualification_error_count"]):
        return retained
    candidate_body = {
        "schema_version": 1,
        "contract_kind": "best_qualified_fallback",
        "backlog_path": str(candidate_backlog_path.resolve()),
        "backlog_sha256": _file_sha256(candidate_backlog_path.resolve()),
        "report_path": str(candidate_report_path.resolve()),
        "report_sha256": _file_sha256(candidate_report_path.resolve()),
        "phase1_bundle_path": str(candidate_phase1_bundle_path.resolve()),
        "phase1_bundle_sha256": phase1_bundle.get("content_sha256"),
        "pending_run_sha256": pending_run_sha256,
        "qualification_basis_sha256": report.get("qualification_basis_sha256"),
        "qualification_stability_sha256": qualification.get("stability_sha256"),
        "output_adjudication_path": str(
            candidate_output_adjudication_path.resolve()
        ),
        "output_adjudication_sha256": _file_sha256(
            candidate_output_adjudication_path.resolve()
        ),
        "qualification_error_count": error_count,
        "selection_reason": (
            "first_independently_qualified_candidate"
            if retained is None
            else "strictly_fewer_independently_measured_errors"
        ),
        "superseded_fallback_content_sha256": (
            retained.get("content_sha256") if retained is not None else None
        ),
    }
    candidate = {
        **candidate_body,
        "content_sha256": _canonical_hash(candidate_body),
    }
    candidate_errors = best_qualified_fallback_errors(candidate, verify_files=True)
    if candidate_errors:
        raise ValueError(
            "best_qualified_fallback_candidate_invalid:" + ",".join(candidate_errors)
        )
    return candidate


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"qualification_repair_existing_artifact_unreadable:{path.name}:{type(exc).__name__}"
        ) from exc
    if not isinstance(value, dict):
        raise ValueError(f"qualification_repair_existing_artifact_invalid:{path.name}")
    return value


def _validate_bound_receipts(receipts: Any) -> None:
    if not isinstance(receipts, list):
        raise ValueError("qualification_repair_existing_pending_receipts_invalid")
    for receipt in receipts:
        if not isinstance(receipt, dict):
            raise ValueError("qualification_repair_existing_pending_receipt_invalid")
        if receipt.get("exists") is not True:
            continue
        source_path = _text(receipt.get("source_path"))
        expected_sha256 = _text(receipt.get("sha256"))
        if source_path is None or expected_sha256 is None:
            raise ValueError("qualification_repair_existing_pending_receipt_unbound")
        path = Path(source_path)
        if not path.is_file() or _file_sha256(path) != expected_sha256:
            raise ValueError(
                "qualification_repair_existing_pending_artifact_changed:"
                f"{receipt.get('name', path.name)}"
            )


def _resolved_artifact_path(value: Any, *, repo_root: Path) -> Path | None:
    raw = _text(value)
    if raw is None:
        return None
    path = Path(raw)
    return (path if path.is_absolute() else repo_root / path).resolve()


def _write_temp_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if path.read_bytes() != content:
        raise ValueError(f"qualification_repair_bundle_write_verification_failed:{path.name}")


def _copy_bundle_artifact(
    *,
    name: str,
    source_path: Path,
    temp_path: Path,
    final_path: Path,
) -> dict[str, Any]:
    if not source_path.is_file():
        raise ValueError(f"qualification_repair_bundle_source_missing:{name}")
    source_content = source_path.read_bytes()
    source_sha256 = sha256(source_content).hexdigest()
    _write_temp_bytes(temp_path, source_content)
    bundled_sha256 = _file_sha256(temp_path)
    if bundled_sha256 != source_sha256:
        raise ValueError(f"qualification_repair_bundle_copy_mismatch:{name}")
    return {
        "name": name,
        "source_path": str(source_path.resolve()),
        "source_sha256": source_sha256,
        "bundled_path": str(final_path.resolve()),
        "bundled_sha256": bundled_sha256,
        "size_bytes": len(source_content),
    }


def _bundle_temp_path(
    path: Path | None,
    *,
    final_root: Path,
    temp_root: Path,
) -> Path | None:
    if path is None:
        return None
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(final_root.resolve())
    except ValueError:
        return resolved
    return (temp_root / relative).resolve()


def _publish_pending_paths(
    pending: dict[str, Any],
    *,
    temp_root: Path,
    final_root: Path,
    final_backlog_path: Path,
) -> dict[str, Any]:
    published = dict(pending)
    published["backlog_path"] = str(final_backlog_path.resolve())
    receipts: list[dict[str, Any]] = []
    for raw_receipt in pending.get("artifact_receipts", []):
        receipt = dict(raw_receipt)
        raw_path = _text(receipt.get("source_path"))
        if raw_path is not None:
            path = Path(raw_path).resolve()
            try:
                relative = path.relative_to(temp_root.resolve())
            except ValueError:
                pass
            else:
                receipt["source_path"] = str((final_root / relative).resolve())
        receipts.append(receipt)
    published["artifact_receipts"] = receipts
    published.pop("content_sha256", None)
    published["content_sha256"] = _canonical_hash(published)
    return published


def _quarantine_incomplete_bundle(path: Path, *, consumption_sha256: str) -> Path | None:
    if not path.exists():
        return None
    quarantine = path.parent / (
        f".{consumption_sha256}.failed-{uuid4().hex}"
    )
    path.rename(quarantine)
    return quarantine


def _existing_materialization_result(
    *,
    repair_root: Path,
    expected_consumption: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a complete prior materialization or reject a partial/conflicting one.

    The consumption content hash is the repair identity.  Once any artifact exists
    under that identity, no artifact at that path may be replaced.
    """

    repaired_backlog_path = repair_root / "repaired.backlog.json"
    repaired_backlog_md_path = repair_root / "repaired.backlog.md"
    repaired_pending_path = shadow_pending_run_path(repaired_backlog_path)
    repaired_contract_path = repair_root / "pending_repaired_shadow_run.json"
    consumption_path = repair_root / "qualification_correction_consumption.json"
    required_paths = (
        consumption_path,
        repaired_backlog_path,
        repaired_backlog_md_path,
        repaired_pending_path,
        repaired_contract_path,
    )
    existing = [path.is_file() for path in required_paths]
    if not any(existing):
        if repair_root.exists():
            raise ValueError("qualification_repair_identity_partial_or_conflicting")
        return None
    if not all(existing):
        raise ValueError("qualification_repair_identity_partial_or_conflicting")

    consumption = _read_json_object(consumption_path)
    if consumption != expected_consumption:
        raise ValueError("qualification_repair_identity_consumption_conflict")
    consumption_errors = qualification_correction_consumption_errors(consumption)
    if consumption_errors:
        raise ValueError(
            "qualification_repair_existing_consumption_invalid:"
            + ",".join(consumption_errors)
        )

    pending = _read_json_object(repaired_pending_path)
    pending_hash = _text(pending.get("content_sha256"))
    pending_projection = {
        key: value for key, value in pending.items() if key != "content_sha256"
    }
    if pending_hash is None or pending_hash != _canonical_hash(pending_projection):
        raise ValueError("qualification_repair_existing_pending_hash_invalid")
    if pending.get("backlog_path") != str(repaired_backlog_path.resolve()):
        raise ValueError("qualification_repair_existing_pending_backlog_mismatch")
    if pending.get("backlog_sha256") != _file_sha256(repaired_backlog_path):
        raise ValueError("qualification_repair_existing_backlog_changed")
    _validate_bound_receipts(pending.get("artifact_receipts"))

    repaired_contract = _read_json_object(repaired_contract_path)
    contract_errors = pending_repaired_shadow_run_errors(repaired_contract)
    if contract_errors:
        raise ValueError(
            "qualification_repair_existing_contract_invalid:"
            + ",".join(contract_errors)
        )
    if repaired_contract.get("correction_consumption_sha256") != consumption.get(
        "content_sha256"
    ):
        raise ValueError("qualification_repair_existing_contract_consumption_mismatch")
    if repaired_contract.get("repaired_backlog_sha256") != _file_sha256(
        repaired_backlog_path
    ):
        raise ValueError("qualification_repair_existing_contract_backlog_mismatch")
    if repaired_contract.get("repaired_pending_run_sha256") != pending_hash:
        raise ValueError("qualification_repair_existing_contract_pending_mismatch")
    _validate_bound_receipts(repaired_contract.get("repaired_artifact_receipts"))

    return {
        "repaired_backlog_path": str(repaired_backlog_path.resolve()),
        "repaired_backlog_markdown_path": str(repaired_backlog_md_path.resolve()),
        "correction_consumption_path": str(consumption_path.resolve()),
        "pending_shadow_run_path": str(repaired_pending_path.resolve()),
        "pending_shadow_run_sha256": pending_hash,
        "pending_repaired_shadow_run_path": str(repaired_contract_path.resolve()),
        "pending_repaired_shadow_run_sha256": repaired_contract["content_sha256"],
        "fresh_independent_readjudication_required": True,
        "release_qualification_eligible": False,
    }


def _stage_paths(runtime: QualificationRepairRuntimeResult) -> dict[str, Path]:
    downstream = runtime.consumption.get("downstream_result")
    receipts_raw = (
        downstream.get("materialized_stage_receipts")
        if isinstance(downstream, dict)
        else None
    )
    receipts = (
        [item for item in receipts_raw if isinstance(item, dict)]
        if isinstance(receipts_raw, list)
        else []
    )
    paths: dict[str, Path] = {}
    for receipt in receipts:
        stage = _text(receipt.get("stage"))
        path_raw = _text(receipt.get("path"))
        if stage is None or path_raw is None:
            continue
        path = Path(path_raw).resolve()
        if not path.is_file() or receipt.get("sha256") != _file_sha256(path):
            raise ValueError(f"qualification_repair_stage_receipt_invalid:{stage}")
        paths[stage] = path
    expected = {
        "problem_mining",
        "problem_prioritization",
        "repro_research",
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
    }
    if not expected.issubset(paths):
        raise ValueError(
            "qualification_repair_stage_receipts_incomplete:"
            + ",".join(sorted(expected - set(paths)))
        )
    return paths


def materialize_repaired_shadow_run(
    *,
    source_backlog: dict[str, Any],
    source_backlog_path: Path,
    atoms: list[dict[str, Any]],
    runtime: QualificationRepairRuntimeResult,
    repo_root: Path,
    repo_input: str | None,
    policy_config: BacklogPolicyConfig,
    policy_config_path: Path,
    export_gate_config_path: Path,
    qualification_manifest_path: Path,
    qualification_manifest_sha256: str,
    qualification_output_adjudication_path: Path | None,
    qualification_output_adjudication_sha256: str,
) -> dict[str, Any] | None:
    """Write an isolated repaired backlog and both pending-run contracts.

    No source artifact is overwritten.  That preserves the already-recorded score and
    its provenance while making the repaired corpus independently re-adjudicable.
    """

    if runtime.consumption.get("accepted_repair_count") in {None, 0}:
        return None
    consumption_errors = qualification_correction_consumption_errors(runtime.consumption)
    if consumption_errors:
        raise ValueError(
            "qualification_repair_consumption_invalid:" + ",".join(consumption_errors)
        )
    consumption_sha256 = str(runtime.consumption["content_sha256"])
    repair_parent = (
        source_backlog_path.parent
        / f"{source_backlog_path.stem}.qualification_repair"
    )
    repair_root = repair_parent / consumption_sha256
    existing_result = _existing_materialization_result(
        repair_root=repair_root,
        expected_consumption=runtime.consumption,
    )
    if existing_result is not None:
        return existing_result

    stage_sources = _stage_paths(runtime)
    try:
        scored_source_raw = json.loads(
            source_backlog_path.resolve().read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("qualification_repair_scored_source_unreadable") from exc
    scored_source = scored_source_raw if isinstance(scored_source_raw, dict) else {}
    scored_artifacts_raw = scored_source.get("artifacts")
    scored_artifacts = (
        scored_artifacts_raw if isinstance(scored_artifacts_raw, Mapping) else {}
    )
    scored_qualification_raw = scored_artifacts.get("shadow_qualification")
    scored_qualification = (
        scored_qualification_raw
        if isinstance(scored_qualification_raw, Mapping)
        else {}
    )
    source_artifacts_raw = source_backlog.get("artifacts")
    source_artifacts = (
        dict(source_artifacts_raw) if isinstance(source_artifacts_raw, dict) else {}
    )
    source_pipeline_raw = source_artifacts.get("six_stage_pipeline")
    source_pipeline = (
        dict(source_pipeline_raw) if isinstance(source_pipeline_raw, dict) else {}
    )
    for stage, pipeline_key in (
        ("problem_mining_evidence", "problem_mining_evidence_json"),
        ("case_registry", "case_registry_json"),
    ):
        if stage in stage_sources:
            continue
        source = _resolved_artifact_path(
            source_pipeline.get(pipeline_key)
            or (source_artifacts.get("case_registry_json") if stage == "case_registry" else None),
            repo_root=repo_root,
        )
        if source is not None:
            stage_sources[stage] = source

    input_raw = source_backlog.get("input")
    source_input = dict(input_raw) if isinstance(input_raw, dict) else {}
    source_totals = source_backlog.get("totals")
    totals = source_totals if isinstance(source_totals, dict) else {}
    repaired_atoms_raw = getattr(runtime, "atoms", None)
    repaired_atoms = (
        [dict(item) for item in repaired_atoms_raw if isinstance(item, dict)]
        if isinstance(repaired_atoms_raw, list)
        else [dict(item) for item in atoms]
    )
    atoms_doc = {
        "atoms": repaired_atoms,
        "totals": {
            "source_counts": dict(totals.get("source_counts") or {}),
            "severity_hint_counts": dict(totals.get("severity_hint_counts") or {}),
        },
    }

    repair_parent.mkdir(parents=True, exist_ok=True)
    temp_root = repair_parent / f".{consumption_sha256}.tmp-{uuid4().hex}"
    temp_root.mkdir(exist_ok=False)
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    repaired_backlog_path = repair_root / "repaired.backlog.json"
    repaired_backlog_md_path = repair_root / "repaired.backlog.md"
    repaired_pending_path = shadow_pending_run_path(repaired_backlog_path)
    repaired_contract_path = repair_root / "pending_repaired_shadow_run.json"
    consumption_path = repair_root / "qualification_correction_consumption.json"
    bundle_manifest_path = repair_root / "qualification_repair_bundle_manifest.json"
    repaired_atoms_path = repair_root / "artifacts" / "atoms.json"
    fresh_adjudication_path = (
        repair_root / "qualification.output_adjudication.json"
    )

    temp_backlog_path = temp_root / repaired_backlog_path.relative_to(repair_root)
    temp_backlog_md_path = temp_root / repaired_backlog_md_path.relative_to(repair_root)
    temp_pending_path = temp_root / repaired_pending_path.relative_to(repair_root)
    temp_contract_path = temp_root / repaired_contract_path.relative_to(repair_root)
    temp_consumption_path = temp_root / consumption_path.relative_to(repair_root)
    temp_bundle_manifest_path = temp_root / bundle_manifest_path.relative_to(repair_root)
    temp_atoms_path = temp_root / repaired_atoms_path.relative_to(repair_root)

    published_stage_paths: dict[str, Path] = {}
    bundle_receipts: list[dict[str, Any]] = []
    try:
        for stage, source_path in sorted(stage_sources.items()):
            final_path = repair_root / "artifacts" / "stages" / f"{stage}.json"
            temp_path = temp_root / final_path.relative_to(repair_root)
            bundle_receipts.append(
                _copy_bundle_artifact(
                    name=f"stage:{stage}",
                    source_path=source_path,
                    temp_path=temp_path,
                    final_path=final_path,
                )
            )
            published_stage_paths[stage] = final_path

        required_bundle_stages = {
            "problem_mining",
            "problem_prioritization",
            "repro_research",
            "solution_optioning",
            "solution_selection",
            "implementation_planning",
            "problem_mining_evidence",
            "case_registry",
        }
        missing_bundle_stages = required_bundle_stages - set(published_stage_paths)
        if missing_bundle_stages:
            raise ValueError(
                "qualification_repair_bundle_stage_missing:"
                + ",".join(sorted(missing_bundle_stages))
            )

        atoms_content = (
            json.dumps(repaired_atoms, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        _write_temp_bytes(temp_atoms_path, atoms_content)
        atoms_sha256 = sha256(atoms_content).hexdigest()
        bundle_receipts.append(
            {
                "name": "atoms",
                "source_path": None,
                "source_sha256": _canonical_hash(repaired_atoms),
                "bundled_path": str(repaired_atoms_path.resolve()),
                "bundled_sha256": atoms_sha256,
                "size_bytes": len(atoms_content),
            }
        )

        bundled_manifest_path = repair_root / "provenance" / "qualification_manifest.json"
        temp_manifest_path = temp_root / bundled_manifest_path.relative_to(repair_root)
        manifest_receipt = _copy_bundle_artifact(
            name="qualification_manifest",
            source_path=qualification_manifest_path.resolve(),
            temp_path=temp_manifest_path,
            final_path=bundled_manifest_path,
        )
        if manifest_receipt["bundled_sha256"] != qualification_manifest_sha256:
            raise ValueError("qualification_repair_manifest_binding_mismatch")
        bundle_receipts.append(manifest_receipt)

        bundled_source_adjudication_path: Path | None = None
        if qualification_output_adjudication_path is not None:
            bundled_source_adjudication_path = (
                repair_root / "provenance" / "source_output_adjudication.json"
            )
            temp_source_adjudication_path = (
                temp_root / bundled_source_adjudication_path.relative_to(repair_root)
            )
            adjudication_receipt = _copy_bundle_artifact(
                name="source_output_adjudication",
                source_path=qualification_output_adjudication_path.resolve(),
                temp_path=temp_source_adjudication_path,
                final_path=bundled_source_adjudication_path,
            )
            if (
                adjudication_receipt["bundled_sha256"]
                != qualification_output_adjudication_sha256
            ):
                raise ValueError("qualification_repair_adjudication_binding_mismatch")
            bundle_receipts.append(adjudication_receipt)

        repaired_pipeline = {
            **source_pipeline,
            "problem_records_json": str(published_stage_paths["problem_mining"]),
            "problem_mining_evidence_json": str(
                published_stage_paths["problem_mining_evidence"]
            ),
            "prioritized_problems_json": str(
                published_stage_paths["problem_prioritization"]
            ),
            "research_json": str(published_stage_paths["repro_research"]),
            "solution_options_json": str(
                published_stage_paths["solution_optioning"]
            ),
            "solution_selection_json": str(
                published_stage_paths["solution_selection"]
            ),
            "change_plans_json": str(
                published_stage_paths["implementation_planning"]
            ),
            "case_registry_json": str(published_stage_paths["case_registry"]),
        }
        existing_shadow_raw = source_artifacts.get("shadow_qualification")
        existing_shadow = (
            dict(existing_shadow_raw) if isinstance(existing_shadow_raw, dict) else {}
        )
        route_receipts_raw = runtime.consumption.get("route_receipts")
        route_receipts = (
            [item for item in route_receipts_raw if isinstance(item, Mapping)]
            if isinstance(route_receipts_raw, list)
            else []
        )
        round_correction_metrics = {
            "correction_route_count": len(route_receipts),
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
            "accepted_repair_count": int(
                runtime.consumption.get("accepted_repair_count") or 0
            ),
            "accepted_repair_group_count": int(
                runtime.consumption.get("accepted_repair_group_count") or 0
            ),
            "unresolved_route_count": int(
                runtime.consumption.get("unresolved_route_count") or 0
            ),
            "pending_not_invoked_route_count": int(
                runtime.consumption.get("pending_not_invoked_route_count") or 0
            ),
        }
        prior_history_raw = scored_qualification.get("correction_history")
        prior_history = (
            [dict(item) for item in prior_history_raw if isinstance(item, Mapping)]
            if isinstance(prior_history_raw, list)
            else []
        )
        round_history_body = {
            "round": len(prior_history) + 1,
            "source_scored_backlog_path": str(source_backlog_path.resolve()),
            "source_scored_backlog_sha256": _file_sha256(source_backlog_path),
            "source_adjudication_path": (
                str(qualification_output_adjudication_path.resolve())
                if qualification_output_adjudication_path is not None
                else None
            ),
            "source_adjudication_sha256": qualification_output_adjudication_sha256,
            "failed_report_path": scored_qualification.get(
                "latest_failed_adjudication_report_path"
            )
            or scored_qualification.get("raw_first_pass_report_path"),
            "failed_report_sha256": scored_qualification.get(
                "latest_failed_adjudication_report_sha256"
            )
            or scored_qualification.get("raw_first_pass_report_sha256"),
            "correction_consumption_sha256": consumption_sha256,
            "correction_consumption_path": str(consumption_path.resolve()),
            "metrics": round_correction_metrics,
        }
        round_history = {
            **round_history_body,
            "content_sha256": _canonical_hash(round_history_body),
        }
        correction_history = [*prior_history, round_history]
        metric_names = sorted(
            {
                key
                for item in correction_history
                for metrics in [item.get("metrics")]
                if isinstance(metrics, Mapping)
                for key in metrics
                if isinstance(key, str)
            }
        )
        correction_metrics = {
            key: sum(
                int(metrics.get(key) or 0)
                for item in correction_history
                for metrics in [item.get("metrics")]
                if isinstance(metrics, Mapping)
            )
            for key in metric_names
        }
        original_report_path = scored_qualification.get(
            "original_first_pass_report_path"
        ) or scored_qualification.get("raw_first_pass_report_path")
        original_report_sha256 = scored_qualification.get(
            "original_first_pass_report_sha256"
        ) or scored_qualification.get("raw_first_pass_report_sha256")
        best_qualified_fallback = select_best_qualified_fallback(
            prior=(
                scored_qualification.get("best_qualified_fallback")
                if isinstance(
                    scored_qualification.get("best_qualified_fallback"), Mapping
                )
                else None
            ),
            candidate_backlog_path=None,
            candidate_report_path=None,
            candidate_output_adjudication_path=None,
            candidate_phase1_bundle_path=None,
        )
        repaired_artifacts = {
            **source_artifacts,
            "atoms_jsonl": str(repaired_atoms_path.resolve()),
            "case_registry_json": str(published_stage_paths["case_registry"]),
            "six_stage_pipeline": repaired_pipeline,
            "qualification_repair_bundle_manifest": str(bundle_manifest_path.resolve()),
            "shadow_qualification": {
                **existing_shadow,
                "schema_version": 1,
                "qualification_corpus_manifest_path": str(
                    bundled_manifest_path.resolve()
                ),
                "qualification_manifest_sha256_expected": qualification_manifest_sha256,
                "qualification_manifest_sha256_observed": qualification_manifest_sha256,
                "qualification_output_adjudication_path": str(
                    fresh_adjudication_path.resolve()
                ),
                "qualification_output_adjudication_sha256_pre_run": None,
                "source_qualification_output_adjudication_path": (
                    str(bundled_source_adjudication_path.resolve())
                    if bundled_source_adjudication_path is not None
                    else None
                ),
                "source_qualification_output_adjudication_sha256": (
                    qualification_output_adjudication_sha256
                ),
                "pending_run_receipt_path": str(repaired_pending_path.resolve()),
                "pending_repaired_run_receipt_path": str(repaired_contract_path.resolve()),
                "qualification_correction_consumption_path": str(
                    consumption_path.resolve()
                ),
                "qualification_correction_consumption_sha256": consumption_sha256,
                "pending_adjudication": True,
                "same_corpus_feedback_exposed": True,
                "release_qualification_eligible": False,
                "fresh_independent_readjudication_required": True,
                "correction_metrics": correction_metrics,
                "correction_history": correction_history,
                "best_qualified_fallback": best_qualified_fallback,
                "original_first_pass_report_path": original_report_path,
                "original_first_pass_report_sha256": original_report_sha256,
                "labels_supplied_to_model_stages": True,
                "source_scored_backlog_path": str(source_backlog_path.resolve()),
            },
        }
        repaired = build_backlog_document(
            atoms_doc=atoms_doc,
            tickets=runtime.tickets,
            input_meta={
                **source_input,
                "qualification_repair": {
                    "source_backlog_path": str(source_backlog_path.resolve()),
                    "source_backlog_sha256": _file_sha256(source_backlog_path),
                    "consumption_sha256": consumption_sha256,
                    "bundle_manifest_path": str(bundle_manifest_path.resolve()),
                    "affected_problem_ids": runtime.affected_problem_ids,
                    "same_corpus_feedback_exposed": True,
                    "release_qualification_eligible": False,
                },
            },
            artifacts=repaired_artifacts,
            miners_meta={},
        )
        repaired["generated_at_utc"] = generated_at
        if isinstance(source_backlog.get("scope"), dict):
            repaired["scope"] = dict(source_backlog["scope"])
        tickets_raw = repaired.get("tickets")
        tickets = (
            [item for item in tickets_raw if isinstance(item, dict)]
            if isinstance(tickets_raw, list)
            else []
        )
        updated_tickets, policy_meta = apply_backlog_policy(tickets, config=policy_config)
        repaired["tickets"] = updated_tickets
        repaired_artifacts["policy"] = {
            "config_path": str(policy_config_path.resolve()),
            "meta": policy_meta,
        }
        repaired["artifacts"] = repaired_artifacts
        projection = _build_export_projection(
            backlog=repaired,
            surface_area_high=set(policy_config.surface_area_high),
            cli_repo_input=repo_input,
            repo_root=repo_root,
        )
        export_contract_raw = repaired_artifacts.get("export_contract")
        export_contract = (
            dict(export_contract_raw) if isinstance(export_contract_raw, dict) else {}
        )
        export_contract.update(
            {
                "schema_version": 1,
                "projection_sha256": projection["sha256"],
                "policy_config_path": str(policy_config_path.resolve()),
            }
        )
        repaired_artifacts["export_contract"] = export_contract
        repaired["artifacts"] = repaired_artifacts

        _write_temp_bytes(
            temp_consumption_path,
            (json.dumps(runtime.consumption, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )
        write_backlog(
            repaired,
            out_json_path=temp_backlog_path,
            out_md_path=temp_backlog_md_path,
            title="Usertest Backlog (same-corpus qualification repair; not release eligible)",
        )
        artifact_paths = _export_artifact_paths(
            backlog=repaired,
            backlog_path=repaired_backlog_path,
            repo_root=repo_root,
            policy_config_path=policy_config_path,
            export_gate_config_path=export_gate_config_path,
            cli_repo_input=repo_input,
        )
        temp_artifact_paths = {
            name: _bundle_temp_path(
                path,
                final_root=repair_root,
                temp_root=temp_root,
            )
            for name, path in artifact_paths.items()
        }
        pending = write_pending_shadow_run(
            pending_path=temp_pending_path,
            backlog_path=temp_backlog_path,
            artifact_paths=temp_artifact_paths,
            qualification_manifest_sha256_expected=qualification_manifest_sha256,
            output_adjudication_sha256_pre_run=None,
            generated_at=generated_at,
        )
        pending = _publish_pending_paths(
            pending,
            temp_root=temp_root,
            final_root=repair_root,
            final_backlog_path=repaired_backlog_path,
        )
        _write_temp_bytes(
            temp_pending_path,
            (json.dumps(pending, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

        bundle_manifest: dict[str, Any] = {
            "schema_version": 1,
            "contract_kind": "qualification_repair_bundle_manifest",
            "correction_consumption_sha256": consumption_sha256,
            "source_backlog_path": str(source_backlog_path.resolve()),
            "source_backlog_sha256": _file_sha256(source_backlog_path),
            "artifact_copies": bundle_receipts,
        }
        bundle_manifest["content_sha256"] = _canonical_hash(bundle_manifest)
        _write_temp_bytes(
            temp_bundle_manifest_path,
            (json.dumps(bundle_manifest, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )
        bundle_manifest_receipt = {
            "name": "qualification_repair.bundle_manifest",
            "source_path": str(bundle_manifest_path.resolve()),
            "exists": True,
            "sha256": _file_sha256(temp_bundle_manifest_path),
            "content_sha256": bundle_manifest["content_sha256"],
            "size_bytes": temp_bundle_manifest_path.stat().st_size,
        }
        repaired_contract = build_pending_repaired_shadow_run(
            correction_consumption=runtime.consumption,
            qualification_manifest_sha256=qualification_manifest_sha256,
            repaired_backlog_sha256=_file_sha256(temp_backlog_path),
            repaired_pending_run_sha256=str(pending["content_sha256"]),
            repaired_artifact_receipts=[
                *list(pending["artifact_receipts"]),
                bundle_manifest_receipt,
            ],
            correction_consumption_path=str(consumption_path.resolve()),
            parent_cycle_contract_path=_text(
                existing_shadow.get("qualification_cycle_contract_path")
            ),
            parent_cycle_contract_sha256=_text(
                existing_shadow.get("qualification_cycle_contract_sha256")
            ),
            qualification_input_bundle_path=_text(
                existing_shadow.get("qualification_input_bundle_path")
            ),
            qualification_input_bundle_sha256=_text(
                existing_shadow.get("qualification_input_bundle_sha256")
            ),
            shared_shadow_state_path=_text(
                existing_shadow.get("shadow_state_path")
            ),
        )
        _write_temp_bytes(
            temp_contract_path,
            (
                json.dumps(repaired_contract, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8"),
        )
        if pending_repaired_shadow_run_errors(repaired_contract):
            raise ValueError("qualification_repair_staged_contract_invalid")
        if _file_sha256(temp_backlog_path) != repaired_contract["repaired_backlog_sha256"]:
            raise ValueError("qualification_repair_staged_backlog_hash_mismatch")
    except Exception:
        _quarantine_incomplete_bundle(
            temp_root,
            consumption_sha256=consumption_sha256,
        )
        raise

    try:
        temp_root.rename(repair_root)
    except FileExistsError as exc:
        _quarantine_incomplete_bundle(temp_root, consumption_sha256=consumption_sha256)
        concurrent_result = _existing_materialization_result(
            repair_root=repair_root,
            expected_consumption=runtime.consumption,
        )
        if concurrent_result is None:
            raise ValueError("qualification_repair_identity_concurrent_write") from exc
        return concurrent_result

    try:
        published_result = _existing_materialization_result(
            repair_root=repair_root,
            expected_consumption=runtime.consumption,
        )
        if published_result is None:
            raise ValueError("qualification_repair_published_bundle_missing")
        return published_result
    except Exception:
        _quarantine_incomplete_bundle(
            repair_root,
            consumption_sha256=consumption_sha256,
        )
        raise


__all__ = [
    "best_qualified_fallback_errors",
    "materialize_repaired_shadow_run",
    "select_best_qualified_fallback",
]
