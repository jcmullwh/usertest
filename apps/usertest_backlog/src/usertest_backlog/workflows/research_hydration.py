from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from backlog_core import (
    SOURCE_EVIDENCE_PROJECTION_VERSION,
    assess_research_readiness,
    source_evidence_snapshot_sha256,
)
from backlog_miner.research_evidence import verify_persisted_research_evidence


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _valid_sha256(value: Any) -> bool:
    text = _text(value)
    return bool(
        text is not None
        and len(text) == 64
        and all(character in "0123456789abcdef" for character in text.casefold())
    )


def _canonical_sha256(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _flatten_artifact_refs(value: Any, *, prefix: str = "") -> list[dict[str, str]]:
    if isinstance(value, str) and value.strip():
        return [{"name": prefix or "artifact", "path": value.strip()}]
    if not isinstance(value, Mapping):
        return []
    refs: list[dict[str, str]] = []
    for raw_key in sorted(value, key=str):
        key = str(raw_key)
        child_prefix = f"{prefix}.{key}" if prefix else key
        refs.extend(_flatten_artifact_refs(value[raw_key], prefix=child_prefix))
    return refs


def _current_research_summary(record: Mapping[str, Any]) -> dict[str, Any] | None:
    prior_raw = record.get("prior_stage_context")
    prior = prior_raw if isinstance(prior_raw, Mapping) else {}
    research_raw = prior.get("research")
    research = research_raw if isinstance(research_raw, Mapping) else {}
    current = research.get("current")
    return dict(current) if isinstance(current, Mapping) else None


def hydrate_retained_research_proof(
    record: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Load one exact retained Stage-3 dossier and re-run the current readiness gate.

    The compact case-registry summary is never itself accepted as research evidence.  It
    must point to the complete Stage-3 document, whose exact case/problem item is checked
    against the retained digest and the current research contract before reuse.
    """

    summary = _current_research_summary(record)
    if summary is None:
        return None, ["retained_research_summary_missing"]

    expected_digest = _text(summary.get("full_dossier_sha256"))
    if not _valid_sha256(expected_digest):
        return None, ["retained_research_dossier_sha256_missing_or_invalid"]

    ref_raw = summary.get("research_stage_artifact_ref")
    ref = dict(ref_raw) if isinstance(ref_raw, Mapping) else {}
    ref_name = _text(ref.get("name"))
    ref_path = _text(ref.get("path"))
    if ref_name not in {"research_json", "repro_research_json"} or ref_path is None:
        return None, ["retained_research_stage_artifact_ref_missing_or_invalid"]

    artifact_path = Path(ref_path).expanduser()
    if not artifact_path.is_file():
        return None, ["retained_research_stage_artifact_missing"]
    try:
        stage_doc = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, ["retained_research_stage_artifact_unreadable"]
    if not isinstance(stage_doc, dict) or stage_doc.get("stage") != "repro_research":
        return None, ["retained_research_stage_artifact_invalid_stage"]

    exact_ref = {"name": ref_name, "path": ref_path}
    if exact_ref not in _flatten_artifact_refs(stage_doc.get("artifacts")):
        return None, ["retained_research_stage_artifact_self_ref_mismatch"]

    case_id = _text(record.get("case_id"))
    problem_id = _text(record.get("problem_id"))
    if case_id is None or problem_id is None:
        return None, ["retained_research_case_identity_missing"]
    if _text(summary.get("case_id")) != case_id or _text(summary.get("problem_id")) != problem_id:
        return None, ["retained_research_summary_identity_mismatch"]

    raw_items = stage_doc.get("items")
    items = raw_items if isinstance(raw_items, list) else []
    matches = [
        dict(item)
        for item in items
        if isinstance(item, Mapping)
        and _text(item.get("case_id")) == case_id
        and _text(item.get("problem_id")) == problem_id
    ]
    if len(matches) != 1:
        return None, [
            "retained_research_dossier_missing"
            if not matches
            else "retained_research_dossier_ambiguous"
        ]
    dossier = matches[0]
    if _canonical_sha256(dossier) != expected_digest.casefold():
        return None, ["retained_research_dossier_sha256_mismatch"]

    current_source_atom_ids = sorted(
        {
            value.strip()
            for value in (
                record.get("source_evidence_atom_ids")
                if isinstance(record.get("source_evidence_atom_ids"), list)
                else []
            )
            if isinstance(value, str) and value.strip()
        }
    )
    current_hashes_raw = record.get("source_evidence_atom_sha256_by_id")
    current_hashes = (
        {
            str(atom_id).strip(): str(atom_sha256).casefold()
            for atom_id, atom_sha256 in current_hashes_raw.items()
            if isinstance(atom_id, str)
            and atom_id.strip()
            and _valid_sha256(atom_sha256)
        }
        if isinstance(current_hashes_raw, Mapping)
        else {}
    )
    current_snapshot_digest = _text(record.get("source_evidence_snapshot_sha256"))
    if (
        record.get("source_evidence_snapshot_complete") is not True
        or record.get("source_evidence_projection_version")
        != SOURCE_EVIDENCE_PROJECTION_VERSION
        or not current_source_atom_ids
        or set(current_hashes) != set(current_source_atom_ids)
        or not _valid_sha256(current_snapshot_digest)
        or source_evidence_snapshot_sha256(current_hashes)
        != current_snapshot_digest.casefold()
    ):
        return None, ["retained_research_current_source_evidence_snapshot_invalid"]
    try:
        current_case_revision = max(1, int(record.get("case_revision") or 1))
        researched_case_revision = max(1, int(summary.get("case_revision") or 0))
    except (TypeError, ValueError):
        return None, ["retained_research_case_revision_invalid"]
    if current_case_revision != researched_case_revision:
        return None, ["retained_research_case_revision_mismatch"]
    if _text(summary.get("source_evidence_snapshot_sha256")) != current_snapshot_digest:
        return None, ["retained_research_source_evidence_snapshot_mismatch"]

    assignment_raw = dossier.get("evidence_assignment")
    assignment = assignment_raw if isinstance(assignment_raw, Mapping) else {}
    researched_source_atom_ids = sorted(
        {
            value.strip()
            for value in (
                assignment.get("expected_atom_ids")
                if isinstance(assignment.get("expected_atom_ids"), list)
                else []
            )
            if isinstance(value, str) and value.strip()
        }
    )
    if current_source_atom_ids != researched_source_atom_ids:
        return None, ["retained_research_source_evidence_frontier_mismatch"]
    receipts_raw = assignment.get("atom_receipts")
    receipts = receipts_raw if isinstance(receipts_raw, list) else []
    researched_hashes: dict[str, str] = {}
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            return None, ["retained_research_source_evidence_receipts_invalid"]
        atom_id = _text(receipt.get("atom_id"))
        atom_sha256 = _text(receipt.get("atom_sha256"))
        if (
            atom_id is None
            or atom_id in researched_hashes
            or not _valid_sha256(atom_sha256)
            or receipt.get("source_projection_version")
            != SOURCE_EVIDENCE_PROJECTION_VERSION
        ):
            return None, ["retained_research_source_evidence_receipts_invalid"]
        researched_hashes[atom_id] = atom_sha256.casefold()
    if researched_hashes != current_hashes:
        return None, ["retained_research_source_evidence_content_mismatch"]

    summary_fields = (
        "research_schema_version",
        "repo_revision",
        "research_method",
        "reproduction_status",
        "research_status",
    )
    if any(summary.get(field) != dossier.get(field) for field in summary_fields):
        return None, ["retained_research_summary_content_mismatch"]

    ready, readiness_errors = assess_research_readiness(dossier)
    if not ready:
        return None, [
            "retained_research_proof_not_ready",
            *(f"retained_research_readiness:{error}" for error in readiness_errors),
        ]
    persisted_ready, persisted_errors = verify_persisted_research_evidence(dossier)
    if not persisted_ready:
        return None, [
            "retained_research_evidence_not_persisted",
            *(f"retained_research_persistence:{error}" for error in persisted_errors),
        ]
    return dossier, []


__all__ = ["hydrate_retained_research_proof"]
