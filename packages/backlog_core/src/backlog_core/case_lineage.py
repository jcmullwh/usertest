"""Persistent case identity and atom-lineage contracts for the backlog pipeline.

The historical pipeline treated generated ``problem_id`` and ticket prose as identity.
This module instead mints a case identity from evidence, persists aliases in a registry,
and carries that identity through every stage.  Legacy artifacts remain readable: missing
lineage fields are normalized at the boundary, while newly emitted artifacts are validated
strictly before they are written.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

from backlog_core.ticket_readiness import plan_revision_id_for

CASE_REGISTRY_SCHEMA_VERSION = 1
DOWNSTREAM_CHAIN_CONTRACT_REVISION = "runner_downstream_chain_v2"
SOURCE_EVIDENCE_PROJECTION_VERSION = 1
ATOM_DISPOSITION_RECEIPT_SCHEMA_VERSION = 1

ATOM_DISPOSITIONS: frozenset[str] = frozenset(
    {
        "supports_case",
        "duplicate",
        "expected_noise",
        "deferred",
        "novel_case",
        "unresolved",
    }
)
EVIDENCE_ROLES: frozenset[str] = frozenset(
    {"observation", "research", "implementation", "verification"}
)
EVIDENCE_CLASSES: frozenset[str] = frozenset({"observed", "proposal"})
ATOM_DISPOSITION_STATUSES: frozenset[str] = frozenset({"pending", "decided"})
ATOM_DISPOSITION_SOURCES: frozenset[str] = frozenset(
    {
        "atom_action_ledger",
        "canonical_case_membership",
        "canonical_problem_evidence",
        "case_registry_membership",
        "problem_mining_evidence_partition",
        "post_research_split",
        "runner_evidence_assignment",
        "runner_novel_case_classification",
        "runner_parent_lineage",
        "runner_target_ref",
        "runner_target_ref_lineage",
        "runner_ticket_ref",
    }
)
TERMINAL_CASE_STATES: frozenset[str] = frozenset({"resolved", "duplicate", "superseded", "split"})

_DERIVED_EVIDENCE_ROLES = frozenset({"research", "implementation", "verification"})
_SERVER_OWNED_PROBLEM_CASE_FIELDS = frozenset(
    {
        "case_id",
        "canonical_problem_id",
        "case_member_problem_ids",
        "case_revision",
        "absorbed_case_ids",
        "identity_coalesced_problem_ids",
        "source_evidence_atom_ids",
        "source_evidence_projection_version",
        "source_evidence_atom_sha256_by_id",
        "source_evidence_snapshot_complete",
        "source_evidence_snapshot_missing_atom_ids",
        "source_evidence_snapshot_sha256",
        "derived_evidence_atom_ids",
        "case_identity_status",
        "case_identity_candidate_ids",
        "provisional_same_cause_group",
        "provisional_same_cause_clearance",
        "provisional_same_cause_integrity_errors",
        "verified_causal_signature_sha256",
        "verified_causal_signature_source",
    }
)
_MISSION_ORIGIN_STAGES: Mapping[str, tuple[str, str]] = {
    # Mission identity is runner metadata. Substring matching made ordinary missions
    # such as ``investigate_verification_path_failure`` look like derived evidence and
    # silently removed their observations from problem mining.
    "backlog_repro_research": ("repro_research", "research"),
    "backlog_repro_research_dossier_repair": (
        "repro_research_dossier_repair",
        "research",
    ),
    "review_backlog_implementation_pr_v1": ("verification", "verification"),
    "implement_backlog_ticket_v1": ("implementation", "implementation"),
    "implement_maintenance_backlog_ticket_v1": ("implementation", "implementation"),
}

_RUNNER_ORIGIN_PRECEDENCE = {
    "observation": 0,
    "research": 1,
    "implementation": 2,
    "verification": 3,
}

_SOURCE_EVIDENCE_DECISION_FIELDS = frozenset(
    {
        "links",
        "artifact_links",
        "case_id",
        "supporting_case_ids",
        "disposition",
        "disposition_status",
        "disposition_receipt",
        "disposition_revisit_when",
        "evidence_role",
        "origin_stage",
        "parent_case_id",
        "parent_problem_id",
        "parent_ticket_fingerprint",
        "derived_from_atom_ids",
        "lineage_authorities",
        "lineage_validation_errors",
        "lineage_mining_blocker",
        "legacy_report_lineage_claims",
        "legacy_parent_problem_id",
        "novel_case_rationale",
    }
)


def _clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip())
    )


def source_evidence_atom_projection(atom: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact runner-owned source projection consumed by Stage 3."""

    return {
        str(key): value
        for key, value in atom.items()
        if key not in _SOURCE_EVIDENCE_DECISION_FIELDS
        and not str(key).startswith(("case_", "disposition_", "lineage_", "parent_"))
    }


def source_evidence_atom_sha256(atom: Mapping[str, Any]) -> str:
    """Hash one atom using the shared Stage-3 source-evidence projection."""

    return sha256(
        json.dumps(
            source_evidence_atom_projection(atom),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def source_evidence_snapshot_sha256(atom_sha256_by_id: Mapping[str, str]) -> str:
    """Content-address a complete per-case mapping of source atom IDs to bytes."""

    return sha256(
        json.dumps(
            {
                "source_projection_version": SOURCE_EVIDENCE_PROJECTION_VERSION,
                "atom_sha256_by_id": {
                    str(atom_id): str(atom_sha256).casefold()
                    for atom_id, atom_sha256 in sorted(atom_sha256_by_id.items())
                },
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def build_source_evidence_snapshot(
    source_evidence_atom_ids: Sequence[str],
    supporting_atoms: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the complete current source frontier or an explicit incomplete receipt."""

    expected_atom_ids = sorted(
        {
            atom_id.strip()
            for atom_id in source_evidence_atom_ids
            if isinstance(atom_id, str) and atom_id.strip()
        }
    )
    atoms_by_id = {
        atom_id: atom
        for atom in supporting_atoms
        for atom_id in [_clean_string(atom.get("atom_id"))]
        if atom_id is not None
    }
    atom_sha256_by_id = {
        atom_id: source_evidence_atom_sha256(atoms_by_id[atom_id])
        for atom_id in expected_atom_ids
        if atom_id in atoms_by_id
    }
    missing_atom_ids = [
        atom_id for atom_id in expected_atom_ids if atom_id not in atom_sha256_by_id
    ]
    complete = bool(expected_atom_ids) and not missing_atom_ids
    return {
        "source_projection_version": SOURCE_EVIDENCE_PROJECTION_VERSION,
        "expected_atom_ids": expected_atom_ids,
        "atom_sha256_by_id": atom_sha256_by_id,
        "missing_atom_ids": missing_atom_ids,
        "complete": complete,
        "snapshot_sha256": (
            source_evidence_snapshot_sha256(atom_sha256_by_id) if complete else None
        ),
    }


def provisional_same_cause_group_errors(
    value: Any,
    *,
    owning_case_id: str | None = None,
) -> list[str]:
    """Validate the durable, pre-research same-cause hypothesis packet.

    A provisional group is deliberately not an alias. Its complete member facets are
    the evidence boundary used both to research the combined unit and to decide later
    whether the hypothesis may be finalized or cleared. Older skeletal registry
    entries remain readable, but are locally blocked until a complete group is rebuilt.
    """

    if not isinstance(value, Mapping):
        return ["provisional_same_cause_group_missing"]
    errors: list[str] = []
    if value.get("schema_version") != 1:
        errors.append("provisional_same_cause_schema_invalid")
    if _clean_string(value.get("status")) != "research_hypothesis":
        errors.append("provisional_same_cause_status_invalid")
    if _clean_string(value.get("group_id")) is None:
        errors.append("provisional_same_cause_group_id_missing")

    member_case_ids = _clean_string_list(value.get("member_case_ids"))
    if len(member_case_ids) < 2:
        errors.append("provisional_same_cause_members_incomplete")
    if owning_case_id is not None and owning_case_id not in member_case_ids:
        errors.append("provisional_same_cause_owner_missing")

    facets_raw = value.get("member_facets")
    facets = (
        [dict(item) for item in facets_raw if isinstance(item, Mapping)]
        if isinstance(facets_raw, list)
        else []
    )
    facet_case_ids = [
        case_id
        for facet in facets
        for case_id in [_clean_string(facet.get("case_id"))]
        if case_id is not None
    ]
    if (
        len(facets) != len(member_case_ids)
        or len(facet_case_ids) != len(set(facet_case_ids))
        or set(facet_case_ids) != set(member_case_ids)
    ):
        errors.append("provisional_same_cause_facets_incomplete")
    for facet in facets:
        case_id = _clean_string(facet.get("case_id")) or "(missing)"
        evidence_atom_ids = set(_clean_string_list(facet.get("evidence_atom_ids")))
        source_atom_ids = set(_clean_string_list(facet.get("source_evidence_atom_ids")))
        if not evidence_atom_ids:
            errors.append(f"provisional_same_cause_facet_evidence_missing:{case_id}")
        if not source_atom_ids:
            errors.append(f"provisional_same_cause_facet_source_evidence_missing:{case_id}")
        elif not source_atom_ids.issubset(evidence_atom_ids):
            errors.append(f"provisional_same_cause_facet_source_evidence_invalid:{case_id}")
    return list(dict.fromkeys(errors))


def provisional_same_cause_clearance_errors(
    group: Any,
    clearance: Any,
) -> list[str]:
    """Validate a runner-owned, evidence-complete decision to dissolve a hypothesis."""

    errors = provisional_same_cause_group_errors(group)
    if not isinstance(group, Mapping) or not isinstance(clearance, Mapping):
        return [*errors, "provisional_same_cause_clearance_missing"]
    if _clean_string(clearance.get("group_id")) != _clean_string(group.get("group_id")):
        errors.append("provisional_same_cause_clearance_group_mismatch")
    member_case_ids = set(_clean_string_list(group.get("member_case_ids")))
    if set(_clean_string_list(clearance.get("member_case_ids"))) != member_case_ids:
        errors.append("provisional_same_cause_clearance_members_mismatch")
    expected_source_atom_ids = {
        atom_id
        for facet in (
            group.get("member_facets") if isinstance(group.get("member_facets"), list) else []
        )
        if isinstance(facet, Mapping)
        for atom_id in _clean_string_list(facet.get("source_evidence_atom_ids"))
    }
    cited_atom_ids = set(_clean_string_list(clearance.get("evidence_atom_ids")))
    if not expected_source_atom_ids or not expected_source_atom_ids.issubset(cited_atom_ids):
        errors.append("provisional_same_cause_clearance_evidence_incomplete")
    return list(dict.fromkeys(errors))


def _atom_evidence_class(atom: Mapping[str, Any]) -> str:
    """Return the semantic evidence class with a legacy source fallback."""

    explicit = _clean_string(atom.get("evidence_class"))
    if explicit in EVIDENCE_CLASSES:
        return explicit
    return "proposal" if _clean_string(atom.get("source")) == "suggested_change" else "observed"


def _confidence_value(value: Any, *, default: float) -> float:
    """Return a bounded confidence value for persisted stage-1 records."""

    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        try:
            return max(0.0, min(1.0, float(value.strip())))
        except ValueError:
            return default
    return default


def mint_case_id(seed_ids: Sequence[str], *, namespace: str = "evidence") -> str:
    """Return a deterministic case ID derived from stable evidence identifiers.

    Titles and generated summaries are intentionally excluded.  Once minted, the ID is
    stored in the case registry and reused through problem-ID and atom-ID aliases.
    """

    cleaned = sorted({item.strip() for item in seed_ids if isinstance(item, str) and item.strip()})
    if not cleaned:
        raise ValueError("mint_case_id: at least one non-empty seed identifier is required")
    blob = json.dumps(
        {"namespace": namespace, "seed_ids": cleaned},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"case:{sha256(blob).hexdigest()[:20]}"


def _origin_from_mission(mission_id: str | None) -> tuple[str, str]:
    mission = (mission_id or "").strip().lower()
    return _MISSION_ORIGIN_STAGES.get(mission, ("observation", "observation"))


def _runner_origin_from_target_ref(target_ref: Mapping[str, Any]) -> tuple[str, str]:
    """Infer origin only from runner-authored target metadata.

    Both fields are inspected because historical runners did not always persist the
    resolved ``mission_id`` after writing ``requested_mission_id``.  A resolved value
    must never downgrade a derived requested mission to an observation.
    """

    explicit, _ = _runner_lineage_from_target_ref(target_ref)
    if explicit is not None:
        return explicit[0], explicit[1]
    candidates = [
        _origin_from_mission(_clean_string(target_ref.get(field)))
        for field in ("mission_id", "requested_mission_id")
    ]
    return max(
        candidates,
        key=lambda item: _RUNNER_ORIGIN_PRECEDENCE[item[1]],
        default=("observation", "observation"),
    )


def _runner_lineage_from_target_ref(
    target_ref: Mapping[str, Any],
) -> tuple[tuple[str, str, str | None] | None, list[str]]:
    """Validate explicit runner-owned lineage without consulting model claims."""

    raw = target_ref.get("backlog_lineage")
    if raw is None:
        return None, []
    if not isinstance(raw, Mapping):
        return None, ["runner_target_ref_backlog_lineage_invalid"]
    evidence_role = _clean_string(raw.get("evidence_role"))
    origin_stage = _clean_string(raw.get("origin_stage"))
    parent_case_id = _clean_string(raw.get("parent_case_id"))
    errors: list[str] = []
    if evidence_role not in EVIDENCE_ROLES:
        errors.append("runner_target_ref_evidence_role_invalid")
    if origin_stage is None:
        errors.append("runner_target_ref_origin_stage_invalid")
    if evidence_role in _DERIVED_EVIDENCE_ROLES and parent_case_id is None:
        errors.append("runner_target_ref_parent_case_id_required")
    if errors:
        return None, errors
    assert evidence_role is not None
    assert origin_stage is not None
    return (origin_stage, evidence_role, parent_case_id), []


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _valid_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value.lower())


def make_atom_disposition_receipt(
    atom: Mapping[str, Any],
    *,
    disposition: str,
    source: str,
    rationale: str,
) -> dict[str, Any]:
    """Build a deterministic server-owned disposition receipt for one atom."""

    if disposition not in ATOM_DISPOSITIONS:
        raise ValueError(f"invalid atom disposition {disposition!r}")
    atom_id = _clean_string(atom.get("atom_id"))
    source_clean = _clean_string(source)
    rationale_clean = _clean_string(rationale)
    if atom_id is None or source_clean is None or rationale_clean is None:
        raise ValueError("atom disposition receipt requires atom_id, source, and rationale")
    if source_clean not in ATOM_DISPOSITION_SOURCES:
        raise ValueError(f"invalid atom disposition source {source_clean!r}")
    receipt: dict[str, Any] = {
        "schema_version": ATOM_DISPOSITION_RECEIPT_SCHEMA_VERSION,
        "atom_id": atom_id,
        "disposition": disposition,
        "source": source_clean,
        "rationale": rationale_clean,
        "case_id": _clean_string(atom.get("case_id")),
        "parent_case_id": _clean_string(atom.get("parent_case_id")),
        "supporting_case_ids": _atom_supporting_case_ids(atom),
    }
    proof = atom.get("disposition_proof")
    if isinstance(proof, Mapping):
        receipt["disposition_proof"] = dict(proof)
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    return receipt


def _permanent_disposition_proof_errors(
    atom: Mapping[str, Any],
    *,
    disposition: str,
    source: str,
) -> list[str]:
    """Validate the typed proof required to stop reconsidering an observation."""

    if disposition not in {"duplicate", "expected_noise"}:
        return []
    proof_raw = atom.get("disposition_proof")
    if not isinstance(proof_raw, Mapping):
        return ["permanent_disposition_proof_missing"]
    proof = dict(proof_raw)
    supplied_hash = _clean_string(proof.get("proof_sha256"))
    expected_hash = _canonical_sha256(
        {key: value for key, value in proof.items() if key != "proof_sha256"}
    )
    if supplied_hash != expected_hash:
        return ["permanent_disposition_proof_hash_mismatch"]
    atom_id = _clean_string(atom.get("atom_id"))
    if proof.get("schema_version") != 1 or _clean_string(proof.get("atom_id")) != atom_id:
        return ["permanent_disposition_proof_identity_invalid"]

    if disposition == "duplicate":
        if (
            source not in {"canonical_case_membership", "case_registry_membership"}
            or proof.get("producer") != "usertest_backlog"
            or proof.get("proof_kind") != "canonical_duplicate_relation_v1"
            or _clean_string(proof.get("duplicate_of_case_id")) is None
            or not _valid_sha256(proof.get("relation_receipt_sha256"))
            or not _valid_sha256(proof.get("relation_sha256"))
        ):
            return ["duplicate_disposition_relation_proof_invalid"]
        return []

    support_raw = proof.get("support")
    support = dict(support_raw) if isinstance(support_raw, Mapping) else {}
    if (
        proof.get("producer") != "usertest_backlog.problem_mining"
        or proof.get("proof_kind") != "runner_expected_noise_rule_v1"
        or proof.get("rule_id") != "proposal_evidence_class_v1"
        or proof.get("rule_version") != 1
    ):
        return ["expected_noise_rule_proof_invalid"]
    explicit_class = _clean_string(atom.get("evidence_class"))
    if explicit_class == "proposal":
        expected_support = {"field": "$.evidence_class", "value": "proposal"}
    elif _clean_string(atom.get("source")) == "suggested_change":
        expected_support = {"field": "$.source", "value": "suggested_change"}
    else:
        return ["expected_noise_rule_not_supported_by_atom"]
    expected_support["value_sha256"] = _canonical_sha256(expected_support["value"])
    if support != expected_support:
        return ["expected_noise_rule_support_mismatch"]
    return []


def apply_atom_disposition_decision(
    atom: Mapping[str, Any],
    *,
    disposition: str,
    source: str,
    rationale: str,
) -> dict[str, Any]:
    """Return an atom carrying an explicit, hash-bound runner disposition."""

    updated = dict(atom)
    updated["disposition"] = disposition
    updated["disposition_status"] = "decided"
    updated["disposition_receipt"] = make_atom_disposition_receipt(
        updated,
        disposition=disposition,
        source=source,
        rationale=rationale,
    )
    return updated


def atom_disposition_receipt_errors(
    atom: Mapping[str, Any],
    *,
    require_decided: bool = False,
) -> list[str]:
    """Validate disposition provenance, optionally rejecting pending decisions."""

    status = _clean_string(atom.get("disposition_status"))
    disposition = _clean_string(atom.get("disposition"))
    receipt_raw = atom.get("disposition_receipt")
    if status not in ATOM_DISPOSITION_STATUSES:
        return ["disposition_status_invalid"]
    if status == "pending":
        errors: list[str] = []
        if disposition != "unresolved":
            errors.append("pending_disposition_must_be_unresolved")
        if receipt_raw is not None:
            errors.append("pending_disposition_has_receipt")
        if require_decided:
            errors.append("disposition_decision_pending")
        return errors
    if not isinstance(receipt_raw, Mapping):
        return ["decided_disposition_receipt_missing"]
    receipt = dict(receipt_raw)
    source = _clean_string(receipt.get("source"))
    rationale = _clean_string(receipt.get("rationale"))
    if (
        source not in ATOM_DISPOSITION_SOURCES
        or rationale is None
        or disposition not in ATOM_DISPOSITIONS
    ):
        return ["decided_disposition_receipt_fields_invalid"]
    proof_errors = _permanent_disposition_proof_errors(
        atom,
        disposition=disposition,
        source=source,
    )
    if proof_errors:
        return proof_errors
    expected = make_atom_disposition_receipt(
        atom,
        disposition=disposition,
        source=source,
        rationale=rationale,
    )
    if receipt != expected:
        return ["decided_disposition_receipt_mismatch"]
    return []


def _validated_runner_evidence_assignment(
    value: Any,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate a runner-owned evidence assignment supplied outside ``report``.

    The self-hash is an integrity check, not the authority boundary.  Authority comes
    from the caller placing the assignment at the run-record boundary; a copy nested
    in model-authored report extensions is never passed here.
    """

    if value is None:
        return None, []
    if not isinstance(value, Mapping):
        return None, ["runner_evidence_assignment_not_object"]

    assignment = dict(value)
    errors: list[str] = []
    case_id = _clean_string(assignment.get("case_id"))
    problem_id = _clean_string(assignment.get("problem_id"))
    if assignment.get("status") != "complete":
        errors.append("runner_evidence_assignment_not_complete")
    if assignment.get("errors") != []:
        errors.append("runner_evidence_assignment_has_errors")
    if case_id is None:
        errors.append("runner_evidence_assignment_case_id_missing")
    if problem_id is None:
        errors.append("runner_evidence_assignment_problem_id_missing")

    expected_atom_ids = _clean_string_list(assignment.get("expected_atom_ids"))
    if not expected_atom_ids or expected_atom_ids != assignment.get("expected_atom_ids"):
        errors.append("runner_evidence_assignment_atom_ids_invalid")

    receipts_raw = assignment.get("atom_receipts")
    receipts = receipts_raw if isinstance(receipts_raw, list) else []
    receipt_ids: list[str] = []
    if not receipts:
        errors.append("runner_evidence_assignment_receipts_missing")
    for index, receipt_raw in enumerate(receipts):
        if not isinstance(receipt_raw, Mapping):
            errors.append(f"runner_evidence_assignment_receipt_invalid:{index}")
            continue
        receipt = dict(receipt_raw)
        atom_id = _clean_string(receipt.get("atom_id"))
        snapshot_raw = receipt.get("atom_snapshot")
        snapshot = dict(snapshot_raw) if isinstance(snapshot_raw, Mapping) else None
        if atom_id is None:
            errors.append(f"runner_evidence_assignment_receipt_atom_id_missing:{index}")
        else:
            receipt_ids.append(atom_id)
        if (
            snapshot is None
            or snapshot.get("atom_id") != atom_id
            or receipt.get("atom_sha256") != _canonical_sha256(snapshot)
        ):
            errors.append(f"runner_evidence_assignment_receipt_snapshot_invalid:{index}")

        if receipt.get("source_projection_version") != 1:
            errors.append(f"runner_evidence_assignment_source_projection_version_invalid:{index}")

        artifact_receipts_raw = receipt.get("artifact_receipts")
        artifact_receipts = artifact_receipts_raw if isinstance(artifact_receipts_raw, list) else []
        origin_evidence_mode = _clean_string(receipt.get("origin_evidence_mode"))
        if origin_evidence_mode is None and artifact_receipts:
            # Compatibility for retained v1 receipts written before the mode was
            # explicit. Their artifact list proves the stronger legacy shape.
            origin_evidence_mode = "snapshot_and_artifacts"
        if origin_evidence_mode not in {"signed_snapshot", "snapshot_and_artifacts"}:
            errors.append(f"runner_evidence_assignment_origin_mode_invalid:{index}:{atom_id}")
        if origin_evidence_mode == "snapshot_and_artifacts" and not artifact_receipts:
            errors.append(f"runner_evidence_assignment_origin_artifacts_missing:{index}:{atom_id}")
        if origin_evidence_mode == "signed_snapshot" and artifact_receipts:
            errors.append(
                f"runner_evidence_assignment_signed_snapshot_has_artifacts:{index}:{atom_id}"
            )
        for artifact_index, artifact in enumerate(artifact_receipts):
            if (
                not isinstance(artifact, Mapping)
                or _clean_string(artifact.get("path")) is None
                or not _valid_sha256(artifact.get("sha256"))
                or isinstance(artifact.get("size_bytes"), bool)
                or not isinstance(artifact.get("size_bytes"), int)
                or artifact.get("size_bytes", -1) < 0
            ):
                errors.append(
                    f"runner_evidence_assignment_artifact_invalid:{index}:{artifact_index}"
                )

    if sorted(receipt_ids) != sorted(expected_atom_ids):
        errors.append("runner_evidence_assignment_atom_coverage_mismatch")

    expected_hash = _canonical_sha256(
        {key: item for key, item in assignment.items() if key != "assignment_sha256"}
    )
    if assignment.get("assignment_sha256") != expected_hash:
        errors.append("runner_evidence_assignment_hash_mismatch")
    return (assignment if not errors else None), errors


def _report_lineage_claims(extensions: Mapping[str, Any]) -> dict[str, Any]:
    """Retain bounded model lineage claims for diagnosis without granting authority."""

    claims: dict[str, Any] = {}
    research_raw = extensions.get("backlog_repro_research")
    if isinstance(research_raw, Mapping):
        research: dict[str, Any] = {}
        for field in ("case_id", "parent_case_id", "problem_id"):
            value = _clean_string(research_raw.get(field))
            if value is not None:
                research[field] = value
        derived = _clean_string_list(research_raw.get("derived_from_atom_ids"))
        if derived:
            research["derived_from_atom_ids"] = derived
        claims["backlog_repro_research"] = research

    lineage_raw = extensions.get("backlog_lineage")
    if isinstance(lineage_raw, Mapping):
        lineage: dict[str, Any] = {}
        for field in (
            "case_id",
            "parent_case_id",
            "parent_problem_id",
            "origin_stage",
            "evidence_role",
            "disposition",
            "novel_case_rationale",
        ):
            value = _clean_string(lineage_raw.get(field))
            if value is not None:
                lineage[field] = value
        derived = _clean_string_list(lineage_raw.get("derived_from_atom_ids"))
        if derived:
            lineage["derived_from_atom_ids"] = derived
        claims["backlog_lineage"] = lineage
    return claims


def record_lineage_context(record: Mapping[str, Any], *, run_id: str) -> dict[str, Any]:
    """Extract lineage defaults shared by every atom emitted from one run record.

    Only runner-owned record fields establish origin and parentage.  Model-authored
    report extensions are retained as bounded legacy claims for audit readability, but
    cannot relabel evidence, choose a parent, or promote a novel case.
    """

    target_ref_raw = record.get("target_ref")
    target_ref = target_ref_raw if isinstance(target_ref_raw, Mapping) else {}
    explicit_lineage, explicit_lineage_errors = _runner_lineage_from_target_ref(target_ref)
    origin_stage, evidence_role = _runner_origin_from_target_ref(target_ref)
    authorities: list[str] = []
    if explicit_lineage is not None:
        authorities.append("runner_target_ref_lineage")
    elif evidence_role in _DERIVED_EVIDENCE_ROLES:
        authorities.append("runner_target_ref")

    explicit_parent_case_id = explicit_lineage[2] if explicit_lineage is not None else None
    parent_case_id: str | None = None
    parent_problem_id: str | None = None
    derived_from_atom_ids: list[str] = []
    lineage_errors: list[str] = list(explicit_lineage_errors)

    report_raw = record.get("report")
    report = report_raw if isinstance(report_raw, Mapping) else {}
    extensions_raw = report.get("extensions")
    extensions = extensions_raw if isinstance(extensions_raw, Mapping) else {}
    report_claims = _report_lineage_claims(extensions)

    assignment, assignment_errors = _validated_runner_evidence_assignment(
        record.get("evidence_assignment")
    )
    lineage_errors.extend(assignment_errors)
    assignment_case_id: str | None = None
    assignment_problem_id: str | None = None
    if assignment is not None:
        assignment_case_id = _clean_string(assignment.get("case_id"))
        assignment_problem_id = _clean_string(assignment.get("problem_id"))
        derived_from_atom_ids = _clean_string_list(assignment.get("expected_atom_ids"))
        if evidence_role == "observation":
            origin_stage = "repro_research"
            evidence_role = "research"
        authorities.append("runner_evidence_assignment")

    ticket_ref_raw = record.get("ticket_ref")
    ticket_ref = ticket_ref_raw if isinstance(ticket_ref_raw, Mapping) else {}
    ticket_case_candidates = {
        value
        for value in (
            _clean_string(ticket_ref.get("case_id")),
            _clean_string(ticket_ref.get("parent_case_id")),
            _clean_string(
                ticket_ref.get("ticket_provenance", {}).get("case_id")
                if isinstance(ticket_ref.get("ticket_provenance"), Mapping)
                else None
            ),
            _clean_string(
                ticket_ref.get("verification_binding", {}).get("case_id")
                if isinstance(ticket_ref.get("verification_binding"), Mapping)
                else None
            ),
        )
        if value is not None
    }
    ticket_case_id = next(iter(ticket_case_candidates), None)
    ticket_case_conflict = len(ticket_case_candidates) > 1
    if ticket_case_conflict:
        ticket_case_id = None
        lineage_errors.append("runner_ticket_ref_case_id_mismatch")
    ticket_problem_id = _clean_string(ticket_ref.get("problem_id"))
    if ticket_ref:
        if evidence_role != "verification":
            origin_stage = "implementation"
            evidence_role = "implementation"
        authorities.append("runner_ticket_ref")

    trusted_case_candidates = {
        value
        for value in (explicit_parent_case_id, assignment_case_id, ticket_case_id)
        if value is not None
    }
    trusted_parent_conflict = len(trusted_case_candidates) > 1
    if trusted_parent_conflict:
        lineage_errors.append("runner_lineage_parent_case_id_mismatch")
    elif trusted_case_candidates:
        parent_case_id = next(iter(trusted_case_candidates))
    if not ticket_case_conflict and not trusted_parent_conflict:
        parent_problem_id = ticket_problem_id or assignment_problem_id

    ticket_fingerprint = (
        None
        if ticket_case_conflict or trusted_parent_conflict
        else _clean_string(ticket_ref.get("fingerprint"))
    )
    disposition = "supports_case" if parent_case_id is not None else "unresolved"
    result = {
        "origin_run_id": run_id,
        "origin_stage": origin_stage,
        "parent_case_id": parent_case_id,
        "parent_problem_id": parent_problem_id,
        "parent_ticket_fingerprint": ticket_fingerprint,
        "derived_from_atom_ids": derived_from_atom_ids,
        "evidence_role": evidence_role,
        "case_id": None if disposition == "novel_case" else parent_case_id,
        "supporting_case_ids": (
            [parent_case_id]
            if disposition == "supports_case" and parent_case_id is not None
            else []
        ),
        "disposition": disposition,
        # The extractor finalizes a trusted parent decision after it knows the
        # source-specific atom ID.  An unparented default remains visibly pending.
        "disposition_status": "pending",
        "disposition_receipt": None,
        "lineage_authorities": list(dict.fromkeys(authorities)),
    }
    if report_claims:
        result["legacy_report_lineage_claims"] = report_claims
        research_claim = report_claims.get("backlog_repro_research")
        if isinstance(research_claim, Mapping):
            legacy_problem_id = _clean_string(research_claim.get("problem_id"))
            if legacy_problem_id is not None:
                result["legacy_parent_problem_id"] = legacy_problem_id
    if lineage_errors:
        result["lineage_validation_errors"] = list(dict.fromkeys(lineage_errors))
        result["lineage_mining_blocker"] = "invalid_runner_lineage_assignment"
    return result


def empty_case_registry() -> dict[str, Any]:
    """Return an empty versioned registry payload."""

    return {
        "schema_version": CASE_REGISTRY_SCHEMA_VERSION,
        "cases": {},
        "problem_id_to_case_id": {},
        "atom_id_to_case_id": {},
        # ``atom_id_to_case_id`` remains the compatibility/identity lookup.  A
        # source observation may legitimately support more than one distinct
        # problem facet, so the complete evidence membership is persisted here.
        "atom_id_to_case_ids": {},
        "ticket_fingerprint_to_case_id": {},
        # Operational observations are immutable occurrence-set snapshots.  Their
        # atom IDs change when a new occurrence arrives, while this stable runner
        # signature keeps every recurrence on the same canonical case.
        "operational_signature_to_case_id": {},
    }


def load_case_registry(path: Path) -> dict[str, Any]:
    """Load a registry, returning an empty one when no historical registry exists."""

    if not path.exists():
        return empty_case_registry()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid case registry {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid case registry {path}: expected a JSON object")
    version = payload.get("schema_version")
    if version != CASE_REGISTRY_SCHEMA_VERSION:
        raise ValueError(
            f"Invalid case registry {path}: expected schema_version "
            f"{CASE_REGISTRY_SCHEMA_VERSION}, got {version!r}"
        )
    normalized = empty_case_registry()
    for key in normalized:
        if key == "schema_version":
            continue
        raw = payload.get(key)
        if isinstance(raw, dict):
            normalized[key] = dict(raw)
    return normalized


def write_case_registry(path: Path, registry: Mapping[str, Any]) -> None:
    """Validate and persist a case registry as deterministic JSON."""

    payload = dict(registry)
    if payload.get("schema_version") != CASE_REGISTRY_SCHEMA_VERSION:
        raise ValueError("write_case_registry: unsupported or missing schema_version")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _registry_mapping(registry: Mapping[str, Any] | None, key: str) -> dict[str, str]:
    if not isinstance(registry, Mapping):
        return {}
    raw = registry.get(key)
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(alias): case_id.strip()
        for alias, case_id in raw.items()
        if isinstance(case_id, str) and case_id.strip()
    }


def _registry_atom_case_memberships(
    registry: Mapping[str, Any] | None,
) -> dict[str, list[str]]:
    """Return complete persisted atom memberships with legacy-primary fallback."""

    memberships: dict[str, set[str]] = {}
    if isinstance(registry, Mapping):
        raw = registry.get("atom_id_to_case_ids")
        if isinstance(raw, Mapping):
            for atom_id, raw_case_ids in raw.items():
                if not isinstance(atom_id, str) or not atom_id.strip():
                    continue
                case_ids = _clean_string_list(raw_case_ids)
                if case_ids:
                    memberships.setdefault(atom_id.strip(), set()).update(case_ids)
    for atom_id, case_id in _registry_mapping(registry, "atom_id_to_case_id").items():
        memberships.setdefault(atom_id, set()).add(case_id)
    return {atom_id: sorted(case_ids) for atom_id, case_ids in memberships.items() if case_ids}


def derived_source_atom_id_aliases(atom: Mapping[str, Any]) -> list[str]:
    """Return durable pre-content-addressing identities for a re-ingested atom.

    Derived-run ingestion uses a content-addressed record identity so the same run
    discovered through more than one evidence root is suppressed deterministically.
    Older plan and outcome records identify those atoms by the durable source-root
    kind plus the run-relative path.  Both names refer to the same runner-owned
    artifact; resolving the latter as a registry alias prevents an already-known
    observation from being mined as a new problem after re-ingestion.

    The alias is reconstructed only from runner-authored structured fields and only
    when the atom's source and ordinal agree with its content-addressed ID.  Free-form
    evidence text and generated ticket wording never participate in identity.
    """

    atom_id = _clean_string(atom.get("atom_id"))
    root_kind = _clean_string(atom.get("derived_source_root_kind"))
    run_rel = _clean_string(atom.get("derived_source_run_rel"))
    source = _clean_string(atom.get("source"))
    if (
        atom_id is None
        or root_kind is None
        or run_rel is None
        or source is None
        or not atom_id.startswith("__derived__/")
    ):
        return []
    root_kind = root_kind.replace("\\", "/").strip("/")
    run_rel = run_rel.replace("\\", "/").strip("/")
    if not root_kind or not run_rel or root_kind in {".", ".."}:
        return []
    if any(part in {"", ".", ".."} for part in run_rel.split("/")):
        return []
    parts = atom_id.rsplit(":", 2)
    if (
        len(parts) != 3
        or parts[1] != source
        or not parts[2].isdigit()
        or not parts[0].startswith(f"__derived__/{root_kind}/")
    ):
        return []
    return [f"{root_kind}/{run_rel}:{source}:{parts[2]}"]


def _atom_supporting_case_ids(atom: Mapping[str, Any]) -> list[str]:
    """Return deterministic complete memberships for one dispositioned atom."""

    case_ids = set(_clean_string_list(atom.get("supporting_case_ids")))
    case_id = _clean_string(atom.get("case_id"))
    if case_id is not None:
        case_ids.add(case_id)
    return sorted(case_ids)


def _operational_candidate_integrity_errors(atom: Mapping[str, Any]) -> list[str]:
    """Validate the narrow runner-owned observation exception without an import cycle."""

    if _clean_string(atom.get("source")) != "operational_failure_candidate":
        return []
    # ``operational_candidates`` uses this module to normalize its final synthetic
    # atom, so importing the validator at module import time would create a cycle.
    # Calls occur only after both modules are initialized.
    from backlog_core.operational_candidates import operational_candidate_receipt_errors

    return operational_candidate_receipt_errors(atom)


def _operational_candidate_signature(atom: Mapping[str, Any]) -> str | None:
    if _clean_string(atom.get("source")) != "operational_failure_candidate":
        return None
    signature = _clean_string(atom.get("operational_candidate_signature"))
    return signature if _valid_sha256(signature) else None


def _operational_candidate_signature_from_atom_id(atom_id: str) -> str | None:
    """Return the stable signature encoded by a synthetic candidate atom ID."""

    parts = atom_id.split(":")
    if (
        len(parts) != 3
        or parts[0] != "operational_failure"
        or not _valid_sha256(parts[1])
        or not _valid_sha256(parts[2])
    ):
        return None
    return parts[1].casefold()


def normalize_atom_lineage(
    atoms: Sequence[dict[str, Any]],
    *,
    case_registry: Mapping[str, Any] | None = None,
    strict_new_output: bool = False,
) -> list[dict[str, Any]]:
    """Return atoms with a complete lineage/disposition contract.

    Missing fields are inferred for legacy inputs.  When ``strict_new_output`` is true,
    the normalized result is validated as a newly emitted artifact.
    """

    by_problem = _registry_mapping(case_registry, "problem_id_to_case_id")
    by_atom = _registry_mapping(case_registry, "atom_id_to_case_id")
    atom_memberships = _registry_atom_case_memberships(case_registry)
    by_fingerprint = _registry_mapping(case_registry, "ticket_fingerprint_to_case_id")
    normalized: list[dict[str, Any]] = []

    for index, source_atom in enumerate(atoms):
        atom = dict(source_atom)
        original_disposition = _clean_string(atom.get("disposition"))
        original_receipt_raw = atom.get("disposition_receipt")
        original_receipt = (
            dict(original_receipt_raw) if isinstance(original_receipt_raw, Mapping) else None
        )
        original_receipt_valid = _clean_string(
            atom.get("disposition_status")
        ) == "decided" and not atom_disposition_receipt_errors(atom)
        atom_id = _clean_string(atom.get("atom_id"))
        run_id = _clean_string(atom.get("origin_run_id")) or _clean_string(atom.get("run_id"))
        mission_id = _clean_string(atom.get("mission_id"))
        inferred_stage, inferred_role = _origin_from_mission(mission_id)

        origin_stage = _clean_string(atom.get("origin_stage")) or inferred_stage
        evidence_role = _clean_string(atom.get("evidence_role")) or inferred_role
        if evidence_role not in EVIDENCE_ROLES:
            evidence_role = "observation"
        evidence_class = _atom_evidence_class(atom)

        parent_case_id = _clean_string(atom.get("parent_case_id"))
        parent_problem_id = _clean_string(atom.get("parent_problem_id"))
        parent_fingerprint = _clean_string(atom.get("parent_ticket_fingerprint"))
        if parent_case_id is None and parent_problem_id is not None:
            parent_case_id = by_problem.get(parent_problem_id)
        if parent_case_id is None and parent_fingerprint is not None:
            parent_case_id = by_fingerprint.get(parent_fingerprint)

        case_id = _clean_string(atom.get("case_id"))
        registry_identities = list(
            dict.fromkeys(
                [
                    *([atom_id] if atom_id is not None else []),
                    *derived_source_atom_id_aliases(atom),
                ]
            )
        )
        persisted_primary_case_ids = list(
            dict.fromkeys(
                by_atom[identity]
                for identity in registry_identities
                if identity in by_atom
            )
        )
        persisted_memberships = sorted(
            {
                case_id
                for identity in registry_identities
                for case_id in atom_memberships.get(identity, [])
            }
        )
        persisted_atom_case_id = (
            persisted_primary_case_ids[0]
            if len(persisted_primary_case_ids) == 1
            else persisted_memberships[0]
            if not persisted_primary_case_ids and len(persisted_memberships) == 1
            else None
        )
        if (
            evidence_role in _DERIVED_EVIDENCE_ROLES
            and parent_case_id is None
            and persisted_atom_case_id is not None
        ):
            # The case registry is runner-owned durable state.  It is the only safe
            # fallback for historical derived atoms whose original run predates a
            # runner evidence-assignment sidecar.
            parent_case_id = persisted_atom_case_id
        if case_id is None:
            case_id = persisted_atom_case_id
        if case_id is None:
            case_id = parent_case_id

        disposition = _clean_string(atom.get("disposition"))
        if disposition not in ATOM_DISPOSITIONS:
            disposition = "supports_case" if case_id is not None else "unresolved"
        elif persisted_atom_case_id is not None and disposition != "novel_case":
            # Extractors attach an ``unresolved`` default before the persistent registry
            # is loaded.  Registry membership is the durable disposition decision and
            # must win on later cycles or the same observation is mined again.
            case_id = persisted_atom_case_id
            disposition = "supports_case"
        if evidence_role in _DERIVED_EVIDENCE_ROLES and parent_case_id is not None:
            if len(persisted_memberships) > 1:
                raise ValueError(
                    f"atoms[{index}]: derived evidence cannot support multiple cases: "
                    + ", ".join(persisted_memberships)
                )
            persisted_distinct_case = (
                persisted_atom_case_id is not None and persisted_atom_case_id != parent_case_id
            )
            if persisted_distinct_case:
                case_id = persisted_atom_case_id
                disposition = "novel_case"
                atom.setdefault(
                    "novel_case_rationale",
                    "Persisted atom-to-case identity records a distinct derived failure.",
                )
            elif disposition == "novel_case":
                # Retain the parent as lineage, not identity.  The new case is
                # minted from this derived failure after stage-1 classification.
                if case_id == parent_case_id:
                    case_id = None
            else:
                case_id = parent_case_id
                disposition = "supports_case"

        supporting_case_ids = set(_clean_string_list(atom.get("supporting_case_ids")))
        supporting_case_ids.update(persisted_memberships)
        if case_id is not None:
            supporting_case_ids.add(case_id)
        if evidence_role in _DERIVED_EVIDENCE_ROLES:
            # Derived evidence has exactly one causal parent by default.  An explicit
            # novel derived failure may have a distinct case, but it is still singular.
            supporting_case_ids = {case_id} if case_id is not None else set()

        atom.update(
            {
                "origin_run_id": run_id,
                "origin_stage": origin_stage,
                "parent_case_id": parent_case_id,
                "derived_from_atom_ids": _clean_string_list(atom.get("derived_from_atom_ids")),
                "evidence_role": evidence_role,
                "evidence_class": evidence_class,
                "case_id": case_id,
                "supporting_case_ids": sorted(supporting_case_ids),
                "disposition": disposition,
            }
        )
        if parent_problem_id is not None:
            atom["parent_problem_id"] = parent_problem_id
        if parent_fingerprint is not None:
            atom["parent_ticket_fingerprint"] = parent_fingerprint

        if original_receipt_valid and original_disposition == disposition:
            assert original_receipt is not None
            atom = apply_atom_disposition_decision(
                atom,
                disposition=disposition,
                source=str(original_receipt["source"]),
                rationale=str(original_receipt["rationale"]),
            )
        elif disposition == "supports_case":
            if persisted_atom_case_id is not None:
                decision_source = "case_registry_membership"
                decision_rationale = (
                    "The persistent case registry assigns this atom to "
                    + ", ".join(sorted(supporting_case_ids))
                    + "."
                )
            elif evidence_role in _DERIVED_EVIDENCE_ROLES:
                decision_source = "runner_parent_lineage"
                decision_rationale = (
                    f"Runner-owned lineage attaches this derived atom to {parent_case_id}."
                )
            else:
                decision_source = "canonical_case_membership"
                decision_rationale = (
                    "Canonical case membership assigns this atom to "
                    + ", ".join(sorted(supporting_case_ids))
                    + "."
                )
            atom = apply_atom_disposition_decision(
                atom,
                disposition=disposition,
                source=decision_source,
                rationale=decision_rationale,
            )
        elif (
            disposition == "novel_case"
            and _clean_string(atom.get("novel_case_rationale")) is not None
        ):
            atom = apply_atom_disposition_decision(
                atom,
                disposition=disposition,
                source="runner_novel_case_classification",
                rationale=str(atom["novel_case_rationale"]),
            )
        else:
            atom["disposition_status"] = "pending"
            atom["disposition_receipt"] = None

        operational_errors = _operational_candidate_integrity_errors(atom)
        if operational_errors:
            prior_case_id = _clean_string(atom.get("case_id"))
            if prior_case_id is not None:
                atom["invalid_operational_candidate_prior_case_id"] = prior_case_id
            atom["case_id"] = None
            atom["supporting_case_ids"] = []
            atom["disposition"] = "unresolved"
            atom["disposition_status"] = "pending"
            atom["disposition_receipt"] = None
            existing_errors = _clean_string_list(atom.get("lineage_validation_errors"))
            atom["lineage_validation_errors"] = list(
                dict.fromkeys(
                    [
                        *existing_errors,
                        *[
                            f"operational_candidate_integrity:{error}"
                            for error in operational_errors
                        ],
                    ]
                )
            )
            atom["lineage_mining_blocker"] = "invalid_operational_candidate_receipt"

        if strict_new_output:
            validate_atom_lineage(atom, context=f"atoms[{index}]")
        normalized.append(atom)

    return normalized


def validate_atom_lineage(atom: Mapping[str, Any], *, context: str = "atom") -> None:
    """Validate the lineage fields required on newly emitted atoms."""

    atom_id = _clean_string(atom.get("atom_id"))
    if atom_id is None:
        raise ValueError(f"{context}: missing non-empty atom_id")
    if _clean_string(atom.get("origin_run_id")) is None:
        raise ValueError(f"{context}: missing non-empty origin_run_id")
    if _clean_string(atom.get("origin_stage")) is None:
        raise ValueError(f"{context}: missing non-empty origin_stage")
    role = _clean_string(atom.get("evidence_role"))
    if role not in EVIDENCE_ROLES:
        raise ValueError(f"{context}: invalid evidence_role {role!r}")
    evidence_class = _clean_string(atom.get("evidence_class"))
    if evidence_class not in EVIDENCE_CLASSES:
        raise ValueError(f"{context}: invalid evidence_class {evidence_class!r}")
    disposition = _clean_string(atom.get("disposition"))
    if disposition not in ATOM_DISPOSITIONS:
        raise ValueError(f"{context}: invalid disposition {disposition!r}")
    if not isinstance(atom.get("derived_from_atom_ids"), list):
        raise ValueError(f"{context}: derived_from_atom_ids must be a list")
    raw_supporting_case_ids = atom.get("supporting_case_ids")
    if not isinstance(raw_supporting_case_ids, list):
        raise ValueError(f"{context}: supporting_case_ids must be a list")
    supporting_case_ids = _clean_string_list(raw_supporting_case_ids)
    if supporting_case_ids != sorted(supporting_case_ids) or len(supporting_case_ids) != len(
        raw_supporting_case_ids
    ):
        raise ValueError(
            f"{context}: supporting_case_ids must contain sorted unique non-empty strings"
        )
    case_id = _clean_string(atom.get("case_id"))
    disposition_receipt_errors = atom_disposition_receipt_errors(atom)
    if disposition_receipt_errors:
        raise ValueError(
            f"{context}: invalid disposition provenance: " + ", ".join(disposition_receipt_errors)
        )
    if disposition == "supports_case":
        if case_id is None:
            raise ValueError(f"{context}: supports_case disposition requires case_id")
        if case_id not in supporting_case_ids:
            raise ValueError(f"{context}: primary case_id must be included in supporting_case_ids")
    if role in _DERIVED_EVIDENCE_ROLES and disposition == "supports_case":
        parent_case_id = _clean_string(atom.get("parent_case_id"))
        if parent_case_id is None:
            raise ValueError(
                f"{context}: derived evidence supporting a case requires parent_case_id"
            )
        if supporting_case_ids != [parent_case_id] or case_id != parent_case_id:
            raise ValueError(f"{context}: derived evidence must support exactly its parent case")
    if role in _DERIVED_EVIDENCE_ROLES and disposition == "novel_case":
        parent_case_id = _clean_string(atom.get("parent_case_id"))
        if parent_case_id is None:
            raise ValueError(f"{context}: distinct derived failure requires parent_case_id lineage")
        if _clean_string(atom.get("novel_case_rationale")) is None:
            raise ValueError(f"{context}: distinct derived failure requires novel_case_rationale")
        if case_id == parent_case_id:
            raise ValueError(f"{context}: distinct derived failure cannot reuse its parent case_id")
        if len(supporting_case_ids) > 1:
            raise ValueError(f"{context}: distinct derived failure must remain single-case")


def eligible_problem_mining_atoms(atoms: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return atoms allowed to originate a problem case.

    Derived research/implementation/verification evidence updates its parent by default.
    It can originate a distinct case only after an explicit ``novel_case`` disposition.
    Previously supported/duplicate/noise observations are not re-mined. Deferred
    observations remain eligible so a temporary decision cannot hide real evidence
    forever; every later cycle independently revisits it until it becomes a case,
    an evidence-backed duplicate, or expected noise.
    """

    eligible: list[dict[str, Any]] = []
    for atom in atoms:
        if atom_is_idea_originated(atom):
            continue
        if _operational_candidate_integrity_errors(atom):
            # Defense in depth for callers that supply an unnormalized atom. Full
            # normalization also retains the exact validation errors and blocker.
            continue
        role = _clean_string(atom.get("evidence_role")) or "observation"
        disposition = _clean_string(atom.get("disposition")) or "unresolved"
        if (
            _clean_string(atom.get("lineage_mining_blocker")) is not None
            and disposition != "novel_case"
        ):
            continue
        if disposition == "novel_case" and role in _DERIVED_EVIDENCE_ROLES:
            if (
                _clean_string(atom.get("parent_case_id")) is None
                or _clean_string(atom.get("novel_case_rationale")) is None
                or atom_disposition_receipt_errors(atom, require_decided=True)
            ):
                continue
        if disposition == "novel_case" and _clean_string(atom.get("case_id")) is None:
            eligible.append(dict(atom))
        elif role == "observation" and disposition in {"unresolved", "deferred"}:
            eligible.append(dict(atom))
        elif role == "observation" and disposition in {"duplicate", "expected_noise"}:
            # Old/model-only permanent exclusions are reconsidered. Only a typed,
            # hash-bound canonical relation or runner noise rule may suppress them.
            if atom_disposition_receipt_errors(atom, require_decided=True):
                eligible.append(dict(atom))
    return eligible


def atom_is_idea_originated(atom: Mapping[str, Any]) -> bool:
    """Return whether an atom came from the external IDEA intake boundary.

    Automated problem mining must never reinterpret user/external ideas as observed
    runtime evidence. Historical producers used a few field names, so the exclusion is
    deliberately explicit across those bounded origin markers.
    """

    if atom.get("idea_originated") is True:
        return True
    for field in ("source", "category", "origin_stage", "evidence_origin"):
        value = _clean_string(atom.get(field))
        if value is not None and value.casefold() in {"idea", "ideas", "external_idea"}:
            return True
    return False


def atom_is_independent_problem_evidence(atom: Mapping[str, Any]) -> bool:
    """Return whether an automated atom may define its own problem work unit.

    Ordinary observations are independent evidence. Research, implementation, and
    verification atoms remain attached to their parent case by default, but a
    runner-classified ``novel_case`` is intentionally different: it represents a
    distinct failure discovered while processing the parent. Treat that one narrow,
    receipted state as source-like for recovery and stability accounting so a real
    pipeline failure cannot disappear merely because it was observed downstream.
    """

    if atom_is_idea_originated(atom):
        return False
    role = _clean_string(atom.get("evidence_role")) or "observation"
    if role not in _DERIVED_EVIDENCE_ROLES:
        return True
    if _clean_string(atom.get("disposition")) != "novel_case":
        return False
    parent_case_id = _clean_string(atom.get("parent_case_id"))
    case_id = _clean_string(atom.get("case_id"))
    receipt_raw = atom.get("disposition_receipt")
    receipt = receipt_raw if isinstance(receipt_raw, Mapping) else {}
    candidate = bool(
        parent_case_id is not None
        and _clean_string(atom.get("novel_case_rationale")) is not None
        and case_id != parent_case_id
        and _clean_string(atom.get("evidence_class")) == "observed"
        and _clean_string(receipt.get("source"))
        in {"runner_novel_case_classification", "atom_action_ledger"}
        and _clean_string(atom.get("lineage_mining_blocker")) is None
        and not _operational_candidate_integrity_errors(atom)
        and not atom_disposition_receipt_errors(atom, require_decided=True)
    )
    if not candidate:
        return False
    try:
        validate_atom_lineage(dict(atom), context="independent_problem_evidence")
    except ValueError:
        return False
    return True


def _evidence_seed_ids(
    evidence_ids: Sequence[str], atoms_by_id: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    seeds: list[str] = []
    for atom_id in evidence_ids:
        atom = atoms_by_id.get(atom_id) or {}
        if _clean_string(atom.get("disposition")) == "novel_case":
            seeds.append(atom_id)
            continue
        derived = _clean_string_list(atom.get("derived_from_atom_ids"))
        seeds.extend(derived or [atom_id])
    return sorted(set(seeds))


def assign_problem_case_ids(
    problem_records: Sequence[dict[str, Any]],
    atoms: Sequence[dict[str, Any]],
    *,
    case_registry: Mapping[str, Any] | None = None,
    strict_new_output: bool = True,
) -> list[dict[str, Any]]:
    """Assign stable case IDs to newly mined problem records.

    Registry aliases and parent-linked atoms take precedence.  A new case ID is minted
    only when no persisted identity applies, and its seed is evidence identity rather than
    generated title text.  Multiple records resolving to the same case are coalesced.
    """

    atoms_by_id: dict[str, Mapping[str, Any]] = {
        atom_id: atom
        for atom in atoms
        for atom_id in [_clean_string(atom.get("atom_id"))]
        if atom_id is not None
    }
    by_problem = _registry_mapping(case_registry, "problem_id_to_case_id")
    by_atom = _registry_mapping(case_registry, "atom_id_to_case_id")
    by_operational_signature = _registry_mapping(
        case_registry,
        "operational_signature_to_case_id",
    )
    registry_cases_raw = case_registry.get("cases") if isinstance(case_registry, Mapping) else None
    registry_cases = registry_cases_raw if isinstance(registry_cases_raw, Mapping) else {}

    assigned: list[dict[str, Any]] = []
    seen_problem_ids: set[str] = set()
    for index, raw_record in enumerate(problem_records):
        record = dict(raw_record)
        supplied_server_fields = sorted(
            field
            for field in _SERVER_OWNED_PROBLEM_CASE_FIELDS
            if field in record and record.get(field) is not None
        )
        if strict_new_output and supplied_server_fields:
            raise ValueError(
                f"problem_records[{index}]: model supplied server-owned case fields: "
                + ", ".join(supplied_server_fields)
            )
        for field in _SERVER_OWNED_PROBLEM_CASE_FIELDS:
            record.pop(field, None)
        problem_id = _clean_string(record.get("problem_id"))
        evidence_ids = _clean_string_list(record.get("evidence_atom_ids"))
        if strict_new_output:
            if problem_id is None:
                raise ValueError(f"problem_records[{index}]: missing non-empty problem_id")
            if not evidence_ids:
                raise ValueError(
                    f"problem_records[{index}] {problem_id}: evidence_atom_ids must be non-empty"
                )
            missing = sorted(set(evidence_ids) - set(atoms_by_id))
            if missing:
                raise ValueError(
                    f"problem_records[{index}] {problem_id}: unknown evidence atom IDs: "
                    + ", ".join(missing[:8])
                )
            observed_evidence_ids = [
                atom_id
                for atom_id in evidence_ids
                if _atom_evidence_class(atoms_by_id.get(atom_id) or {}) == "observed"
            ]
            if not observed_evidence_ids:
                raise ValueError(
                    f"problem_records[{index}] {problem_id}: proposal-only evidence "
                    "cannot originate or support a problem case"
                )
        if problem_id is None:
            continue
        if problem_id in seen_problem_ids:
            raise ValueError(f"problem_records[{index}]: duplicate problem_id {problem_id!r}")
        seen_problem_ids.add(problem_id)

        candidates: set[str] = set()
        related_parent_cases: set[str] = set()
        operational_signatures: set[str] = set()
        persisted_problem_case = by_problem.get(problem_id)
        if persisted_problem_case is not None:
            candidates.add(persisted_problem_case)
        for atom_id in evidence_ids:
            atom = atoms_by_id.get(atom_id) or {}
            operational_signature = _operational_candidate_signature(atom)
            if operational_signature is not None:
                operational_signatures.add(operational_signature)
                persisted_operational_case = by_operational_signature.get(operational_signature)
                if persisted_operational_case is not None:
                    candidates.add(persisted_operational_case)
            is_novel = _clean_string(atom.get("disposition")) == "novel_case"
            parent_case_id = _clean_string(atom.get("parent_case_id"))
            atom_candidates = (
                (_clean_string(atom.get("case_id")), by_atom.get(atom_id))
                if is_novel
                else (parent_case_id, _clean_string(atom.get("case_id")), by_atom.get(atom_id))
            )
            for candidate in atom_candidates:
                if candidate is not None:
                    candidates.add(candidate)
            if is_novel and parent_case_id is not None:
                related_parent_cases.add(parent_case_id)

        identity_status = "resolved"
        identity_candidates: list[str] = []
        if len(candidates) == 1:
            case_id = next(iter(candidates))
        elif len(candidates) > 1:
            # Evidence already owned by multiple durable cases is a relation question,
            # not a reason to manufacture a third identity.  Keep the record visible as
            # one pending relation-review packet, route it through an existing identity,
            # and retain the complete candidate set for the reviewer and downstream
            # evidence gates.  A merge/split/same-cause decision must resolve it before
            # implementation planning can advance.
            identity_candidates = sorted(candidates)
            case_id = (
                persisted_problem_case
                if persisted_problem_case in candidates
                else identity_candidates[0]
            )
            identity_status = "pending_relation"
        elif not candidates and operational_signatures:
            # The first occurrence set mints identity from the stable failure
            # signature, never from its mutable set of occurrence atom IDs.
            case_id = mint_case_id(
                [f"operational_signature:{value}" for value in sorted(operational_signatures)]
            )
        else:
            seeds = _evidence_seed_ids(evidence_ids, atoms_by_id)
            case_id = mint_case_id(seeds or evidence_ids)

        persisted_case_raw = registry_cases.get(case_id)
        persisted_case = persisted_case_raw if isinstance(persisted_case_raw, Mapping) else {}
        split_child_case_ids = _clean_string_list(persisted_case.get("child_case_ids"))
        if _clean_string(persisted_case.get("state")) == "split" and split_child_case_ids:
            # A broad signature which was previously split is still evidence for the
            # durable parent relation node. It must not silently select whichever child
            # sorts first (or reopen the parent as if the split never happened).
            identity_status = "pending_relation"
            identity_candidates = sorted(split_child_case_ids)
            related_parent_cases.update(split_child_case_ids)

        record["case_id"] = case_id
        record["case_identity_status"] = identity_status
        if identity_candidates:
            record["case_identity_candidate_ids"] = identity_candidates
        record["canonical_problem_id"] = problem_id
        record["case_member_problem_ids"] = [problem_id]
        record["case_revision"] = max(1, int(record.get("case_revision") or 1))
        record["evidence_atom_ids"] = evidence_ids
        record["source_evidence_atom_ids"] = [
            atom_id
            for atom_id in evidence_ids
            if (
                _clean_string((atoms_by_id.get(atom_id) or {}).get("evidence_role"))
                or "observation"
            )
            not in _DERIVED_EVIDENCE_ROLES
        ]
        record["derived_evidence_atom_ids"] = [
            atom_id
            for atom_id in evidence_ids
            if (
                _clean_string((atoms_by_id.get(atom_id) or {}).get("evidence_role"))
                or "observation"
            )
            in _DERIVED_EVIDENCE_ROLES
        ]
        related = sorted(
            {
                *_clean_string_list(record.get("related_case_ids")),
                *related_parent_cases,
                *(candidate for candidate in candidates if candidate != case_id),
            }
        )
        if related:
            record["related_case_ids"] = related
        assigned.append(record)

    # Identical persisted evidence identity is one work unit, even if separate miners used
    # different generated problem IDs.  This is identity reconciliation, not a semantic merge.
    by_case: dict[str, dict[str, Any]] = {}
    case_order: list[str] = []
    for record in assigned:
        case_id = str(record["case_id"])
        existing = by_case.get(case_id)
        if existing is None:
            by_case[case_id] = dict(record)
            case_order.append(case_id)
            continue
        identity_statuses = {
            value
            for value in (
                _clean_string(existing.get("case_identity_status")),
                _clean_string(record.get("case_identity_status")),
            )
            if value is not None
        }
        if "pending_relation" in identity_statuses:
            existing["case_identity_status"] = "pending_relation"
        elif "provisional_same_cause" in identity_statuses:
            existing["case_identity_status"] = "provisional_same_cause"
        else:
            existing["case_identity_status"] = "resolved"
        identity_candidates = sorted(
            {
                *_clean_string_list(existing.get("case_identity_candidate_ids")),
                *_clean_string_list(record.get("case_identity_candidate_ids")),
            }
        )
        if identity_candidates:
            existing["case_identity_candidate_ids"] = identity_candidates
        elif existing["case_identity_status"] == "resolved":
            existing.pop("case_identity_candidate_ids", None)
        existing["evidence_atom_ids"] = list(
            dict.fromkeys(
                _clean_string_list(existing.get("evidence_atom_ids"))
                + _clean_string_list(record.get("evidence_atom_ids"))
            )
        )
        existing["source_evidence_atom_ids"] = list(
            dict.fromkeys(
                _clean_string_list(existing.get("source_evidence_atom_ids"))
                + _clean_string_list(record.get("source_evidence_atom_ids"))
            )
        )
        existing["derived_evidence_atom_ids"] = list(
            dict.fromkeys(
                _clean_string_list(existing.get("derived_evidence_atom_ids"))
                + _clean_string_list(record.get("derived_evidence_atom_ids"))
            )
        )
        existing["case_member_problem_ids"] = list(
            dict.fromkeys(
                _clean_string_list(existing.get("case_member_problem_ids"))
                + _clean_string_list(record.get("case_member_problem_ids"))
            )
        )
        existing["identity_coalesced_problem_ids"] = list(
            dict.fromkeys(
                _clean_string_list(existing.get("identity_coalesced_problem_ids"))
                + [_clean_string(record.get("problem_id")) or ""]
            )
        )
        related_case_ids = sorted(
            {
                *_clean_string_list(existing.get("related_case_ids")),
                *_clean_string_list(record.get("related_case_ids")),
            }
        )
        if related_case_ids:
            existing["related_case_ids"] = related_case_ids
        by_case[case_id] = existing
    return [by_case[case_id] for case_id in case_order]


def apply_atom_dispositions(
    atoms: Sequence[dict[str, Any]],
    problem_cases: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach cited and derived atoms to canonical cases and disposition every atom."""

    cited_by: dict[str, set[str]] = {}
    absorbed_case_aliases: dict[str, str] = {}
    split_parent_case_ids: set[str] = set()
    for case in problem_cases:
        case_id = _clean_string(case.get("case_id"))
        if case_id is None:
            raise ValueError("apply_atom_dispositions: problem case missing case_id")
        for absorbed_case_id in _clean_string_list(case.get("absorbed_case_ids")):
            previous_target = absorbed_case_aliases.get(absorbed_case_id)
            if previous_target is not None and previous_target != case_id:
                raise ValueError(
                    "apply_atom_dispositions: absorbed case resolves to multiple canonical "
                    f"cases ({absorbed_case_id}, {previous_target}, {case_id})"
                )
            absorbed_case_aliases[absorbed_case_id] = case_id
        split_parent_case_id = _clean_string(case.get("split_from_case_id"))
        if split_parent_case_id is not None:
            split_parent_case_ids.add(split_parent_case_id)
        for atom_id in _clean_string_list(case.get("evidence_atom_ids")):
            cited_by.setdefault(atom_id, set()).add(case_id)

    def canonicalize_membership(case_id: str) -> str:
        seen: set[str] = set()
        while case_id not in seen and case_id in absorbed_case_aliases:
            seen.add(case_id)
            case_id = absorbed_case_aliases[case_id]
        return case_id

    updated: list[dict[str, Any]] = []
    for raw_atom in atoms:
        atom = dict(raw_atom)
        if _clean_string(atom.get("evidence_class")) is None:
            # Historical atom snapshots predate the explicit observed/proposal field.
            # Apply the same bounded source fallback used by full lineage normalization
            # before validating the newly dispositioned record.
            atom["evidence_class"] = _atom_evidence_class(atom)
        prior_receipt_raw = atom.get("disposition_receipt")
        prior_receipt = dict(prior_receipt_raw) if isinstance(prior_receipt_raw, Mapping) else None
        prior_receipt_valid = _clean_string(
            atom.get("disposition_status")
        ) == "decided" and not atom_disposition_receipt_errors(atom)
        current_atom_id = _clean_string(atom.get("atom_id"))
        cited_cases = cited_by.get(current_atom_id or "", set())
        parent_case = _clean_string(atom.get("parent_case_id"))
        role = _clean_string(atom.get("evidence_role")) or "observation"
        existing_primary = _clean_string(atom.get("case_id"))
        supporting_case_ids = {
            canonicalize_membership(case_id) for case_id in _atom_supporting_case_ids(atom)
        }
        if cited_cases:
            # A split partitions the old parent evidence.  Current child citations are
            # authoritative for that parent; unrelated historical facet memberships stay.
            supporting_case_ids.difference_update(split_parent_case_ids)
            supporting_case_ids.update(cited_cases)
            if role in _DERIVED_EVIDENCE_ROLES and len(supporting_case_ids) != 1:
                raise ValueError(
                    f"apply_atom_dispositions: derived atom {current_atom_id!r} supports "
                    "multiple canonical cases (" + ", ".join(sorted(supporting_case_ids)) + ")"
                )
            canonical_existing = (
                canonicalize_membership(existing_primary) if existing_primary is not None else None
            )
            primary_case_id = (
                canonical_existing
                if canonical_existing in supporting_case_ids
                else min(supporting_case_ids)
            )
            atom["case_id"] = primary_case_id
            atom["supporting_case_ids"] = sorted(supporting_case_ids)
            if atom.get("disposition") != "novel_case":
                atom["disposition"] = "supports_case"
        elif role in _DERIVED_EVIDENCE_ROLES and parent_case is not None:
            atom["case_id"] = parent_case
            atom["supporting_case_ids"] = [parent_case]
            if atom.get("disposition") != "novel_case":
                atom["disposition"] = "supports_case"
        elif _clean_string(atom.get("disposition")) not in ATOM_DISPOSITIONS:
            atom["disposition"] = "unresolved"
            atom["supporting_case_ids"] = sorted(supporting_case_ids)
        else:
            atom["supporting_case_ids"] = sorted(supporting_case_ids)

        final_disposition = _clean_string(atom.get("disposition")) or "unresolved"
        if final_disposition == "supports_case":
            if cited_cases:
                atom = apply_atom_disposition_decision(
                    atom,
                    disposition=final_disposition,
                    source="canonical_problem_evidence",
                    rationale="Canonical problem mining cited this atom for case membership.",
                )
            elif prior_receipt_valid:
                assert prior_receipt is not None
                atom = apply_atom_disposition_decision(
                    atom,
                    disposition=final_disposition,
                    source=str(prior_receipt["source"]),
                    rationale=str(prior_receipt["rationale"]),
                )
            elif role in _DERIVED_EVIDENCE_ROLES and parent_case is not None:
                atom = apply_atom_disposition_decision(
                    atom,
                    disposition=final_disposition,
                    source="runner_parent_lineage",
                    rationale=(
                        f"Runner-owned lineage attaches this derived atom to {parent_case}."
                    ),
                )
            else:
                atom = apply_atom_disposition_decision(
                    atom,
                    disposition=final_disposition,
                    source="canonical_case_membership",
                    rationale=(
                        "Canonical case membership assigns this atom to "
                        + ", ".join(sorted(supporting_case_ids))
                        + "."
                    ),
                )
        elif prior_receipt_valid:
            assert prior_receipt is not None
            atom = apply_atom_disposition_decision(
                atom,
                disposition=final_disposition,
                source=str(prior_receipt["source"]),
                rationale=str(prior_receipt["rationale"]),
            )
        elif (
            final_disposition == "novel_case"
            and _clean_string(atom.get("novel_case_rationale")) is not None
        ):
            atom = apply_atom_disposition_decision(
                atom,
                disposition=final_disposition,
                source="runner_novel_case_classification",
                rationale=str(atom["novel_case_rationale"]),
            )
        else:
            atom["disposition_status"] = "pending"
            atom["disposition_receipt"] = None
        validate_atom_lineage(atom)
        updated.append(atom)
    return updated


def attach_supporting_atoms_to_problem_cases(
    problem_cases: Sequence[dict[str, Any]],
    atoms: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach already-classified supporting evidence to its active parent work unit.

    Derived research, implementation, and verification atoms are deliberately excluded
    from new-case mining.  Exclusion must not discard their evidence: this helper adds
    them to the canonical case record that will flow through prioritization and later
    stages.  Absorbed case IDs are treated as aliases of the canonical work unit.
    """

    support_by_case: dict[str, list[dict[str, Any]]] = {}
    for atom in atoms:
        if _clean_string(atom.get("disposition")) != "supports_case":
            continue
        atom_id = _clean_string(atom.get("atom_id"))
        if atom_id is None:
            continue
        role = _clean_string(atom.get("evidence_role")) or "observation"
        if role in _DERIVED_EVIDENCE_ROLES:
            parent_case_id = _clean_string(atom.get("parent_case_id"))
            case_ids = [parent_case_id] if parent_case_id is not None else []
        else:
            case_ids = _atom_supporting_case_ids(atom)
        for case_id in case_ids:
            support_by_case.setdefault(case_id, []).append(atom)

    attached: list[dict[str, Any]] = []
    for raw_case in problem_cases:
        case = dict(raw_case)
        case_id = _clean_string(case.get("case_id"))
        represented_ids = {
            candidate
            for candidate in [
                case_id,
                *_clean_string_list(case.get("absorbed_case_ids")),
                *(
                    _clean_string_list(case.get("case_identity_candidate_ids"))
                    if case.get("case_identity_status") == "provisional_same_cause"
                    else []
                ),
            ]
            if candidate is not None
        }
        supporting_atoms = [
            atom
            for represented_id in represented_ids
            for atom in support_by_case.get(represented_id, [])
        ]
        evidence_ids = _clean_string_list(case.get("evidence_atom_ids"))
        evidence_ids.extend(
            atom_id
            for atom in supporting_atoms
            for atom_id in [_clean_string(atom.get("atom_id"))]
            if atom_id is not None
        )
        case["evidence_atom_ids"] = list(dict.fromkeys(evidence_ids))

        evidence_id_set = set(case["evidence_atom_ids"])
        derived_ids = [
            atom_id
            for atom_id in _clean_string_list(case.get("derived_evidence_atom_ids"))
            if atom_id in evidence_id_set
        ]
        derived_ids.extend(
            atom_id
            for atom in supporting_atoms
            if _clean_string(atom.get("evidence_role")) in _DERIVED_EVIDENCE_ROLES
            for atom_id in [_clean_string(atom.get("atom_id"))]
            if atom_id is not None
        )
        derived_ids = list(dict.fromkeys(derived_ids))
        case["derived_evidence_atom_ids"] = derived_ids
        source_ids = [
            atom_id
            for atom_id in _clean_string_list(case.get("source_evidence_atom_ids"))
            if atom_id in evidence_id_set
        ]
        source_ids.extend(
            atom_id
            for atom in supporting_atoms
            if _clean_string(atom.get("evidence_role")) not in _DERIVED_EVIDENCE_ROLES
            for atom_id in [_clean_string(atom.get("atom_id"))]
            if atom_id is not None
        )
        source_ids.extend(
            atom_id for atom_id in case["evidence_atom_ids"] if atom_id not in set(derived_ids)
        )
        case["source_evidence_atom_ids"] = list(dict.fromkeys(source_ids))
        attached.append(case)
    return attached


def build_case_registry(
    problem_cases: Sequence[dict[str, Any]],
    *,
    previous: Mapping[str, Any] | None = None,
    supporting_atoms: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Update a persistent registry from canonical problem work units."""

    registry = empty_case_registry()
    if isinstance(previous, Mapping):
        for key in registry:
            if key == "schema_version":
                continue
            raw = previous.get(key)
            if isinstance(raw, Mapping):
                registry[key] = dict(raw)

    cases = registry["cases"]
    problem_map = registry["problem_id_to_case_id"]
    atom_map = registry["atom_id_to_case_id"]
    atom_membership_map = registry["atom_id_to_case_ids"]
    fingerprint_map = registry["ticket_fingerprint_to_case_id"]
    operational_signature_map = registry["operational_signature_to_case_id"]
    assert isinstance(cases, dict)
    assert isinstance(problem_map, dict)
    assert isinstance(atom_map, dict)
    assert isinstance(atom_membership_map, dict)
    assert isinstance(fingerprint_map, dict)
    assert isinstance(operational_signature_map, dict)
    current_atom_memberships: dict[str, set[str]] = {}
    current_atom_primary: dict[str, str] = {}
    current_operational_signature_cases: dict[str, set[str]] = {}
    supporting_atoms_by_id = {
        atom_id: atom
        for atom in supporting_atoms
        for atom_id in [_clean_string(atom.get("atom_id"))]
        if atom_id is not None
    }

    for record in problem_cases:
        case_id = _clean_string(record.get("case_id"))
        canonical_problem_id = _clean_string(record.get("canonical_problem_id")) or _clean_string(
            record.get("problem_id")
        )
        if case_id is None or canonical_problem_id is None:
            raise ValueError("build_case_registry: canonical record missing case_id/problem_id")
        previous_entry_raw = cases.get(case_id)
        previous_entry = dict(previous_entry_raw) if isinstance(previous_entry_raw, Mapping) else {}
        member_problem_ids = list(
            dict.fromkeys(
                _clean_string_list(previous_entry.get("problem_ids"))
                + [canonical_problem_id]
                + _clean_string_list(record.get("case_member_problem_ids"))
            )
        )
        current_evidence = _clean_string_list(record.get("evidence_atom_ids"))
        current_operational_atom_ids_by_signature: dict[str, set[str]] = {}
        for atom_id in current_evidence:
            signature = _operational_candidate_signature(
                supporting_atoms_by_id.get(atom_id) or {}
            )
            if signature is not None:
                current_operational_atom_ids_by_signature.setdefault(
                    signature,
                    set(),
                ).add(atom_id)
        previous_evidence_all = _clean_string_list(
            previous_entry.get("evidence_atom_ids")
        )
        superseded_operational_atom_ids = sorted(
            {
                atom_id
                for atom_id in previous_evidence_all
                for signature in [
                    _operational_candidate_signature_from_atom_id(atom_id)
                ]
                if signature in current_operational_atom_ids_by_signature
                and atom_id
                not in current_operational_atom_ids_by_signature.get(signature, set())
            }
        )
        previous_evidence_list = [
            atom_id
            for atom_id in previous_evidence_all
            if atom_id not in set(superseded_operational_atom_ids)
        ]
        evidence_ids = list(dict.fromkeys(previous_evidence_list + current_evidence))
        derived_evidence_ids = list(
            dict.fromkeys(
                _clean_string_list(previous_entry.get("derived_evidence_atom_ids"))
                + _clean_string_list(record.get("derived_evidence_atom_ids"))
            )
        )
        source_evidence_ids = list(
            dict.fromkeys(
                [
                    atom_id
                    for atom_id in _clean_string_list(
                        previous_entry.get("source_evidence_atom_ids")
                    )
                    if atom_id not in set(superseded_operational_atom_ids)
                ]
                + _clean_string_list(record.get("source_evidence_atom_ids"))
                + [atom_id for atom_id in evidence_ids if atom_id not in set(derived_evidence_ids)]
            )
        )
        source_snapshot = build_source_evidence_snapshot(
            source_evidence_ids,
            supporting_atoms,
        )
        previous_source_hashes_raw = previous_entry.get("source_evidence_atom_sha256_by_id")
        previous_source_hashes = (
            {
                str(atom_id): str(atom_sha256).casefold()
                for atom_id, atom_sha256 in previous_source_hashes_raw.items()
                if _clean_string(atom_id) is not None and _valid_sha256(atom_sha256)
            }
            if isinstance(previous_source_hashes_raw, Mapping)
            else {}
        )
        current_source_hashes = dict(source_snapshot["atom_sha256_by_id"])
        requested_revision = max(1, int(record.get("case_revision") or 1))
        previous_revision = max(0, int(previous_entry.get("case_revision") or 0))
        source_content_changed = bool(
            previous_entry
            and previous_entry.get("source_evidence_snapshot_complete") is True
            and source_snapshot["complete"] is True
            and previous_source_hashes
            and set(previous_source_hashes) == set(current_source_hashes)
            and previous_source_hashes != current_source_hashes
        )
        evidence_changed = bool(previous_entry) and (
            bool(set(evidence_ids) - set(previous_evidence_list)) or source_content_changed
        )
        case_revision = max(requested_revision, previous_revision)
        if evidence_changed:
            case_revision = max(case_revision, previous_revision + 1)

        canonical_symptoms = _clean_string_list(record.get("canonical_symptoms"))
        if not canonical_symptoms:
            canonical_symptoms = _clean_string_list(previous_entry.get("canonical_symptoms"))
        problem_text = _clean_string(record.get("problem")) or _clean_string(
            previous_entry.get("problem")
        )
        if not canonical_symptoms and problem_text is not None:
            canonical_symptoms = [problem_text]

        entry = {
            **previous_entry,
            "case_id": case_id,
            "canonical_problem_id": canonical_problem_id,
            "problem_ids": member_problem_ids,
            "evidence_atom_ids": evidence_ids,
            "source_evidence_atom_ids": source_evidence_ids,
            "source_evidence_projection_version": source_snapshot["source_projection_version"],
            "source_evidence_atom_sha256_by_id": current_source_hashes,
            "source_evidence_snapshot_complete": source_snapshot["complete"],
            "source_evidence_snapshot_missing_atom_ids": source_snapshot["missing_atom_ids"],
            "source_evidence_snapshot_sha256": source_snapshot["snapshot_sha256"],
            "superseded_operational_evidence_atom_ids": list(
                dict.fromkeys(
                    _clean_string_list(
                        previous_entry.get(
                            "superseded_operational_evidence_atom_ids"
                        )
                    )
                    + superseded_operational_atom_ids
                )
            ),
            "case_revision": case_revision,
            "same_cause_group_id": _clean_string(record.get("same_cause_group_id"))
            or _clean_string(previous_entry.get("same_cause_group_id")),
            "state": _clean_string(record.get("case_state"))
            or _clean_string(previous_entry.get("state"))
            or "active",
            "title": _clean_string(record.get("title"))
            or _clean_string(previous_entry.get("title")),
            "problem": problem_text,
            "user_impact": _clean_string(record.get("user_impact"))
            or _clean_string(previous_entry.get("user_impact")),
            "severity": _clean_string(record.get("severity"))
            or _clean_string(previous_entry.get("severity"))
            or "medium",
            "confidence": _confidence_value(
                record.get("confidence"),
                default=_confidence_value(previous_entry.get("confidence"), default=0.5),
            ),
            "problem_status": _clean_string(record.get("problem_status"))
            or _clean_string(previous_entry.get("problem_status"))
            or "identified",
            "suggested_owner": _clean_string(record.get("suggested_owner"))
            or _clean_string(previous_entry.get("suggested_owner")),
            "evidence_summary": _clean_string(record.get("evidence_summary"))
            or _clean_string(previous_entry.get("evidence_summary")),
            "canonical_symptoms": canonical_symptoms,
            "root_cause_status": _clean_string(record.get("root_cause_status"))
            or _clean_string(previous_entry.get("root_cause_status"))
            or "unestablished",
            "related_case_ids": list(
                dict.fromkeys(
                    _clean_string_list(previous_entry.get("related_case_ids"))
                    + _clean_string_list(record.get("related_case_ids"))
                    + _clean_string_list(record.get("absorbed_case_ids"))
                )
            ),
            "derived_evidence_atom_ids": derived_evidence_ids,
            "occurrence_evidence_atom_ids": list(
                dict.fromkeys(
                    _clean_string_list(previous_entry.get("occurrence_evidence_atom_ids"))
                    + _clean_string_list(record.get("occurrence_evidence_atom_ids"))
                )
            ),
            "split_from_case_id": _clean_string(record.get("split_from_case_id"))
            or _clean_string(previous_entry.get("split_from_case_id")),
            "split_parent_problem_id": _clean_string(record.get("split_parent_problem_id"))
            or _clean_string(previous_entry.get("split_parent_problem_id")),
        }
        split_receipt_raw = record.get("post_research_split_receipt")
        previous_split_receipt_raw = previous_entry.get("post_research_split_receipt")
        if isinstance(split_receipt_raw, Mapping):
            entry["post_research_split_receipt"] = deepcopy(dict(split_receipt_raw))
        elif isinstance(previous_split_receipt_raw, Mapping):
            entry["post_research_split_receipt"] = deepcopy(dict(previous_split_receipt_raw))
        causal_signature = _clean_string(record.get("verified_causal_signature_sha256"))
        causal_signature_source = _clean_string(record.get("verified_causal_signature_source"))
        if (
            _valid_sha256(causal_signature)
            and causal_signature_source == "runner_verified_causal_signature_v1"
        ):
            entry["verified_causal_signature_sha256"] = causal_signature.casefold()
            entry["verified_causal_signature_source"] = causal_signature_source
        requested_identity_status = _clean_string(record.get("case_identity_status"))
        prior_identity_status = _clean_string(previous_entry.get("case_identity_status"))
        record_provisional = record.get("provisional_same_cause_group")
        previous_provisional = previous_entry.get("provisional_same_cause_group")
        clearance = record.get("provisional_same_cause_clearance")
        clears_previous_provisional = bool(
            isinstance(previous_provisional, Mapping)
            and (
                not provisional_same_cause_clearance_errors(previous_provisional, clearance)
                or (
                    requested_identity_status == "resolved"
                    and bool(_clean_string_list(record.get("absorbed_case_ids")))
                )
            )
        )
        provisional_group: Mapping[str, Any] | None = None
        if isinstance(record_provisional, Mapping):
            provisional_group = record_provisional
        elif isinstance(previous_provisional, Mapping) and not clears_previous_provisional:
            provisional_group = previous_provisional

        if provisional_group is not None:
            provisional_errors = provisional_same_cause_group_errors(
                provisional_group,
                owning_case_id=case_id,
            )
            entry["provisional_same_cause_group"] = deepcopy(provisional_group)
            identity_candidates = sorted(
                {
                    *_clean_string_list(previous_entry.get("case_identity_candidate_ids")),
                    *_clean_string_list(record.get("case_identity_candidate_ids")),
                    *_clean_string_list(provisional_group.get("member_case_ids")),
                }
            )
            if identity_candidates:
                entry["case_identity_candidate_ids"] = identity_candidates
            if provisional_errors:
                entry["case_identity_status"] = "pending_relation"
                entry["provisional_same_cause_integrity_errors"] = provisional_errors
            else:
                entry["case_identity_status"] = (
                    "pending_relation"
                    if requested_identity_status == "pending_relation"
                    else "provisional_same_cause"
                )
                entry.pop("provisional_same_cause_integrity_errors", None)
        else:
            identity_status = requested_identity_status or prior_identity_status
            if identity_status is not None:
                entry["case_identity_status"] = identity_status
            if identity_status == "resolved" or clears_previous_provisional:
                entry.pop("case_identity_candidate_ids", None)
                entry.pop("provisional_same_cause_group", None)
                entry.pop("provisional_same_cause_integrity_errors", None)
                entry["case_identity_status"] = "resolved"
            else:
                identity_candidates = _clean_string_list(
                    record.get("case_identity_candidate_ids")
                ) or _clean_string_list(previous_entry.get("case_identity_candidate_ids"))
                if identity_candidates:
                    entry["case_identity_candidate_ids"] = identity_candidates
        reopened_from_state = _clean_string(record.get("reopened_from_state"))
        if reopened_from_state in TERMINAL_CASE_STATES:
            lifecycle_raw = previous_entry.get("current_lifecycle")
            lifecycle = lifecycle_raw if isinstance(lifecycle_raw, Mapping) else {}
            outcome_reference_raw = lifecycle.get("outcome_reference")
            outcome_reference = (
                outcome_reference_raw if isinstance(outcome_reference_raw, Mapping) else {}
            )
            against_plan_revision_id = _clean_string(outcome_reference.get("plan_revision_id"))
            if against_plan_revision_id is not None:
                entry["recurrence_reopen"] = {
                    "from_state": reopened_from_state,
                    "against_plan_revision_id": against_plan_revision_id,
                    "case_revision": case_revision,
                    "new_evidence_atom_ids": sorted(
                        set(evidence_ids)
                        - set(_clean_string_list(previous_entry.get("evidence_atom_ids")))
                    ),
                }
        cases[case_id] = entry
        for problem_id in member_problem_ids:
            problem_map[problem_id] = case_id
        for atom_id in evidence_ids:
            current_atom_memberships.setdefault(atom_id, set()).add(case_id)
            operational_signature = _operational_candidate_signature(
                supporting_atoms_by_id.get(atom_id) or {}
            )
            if operational_signature is not None:
                current_operational_signature_cases.setdefault(
                    operational_signature,
                    set(),
                ).add(case_id)
        # Split children cite original occurrences through a separate immutable
        # receipt; those observations are support membership, not child-owned
        # canonical evidence and must not be hidden behind only the facet context.
        for atom_id in _clean_string_list(record.get("occurrence_evidence_atom_ids")):
            current_atom_memberships.setdefault(atom_id, set()).add(case_id)
        for fingerprint in _clean_string_list(record.get("ticket_fingerprints")):
            fingerprint_map[fingerprint] = case_id
        for absorbed_case_id in _clean_string_list(record.get("absorbed_case_ids")):
            absorbed_previous = cases.get(absorbed_case_id)
            if isinstance(absorbed_previous, Mapping):
                canonical_raw = cases.get(case_id)
                canonical_entry = (
                    dict(canonical_raw) if isinstance(canonical_raw, Mapping) else entry
                )
                cases[case_id] = _merge_case_stage_lineage_entries(
                    canonical_entry,
                    absorbed_previous,
                )
            alias_entry = (
                dict(absorbed_previous)
                if isinstance(absorbed_previous, Mapping)
                else {"case_id": absorbed_case_id}
            )
            alias_entry["state"] = "alias"
            alias_entry["alias_of"] = case_id
            alias_entry.pop("case_identity_candidate_ids", None)
            alias_entry.pop("provisional_same_cause_group", None)
            alias_entry["case_identity_status"] = "resolved"
            cases[absorbed_case_id] = alias_entry

    # Persist split lineage after all children exist.  The parent remains a durable
    # graph node but is no longer an active work unit; its original problem aliases
    # continue resolving to the parent rather than arbitrarily choosing one child.
    split_children_by_parent: dict[str, list[dict[str, Any]]] = {}
    superseded_split_child_case_ids: set[str] = set()
    for record in problem_cases:
        parent_case_id = _clean_string(record.get("split_from_case_id"))
        if parent_case_id is not None:
            split_children_by_parent.setdefault(parent_case_id, []).append(record)
    for parent_case_id, child_records in split_children_by_parent.items():
        parent_raw = cases.get(parent_case_id)
        parent_entry = dict(parent_raw) if isinstance(parent_raw, Mapping) else {}
        first_child = child_records[0]
        parent_problem_ids = list(
            dict.fromkeys(
                _clean_string_list(parent_entry.get("problem_ids"))
                + _clean_string_list(first_child.get("split_parent_problem_ids"))
            )
        )
        parent_problem_id = _clean_string(
            first_child.get("split_parent_problem_id")
        ) or _clean_string(parent_entry.get("canonical_problem_id"))
        if parent_problem_id is not None and parent_problem_id not in parent_problem_ids:
            parent_problem_ids.insert(0, parent_problem_id)
        previous_child_case_ids = _clean_string_list(parent_entry.get("child_case_ids"))
        child_case_ids = [
            child_case_id
            for child in child_records
            for child_case_id in [_clean_string(child.get("case_id"))]
            if child_case_id is not None
        ]
        child_case_ids = list(dict.fromkeys(child_case_ids))
        historical_child_case_ids = list(
            dict.fromkeys(
                _clean_string_list(parent_entry.get("historical_child_case_ids"))
                + previous_child_case_ids
                + child_case_ids
            )
        )
        current_children_by_id = {
            child_case_id: child
            for child in child_records
            for child_case_id in [_clean_string(child.get("case_id"))]
            if child_case_id is not None
        }
        split_receipts = [
            deepcopy(dict(receipt))
            for child in child_records
            for receipt in [child.get("post_research_split_receipt")]
            if isinstance(receipt, Mapping)
        ]
        unique_current_receipts = {
            json.dumps(receipt, sort_keys=True, separators=(",", ":")): receipt
            for receipt in split_receipts
        }
        if len(unique_current_receipts) > 1:
            raise ValueError(
                f"build_case_registry: split children disagree on receipt for {parent_case_id}"
            )
        current_split_receipt = (
            next(iter(unique_current_receipts.values())) if unique_current_receipts else None
        )
        previous_current_receipt = parent_entry.get("current_post_research_split_receipt")
        if not isinstance(previous_current_receipt, Mapping):
            historical_receipts_raw = parent_entry.get("post_research_split_receipts")
            historical_receipts = (
                [receipt for receipt in historical_receipts_raw if isinstance(receipt, Mapping)]
                if isinstance(historical_receipts_raw, list)
                else []
            )
            previous_current_receipt = historical_receipts[-1] if historical_receipts else None
        previous_split_revision = max(
            0,
            int(parent_entry.get("split_revision") or 0),
            1 if isinstance(previous_current_receipt, Mapping) else 0,
        )
        split_revision = previous_split_revision or 1
        if (
            isinstance(current_split_receipt, Mapping)
            and isinstance(previous_current_receipt, Mapping)
            and dict(current_split_receipt) != dict(previous_current_receipt)
        ):
            split_revision = previous_split_revision + 1

        for current_child_case_id in child_case_ids:
            current_raw = cases.get(current_child_case_id)
            if not isinstance(current_raw, Mapping):
                continue
            current_entry = dict(current_raw)
            current_entry["state"] = "active"
            current_entry.pop("superseded_by_case_ids", None)
            current_entry.pop("superseded_by_split_receipt", None)
            current_entry.pop("superseded_by_split_revision", None)
            cases[current_child_case_id] = current_entry

        for old_child_case_id in previous_child_case_ids:
            if old_child_case_id in current_children_by_id:
                continue
            old_raw = cases.get(old_child_case_id)
            if not isinstance(old_raw, Mapping):
                continue
            old_entry = dict(old_raw)
            old_occurrences = set(_clean_string_list(old_entry.get("occurrence_evidence_atom_ids")))
            replacements = [
                current_child_case_id
                for current_child_case_id, current_child in current_children_by_id.items()
                if old_occurrences.intersection(
                    _clean_string_list(current_child.get("occurrence_evidence_atom_ids"))
                )
            ]
            old_entry["state"] = "superseded"
            old_entry["superseded_by_case_ids"] = replacements or child_case_ids
            old_entry["superseded_by_split_revision"] = split_revision
            if isinstance(current_split_receipt, Mapping):
                old_entry["superseded_by_split_receipt"] = deepcopy(dict(current_split_receipt))
            cases[old_child_case_id] = old_entry
            superseded_split_child_case_ids.add(old_child_case_id)
        parent_entry.update(
            {
                "case_id": parent_case_id,
                "canonical_problem_id": parent_problem_id,
                "problem_ids": parent_problem_ids,
                "state": "split",
                "child_case_ids": child_case_ids,
                "historical_child_case_ids": historical_child_case_ids,
                "split_revision": split_revision,
                "title": _clean_string(parent_entry.get("title"))
                or _clean_string(first_child.get("title")),
                "problem": _clean_string(parent_entry.get("problem"))
                or _clean_string(first_child.get("problem")),
                "user_impact": _clean_string(parent_entry.get("user_impact"))
                or _clean_string(first_child.get("user_impact")),
            }
        )
        if isinstance(current_split_receipt, Mapping):
            parent_entry["current_post_research_split_receipt"] = deepcopy(
                dict(current_split_receipt)
            )
        if split_receipts:
            previous_receipts_raw = parent_entry.get("post_research_split_receipts")
            previous_receipts = (
                [
                    deepcopy(dict(receipt))
                    for receipt in previous_receipts_raw
                    if isinstance(receipt, Mapping)
                ]
                if isinstance(previous_receipts_raw, list)
                else []
            )
            parent_entry["post_research_split_receipts"] = list(
                {
                    json.dumps(receipt, sort_keys=True, separators=(",", ":")): receipt
                    for receipt in [*previous_receipts, *split_receipts]
                }.values()
            )
        cases[parent_case_id] = parent_entry
        for problem_id in parent_problem_ids:
            problem_map[problem_id] = parent_case_id

    def _canonical_registry_case_id(raw_case_id: str) -> str:
        seen: set[str] = set()
        case_id = raw_case_id
        while case_id not in seen:
            seen.add(case_id)
            raw_entry = cases.get(case_id)
            if not isinstance(raw_entry, Mapping):
                break
            alias_of = _clean_string(raw_entry.get("alias_of"))
            if alias_of is None:
                break
            case_id = alias_of
        return case_id

    # Supporting evidence updates every represented case.  Derived evidence is singular;
    # an original observation may support multiple distinct facets.
    for atom in supporting_atoms:
        if _clean_string(atom.get("disposition")) != "supports_case":
            continue
        supporting_atom_id = _clean_string(atom.get("atom_id"))
        if supporting_atom_id is None:
            continue
        role = _clean_string(atom.get("evidence_role")) or "observation"
        if role in _DERIVED_EVIDENCE_ROLES:
            raw_case_id = _clean_string(atom.get("parent_case_id")) or _clean_string(
                atom.get("case_id")
            )
            raw_case_ids = [raw_case_id] if raw_case_id is not None else []
            if len(_atom_supporting_case_ids(atom)) > 1:
                raise ValueError(
                    "build_case_registry: derived evidence cannot support multiple cases"
                )
        else:
            raw_case_ids = _atom_supporting_case_ids(atom)
        case_ids = sorted({_canonical_registry_case_id(value) for value in raw_case_ids})
        operational_signature = _operational_candidate_signature(atom)
        if operational_signature is not None:
            current_operational_signature_cases.setdefault(
                operational_signature,
                set(),
            ).update(case_ids)
        primary = _clean_string(atom.get("case_id"))
        if primary is not None:
            current_atom_primary[supporting_atom_id] = _canonical_registry_case_id(primary)
        for case_id in case_ids:
            raw_entry = cases.get(case_id)
            if not isinstance(raw_entry, Mapping):
                continue
            entry = dict(raw_entry)
            evidence_ids = _clean_string_list(entry.get("evidence_atom_ids"))
            is_new_evidence = supporting_atom_id not in evidence_ids
            if is_new_evidence:
                evidence_ids.append(supporting_atom_id)
                entry["evidence_atom_ids"] = evidence_ids
                entry["case_revision"] = max(1, int(entry.get("case_revision") or 1)) + 1
            if role in _DERIVED_EVIDENCE_ROLES:
                derived_ids = _clean_string_list(entry.get("derived_evidence_atom_ids"))
                if supporting_atom_id not in derived_ids:
                    derived_ids.append(supporting_atom_id)
                entry["derived_evidence_atom_ids"] = derived_ids
            else:
                source_ids = _clean_string_list(entry.get("source_evidence_atom_ids"))
                if supporting_atom_id not in source_ids:
                    source_ids.append(supporting_atom_id)
                entry["source_evidence_atom_ids"] = source_ids
            cases[case_id] = entry
            current_atom_memberships.setdefault(supporting_atom_id, set()).add(case_id)

    previous_memberships = _registry_atom_case_memberships(previous)
    previous_primary = _registry_mapping(previous, "atom_id_to_case_id")
    split_parent_for_child = {
        child_case_id: parent_case_id
        for parent_case_id, child_records in split_children_by_parent.items()
        for child in child_records
        for child_case_id in [_clean_string(child.get("case_id"))]
        if child_case_id is not None
    }
    all_atom_ids = sorted(set(previous_memberships) | set(current_atom_memberships))
    rebuilt_primary: dict[str, str] = {}
    rebuilt_memberships: dict[str, list[str]] = {}
    for atom_id in all_atom_ids:
        current_case_ids = {
            _canonical_registry_case_id(case_id)
            for case_id in current_atom_memberships.get(atom_id, set())
        }
        persisted_case_ids = {
            _canonical_registry_case_id(case_id)
            for case_id in previous_memberships.get(atom_id, [])
            if case_id not in superseded_split_child_case_ids
        }
        # A split is an explicit evidence partition, so the split parent ceases to be
        # a membership for atoms now assigned to one of its children.
        persisted_case_ids.difference_update(
            split_parent_for_child[case_id]
            for case_id in current_case_ids
            if case_id in split_parent_for_child
        )
        memberships = sorted(persisted_case_ids | current_case_ids)
        if not memberships:
            continue
        preferred = current_atom_primary.get(atom_id)
        if preferred is None:
            persisted_primary = previous_primary.get(atom_id)
            if persisted_primary is not None:
                preferred = _canonical_registry_case_id(persisted_primary)
        primary = preferred if preferred in memberships else memberships[0]
        rebuilt_primary[atom_id] = primary
        rebuilt_memberships[atom_id] = memberships

    atom_map.clear()
    atom_map.update(rebuilt_primary)
    atom_membership_map.clear()
    atom_membership_map.update(rebuilt_memberships)

    # Preserve one durable case identity for each exact operational failure
    # signature.  New occurrence-set atoms may extend/reopen that case, but may not
    # silently redirect the signature to an unrelated case.  Explicit relation merges
    # are safe because aliases canonicalize to the same node here.
    combined_operational_signature_cases: dict[str, set[str]] = {
        signature: {_canonical_registry_case_id(case_id)}
        for signature, case_id in _registry_mapping(
            registry,
            "operational_signature_to_case_id",
        ).items()
        if _valid_sha256(signature)
    }
    for signature, case_ids in current_operational_signature_cases.items():
        combined_operational_signature_cases.setdefault(signature, set()).update(
            _canonical_registry_case_id(case_id) for case_id in case_ids
        )
    rebuilt_operational_signature_map: dict[str, str] = {}
    for signature, case_ids in sorted(combined_operational_signature_cases.items()):
        canonical_case_ids = sorted({_canonical_registry_case_id(case_id) for case_id in case_ids})
        if len(canonical_case_ids) != 1:
            raise ValueError(
                "build_case_registry: operational signature maps to multiple canonical cases: "
                f"{signature}: " + ", ".join(canonical_case_ids)
            )
        rebuilt_operational_signature_map[signature] = canonical_case_ids[0]
    operational_signature_map.clear()
    operational_signature_map.update(rebuilt_operational_signature_map)

    return registry


def _canonical_registry_case_id(registry: Mapping[str, Any], raw_case_id: str) -> str:
    """Resolve an alias to its canonical registry node without crossing splits."""

    raw_cases = registry.get("cases")
    cases = raw_cases if isinstance(raw_cases, Mapping) else {}
    seen: set[str] = set()
    case_id = raw_case_id
    while case_id not in seen:
        seen.add(case_id)
        raw_entry = cases.get(case_id)
        if not isinstance(raw_entry, Mapping):
            break
        alias_of = _clean_string(raw_entry.get("alias_of"))
        if alias_of is None:
            break
        case_id = alias_of
    return case_id


def _case_id_for_stage_record(
    registry: Mapping[str, Any],
    record: Mapping[str, Any],
) -> str | None:
    """Resolve a stage record by durable case or problem aliases."""

    explicit = _clean_string(record.get("case_id"))
    if explicit is not None:
        explicit = _canonical_registry_case_id(registry, explicit)

    by_problem = _registry_mapping(registry, "problem_id_to_case_id")
    problem_candidates = [
        _clean_string(record.get("problem_id")),
        _clean_string(record.get("canonical_problem_id")),
    ]
    resolved_problem: str | None = None
    for problem_id in problem_candidates:
        if problem_id is None or problem_id not in by_problem:
            continue
        candidate = _canonical_registry_case_id(registry, by_problem[problem_id])
        if resolved_problem is not None and resolved_problem != candidate:
            raise ValueError(
                "update_case_registry_stage_lineage: conflicting problem aliases "
                f"for stage record ({resolved_problem}, {candidate})"
            )
        resolved_problem = candidate

    if explicit is not None and resolved_problem is not None and explicit != resolved_problem:
        raise ValueError(
            "update_case_registry_stage_lineage: case/problem identity mismatch "
            f"({explicit}, {resolved_problem})"
        )
    return explicit or resolved_problem


def _flatten_stage_artifact_refs(
    value: Any,
    *,
    prefix: str = "",
) -> list[dict[str, str]]:
    """Return compact named path references from a stage artifact mapping."""

    if isinstance(value, str) and value.strip():
        return [{"name": prefix or "artifact", "path": value.strip()}]
    if not isinstance(value, Mapping):
        return []
    refs: list[dict[str, str]] = []
    for raw_key in sorted(value, key=str):
        key = str(raw_key)
        child_prefix = f"{prefix}.{key}" if prefix else key
        refs.extend(_flatten_stage_artifact_refs(value[raw_key], prefix=child_prefix))
    return refs


_STAGE_ITEM_REFERENCE_FIELDS: tuple[str, ...] = (
    "problem_id",
    "canonical_problem_id",
    "case_id",
    "priority_bucket",
    "selected_for_research",
    "eligible_for_downstream",
    "research_route",
    "research_route_revision",
    "research_frontier_sha256",
    "research_snapshot_id",
    "reconsider_when",
    "research_schema_version",
    "repo_revision",
    "research_status",
    "reproduction_status",
    "option_id",
    "family_id",
    "optioning_status",
    "selected_option_id",
    "selected_family_id",
    "selection_status",
    "falsification_verdict",
    "change_plan_id",
    "plan_revision_id",
    "plan_revision_source",
    "planning_status",
    "change_plan_status",
    "ticket_fingerprint",
    "ticket_stage",
)


def _compact_stage_item_reference(record: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only stable identifiers and machine statuses into an artifact reference."""

    compact: dict[str, Any] = {}
    for field in _STAGE_ITEM_REFERENCE_FIELDS:
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            compact[field] = value.strip()
        elif isinstance(value, bool) or (
            isinstance(value, (int, float)) and not isinstance(value, bool)
        ):
            compact[field] = value
    warning = _clean_string(record.get("_parse_warning"))
    if warning is not None:
        compact["_parse_warning"] = warning
    return compact


def _stage_auxiliary_records(stage_doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Collect per-case stage outcomes that are intentionally not stage items."""

    raw_meta = stage_doc.get("input_meta")
    meta = raw_meta if isinstance(raw_meta, Mapping) else {}
    auxiliary: list[dict[str, Any]] = []
    for raw_key, raw_value in meta.items():
        key = str(raw_key)
        if key != "rejected_plans" and not key.endswith("_outcomes"):
            continue
        if not isinstance(raw_value, list):
            continue
        auxiliary.extend(dict(item) for item in raw_value if isinstance(item, Mapping))
    return auxiliary


def _stage_snapshot_reference(
    *,
    stage: str,
    generated_at: str | None,
    artifact_refs: Sequence[dict[str, str]],
    records: Sequence[Mapping[str, Any]],
    auxiliary_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a content-addressed pointer to one case's stage artifact revision."""

    def _snapshot_record(record: Mapping[str, Any]) -> dict[str, Any]:
        # Carry-forward context points at earlier stage snapshots.  Hashing it would
        # create a self-referential new revision every cycle even when stage output is
        # otherwise unchanged.
        return {
            str(key): value
            for key, value in record.items()
            if key not in {"prior_stage_context", "_historical_case_context"}
        }

    snapshot_blob = json.dumps(
        {
            "stage": stage,
            "records": [_snapshot_record(record) for record in records],
            "auxiliary_records": [_snapshot_record(record) for record in auxiliary_records],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    reference: dict[str, Any] = {
        "stage": stage,
        "stage_snapshot_id": f"stagesnap:{sha256(snapshot_blob).hexdigest()[:24]}",
        "artifact_refs": [dict(item) for item in artifact_refs],
        "item_refs": [
            _compact_stage_item_reference(record) for record in [*records, *auxiliary_records]
        ],
    }
    if generated_at is not None:
        reference["recorded_at"] = generated_at
    return reference


def _append_snapshot_history(
    entry: dict[str, Any],
    *,
    history_field: str,
    current_field: str,
    snapshot: Mapping[str, Any],
) -> None:
    """Append a distinct content-addressed snapshot and update its current pointer."""

    snapshot_copy = deepcopy(dict(snapshot))
    snapshot_id = _clean_string(snapshot.get("stage_snapshot_id"))
    raw_history = entry.get(history_field)
    history = (
        [deepcopy(dict(item)) for item in raw_history if isinstance(item, Mapping)]
        if isinstance(raw_history, list)
        else []
    )
    if snapshot_id is None or not any(
        _clean_string(item.get("stage_snapshot_id")) == snapshot_id for item in history
    ):
        history.append(snapshot_copy)
    entry[history_field] = history
    entry[current_field] = snapshot_copy


def _material_unknown_summary(value: Any) -> list[dict[str, Any]]:
    """Retain the decision-relevant portion of material research unknowns."""

    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw_unknown in value:
        if not isinstance(raw_unknown, Mapping):
            continue
        unknown = _clean_string(raw_unknown.get("unknown"))
        evidence_needed = _clean_string(raw_unknown.get("evidence_needed"))
        affects = _clean_string_list(raw_unknown.get("affects"))
        if unknown is None and evidence_needed is None and not affects:
            continue
        summary: dict[str, Any] = {"affects": affects}
        if unknown is not None:
            summary["unknown"] = unknown
        if evidence_needed is not None:
            summary["evidence_needed"] = evidence_needed
        result.append(summary)
    return result


def _research_root_cause_status(record: Mapping[str, Any]) -> str:
    """Derive a compact root-cause state from the validated research outcome."""

    explicit = _clean_string(record.get("root_cause_status"))
    if explicit is not None:
        return explicit
    research_status = _clean_string(record.get("research_status"))
    if research_status == "evidence_sufficient":
        return "established"
    if research_status == "blocked":
        return "blocked"
    hypotheses = record.get("root_cause_hypotheses")
    if isinstance(hypotheses, list) and hypotheses:
        return "hypothesized"
    return "unestablished"


def _canonical_content_sha256(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def downstream_chain_input_sha256(
    *,
    stage: str,
    case_id: str | None,
    case_revision: int,
    source_evidence_atom_ids: Sequence[str],
    research_dossier_sha256: str | None,
    source_evidence_snapshot_sha256: str | None = None,
    option_records_sha256: str | None = None,
    selection_records_sha256: str | None = None,
) -> str:
    """Bind a downstream artifact to the exact causal inputs it consumed."""

    return _canonical_content_sha256(
        {
            "contract_revision": DOWNSTREAM_CHAIN_CONTRACT_REVISION,
            "stage": stage,
            "case_id": case_id,
            "case_revision": max(1, int(case_revision or 1)),
            "source_evidence_atom_ids": sorted(
                {
                    value.strip()
                    for value in source_evidence_atom_ids
                    if isinstance(value, str) and value.strip()
                }
            ),
            "source_evidence_snapshot_sha256": source_evidence_snapshot_sha256,
            "research_dossier_sha256": research_dossier_sha256,
            "option_records_sha256": option_records_sha256,
            "selection_records_sha256": selection_records_sha256,
        }
    )


def _exact_stage_json_ref(
    snapshot: Mapping[str, Any],
    *,
    allowed_names: set[str],
) -> dict[str, str] | None:
    refs = [
        deepcopy(dict(ref))
        for ref in (
            snapshot.get("artifact_refs") if isinstance(snapshot.get("artifact_refs"), list) else []
        )
        if isinstance(ref, Mapping)
        and _clean_string(ref.get("path")) is not None
        and _clean_string(ref.get("name")) in allowed_names
    ]
    return refs[0] if len(refs) == 1 else None


def _verified_research_mechanism_summary(
    record: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Copy mechanism identity only from a complete runner-owned research receipt."""

    verification_raw = record.get("evidence_verification")
    if not isinstance(verification_raw, Mapping):
        return None
    verification = dict(verification_raw)
    projection = verification.get("verified_mechanism")
    digest = _clean_string(verification.get("verified_mechanism_sha256"))
    provenance = verification.get("verified_mechanism_provenance")
    provenance_digest = _clean_string(verification.get("verified_mechanism_provenance_sha256"))
    receipt_digest = _clean_string(verification.get("receipt_sha256"))
    errors = verification.get("errors")
    if (
        verification.get("status") != "verified"
        or not isinstance(projection, Mapping)
        or not isinstance(provenance, Mapping)
        or not isinstance(errors, list)
        or errors
        or digest is None
        or provenance_digest is None
        or receipt_digest is None
        or receipt_digest
        != _canonical_content_sha256(
            {key: value for key, value in verification.items() if key != "receipt_sha256"}
        )
        or digest != _canonical_content_sha256(projection)
        or provenance_digest != _canonical_content_sha256(provenance)
        or len(receipt_digest) != 64
        or any(character not in "0123456789abcdef" for character in receipt_digest.casefold())
    ):
        return None
    # The full stage contract validates the receipt's self hash and reconstructs the
    # projection from runner-minted mechanism/control/intervention receipts.  This
    # summary deliberately ignores any same-named top-level model fields.
    return {
        "verified_mechanism": deepcopy(dict(projection)),
        "verified_mechanism_sha256": digest.casefold(),
        "verified_mechanism_provenance": deepcopy(dict(provenance)),
        "verified_mechanism_provenance_sha256": provenance_digest.casefold(),
        "verified_mechanism_receipt_sha256": receipt_digest.casefold(),
        "verified_mechanism_source": "runner_research_evidence_verification_v1",
    }


def _research_proof_summary(
    record: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a bounded proof summary that points back to the full research artifact."""

    research_json_refs = [
        deepcopy(dict(ref))
        for ref in (
            snapshot.get("artifact_refs") if isinstance(snapshot.get("artifact_refs"), list) else []
        )
        if isinstance(ref, Mapping)
        and _clean_string(ref.get("path")) is not None
        and _clean_string(ref.get("name")) in {"research_json", "repro_research_json"}
    ]
    summary: dict[str, Any] = {
        "stage_snapshot_id": snapshot.get("stage_snapshot_id"),
        "artifact_refs": deepcopy(snapshot.get("artifact_refs", [])),
        # The registry intentionally retains only a digest and an exact pointer.  The
        # complete proof remains in the immutable stage artifact and must be re-read and
        # revalidated before it can advance in a later cycle.
        "full_dossier_sha256": _canonical_content_sha256(record),
        "research_stage_artifact_ref": (
            research_json_refs[0] if len(research_json_refs) == 1 else None
        ),
        "research_schema_version": record.get("research_schema_version"),
        "case_id": _clean_string(record.get("case_id")),
        "problem_id": _clean_string(record.get("problem_id")),
        "case_revision": max(1, int(entry.get("case_revision") or 1)),
        "source_evidence_snapshot_sha256": _clean_string(
            entry.get("source_evidence_snapshot_sha256")
        ),
        "repo_revision": _clean_string(record.get("repo_revision")),
        "research_method": _clean_string(record.get("research_method")),
        "reproduction_status": _clean_string(record.get("reproduction_status")),
        "research_status": _clean_string(record.get("research_status")),
        "root_cause_status": _research_root_cause_status(record),
        "root_cause_confidence": _confidence_value(
            record.get("root_cause_confidence"), default=0.0
        ),
        "material_unknown_summary": _material_unknown_summary(record.get("material_unknowns")),
        "blocking_reasons": _clean_string_list(record.get("blocking_reasons")),
        "source_artifact_refs": deepcopy(record.get("artifact_refs", []))
        if isinstance(record.get("artifact_refs"), list)
        else [],
    }
    actionability_raw = record.get("actionability_assessment")
    if isinstance(actionability_raw, Mapping):
        summary["actionability_assessment"] = deepcopy(dict(actionability_raw))
    hypotheses = record.get("root_cause_hypotheses")
    if isinstance(hypotheses, list):
        summary["root_cause_hypothesis_ids"] = [
            hypothesis_id
            for hypothesis in hypotheses
            if isinstance(hypothesis, Mapping)
            for hypothesis_id in [_clean_string(hypothesis.get("hypothesis_id"))]
            if hypothesis_id is not None
        ]
    mechanism_summary = _verified_research_mechanism_summary(record)
    if mechanism_summary is not None:
        summary.update(mechanism_summary)
    warning = _clean_string(record.get("_parse_warning"))
    if warning is not None:
        summary["_parse_warning"] = warning
    return summary


def _research_proof_rank(summary: Mapping[str, Any]) -> tuple[int, int, float]:
    status_rank = {
        "blocked": 0,
        "insufficient_evidence": 1,
        "evidence_sufficient": 2,
    }.get(_clean_string(summary.get("research_status")) or "", -1)
    reproduction_rank = int(_clean_string(summary.get("reproduction_status")) == "reproduced")
    return (
        status_rank,
        reproduction_rank,
        _confidence_value(summary.get("root_cause_confidence"), default=0.0),
    )


def _merge_snapshot_lists(current: Any, absorbed: Any) -> list[dict[str, Any]]:
    """Union content-addressed summary histories without discarding either case."""

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_item in [
        *(current if isinstance(current, list) else []),
        *(absorbed if isinstance(absorbed, list) else []),
    ]:
        if not isinstance(raw_item, Mapping):
            continue
        item = deepcopy(dict(raw_item))
        snapshot_id = _clean_string(item.get("stage_snapshot_id"))
        if snapshot_id is None:
            snapshot_id = sha256(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
        if snapshot_id in seen:
            continue
        seen.add(snapshot_id)
        merged.append(item)
    return merged


def _merge_case_stage_lineage_entries(
    canonical: Mapping[str, Any], absorbed: Mapping[str, Any]
) -> dict[str, Any]:
    """Carry an absorbed case's historical stage graph onto its canonical case."""

    merged = deepcopy(dict(canonical))
    for history_field in (
        "research_proof_history",
        "option_set_history",
        "selection_history",
        "planning_history",
    ):
        history = _merge_snapshot_lists(merged.get(history_field), absorbed.get(history_field))
        if history:
            merged[history_field] = history

    current_stage_raw = merged.get("stage_artifact_refs")
    current_stages = dict(current_stage_raw) if isinstance(current_stage_raw, Mapping) else {}
    absorbed_stages_raw = absorbed.get("stage_artifact_refs")
    if isinstance(absorbed_stages_raw, Mapping):
        for raw_stage, absorbed_history in absorbed_stages_raw.items():
            stage = str(raw_stage)
            current_stages[stage] = _merge_snapshot_lists(
                current_stages.get(stage), absorbed_history
            )
    if current_stages:
        merged["stage_artifact_refs"] = current_stages

    absorbed_current_refs = absorbed.get("current_stage_artifact_refs")
    if isinstance(absorbed_current_refs, Mapping):
        current_refs_raw = merged.get("current_stage_artifact_refs")
        current_refs = (
            deepcopy(dict(current_refs_raw)) if isinstance(current_refs_raw, Mapping) else {}
        )
        merged["current_stage_artifact_refs"] = {
            **deepcopy(dict(absorbed_current_refs)),
            **current_refs,
        }

    for mapping_field in ("plan_revisions", "ticket_records"):
        absorbed_mapping = absorbed.get(mapping_field)
        if not isinstance(absorbed_mapping, Mapping):
            continue
        current_mapping_raw = merged.get(mapping_field)
        current_mapping = (
            deepcopy(dict(current_mapping_raw)) if isinstance(current_mapping_raw, Mapping) else {}
        )
        # Canonical revisions win on an identical durable identifier.
        merged_mapping = {**deepcopy(dict(absorbed_mapping)), **current_mapping}
        merged[mapping_field] = merged_mapping

    for list_field in (
        "evidence_atom_ids",
        "source_evidence_atom_ids",
        "derived_evidence_atom_ids",
        "plan_revision_ids",
        "ticket_fingerprints",
    ):
        values = list(
            dict.fromkeys(
                _clean_string_list(absorbed.get(list_field))
                + _clean_string_list(merged.get(list_field))
            )
        )
        if values:
            merged[list_field] = values

    for current_field in (
        "current_research_proof",
        "best_research_proof",
        "current_option_set",
        "current_selection",
        "current_planning",
        "current_lifecycle",
    ):
        if not isinstance(merged.get(current_field), Mapping) and isinstance(
            absorbed.get(current_field), Mapping
        ):
            merged[current_field] = deepcopy(dict(absorbed[current_field]))

    canonical_best = merged.get("best_research_proof")
    absorbed_best = absorbed.get("best_research_proof")
    if isinstance(absorbed_best, Mapping) and (
        not isinstance(canonical_best, Mapping)
        or _research_proof_rank(absorbed_best) > _research_proof_rank(canonical_best)
    ):
        merged["best_research_proof"] = deepcopy(dict(absorbed_best))
        merged["root_cause_status"] = _clean_string(
            absorbed_best.get("root_cause_status")
        ) or merged.get("root_cause_status")
        merged["root_cause_confidence"] = _confidence_value(
            absorbed_best.get("root_cause_confidence"), default=0.0
        )
        for field in (
            "verified_mechanism",
            "verified_mechanism_sha256",
            "verified_mechanism_provenance",
            "verified_mechanism_provenance_sha256",
            "verified_mechanism_receipt_sha256",
            "verified_mechanism_source",
        ):
            if field in absorbed_best:
                merged[field] = deepcopy(absorbed_best[field])
    return merged


def _update_research_stage_summary(
    entry: dict[str, Any],
    records: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
) -> None:
    if not records:
        return
    record = records[-1]
    summary = _research_proof_summary(record, snapshot, entry=entry)
    _append_snapshot_history(
        entry,
        history_field="research_proof_history",
        current_field="current_research_proof",
        snapshot=summary,
    )
    previous_best_raw = entry.get("best_research_proof")
    previous_best = dict(previous_best_raw) if isinstance(previous_best_raw, Mapping) else None
    best = summary
    if previous_best is not None and _research_proof_rank(previous_best) > _research_proof_rank(
        summary
    ):
        best = previous_best
    entry["best_research_proof"] = deepcopy(best)
    entry["root_cause_status"] = best["root_cause_status"]
    entry["root_cause_confidence"] = best["root_cause_confidence"]
    entry["material_unknown_summary"] = deepcopy(summary["material_unknown_summary"])
    for field in (
        "verified_mechanism",
        "verified_mechanism_sha256",
        "verified_mechanism_provenance",
        "verified_mechanism_provenance_sha256",
        "verified_mechanism_receipt_sha256",
        "verified_mechanism_source",
    ):
        if field in best:
            entry[field] = deepcopy(best[field])


def _update_optioning_stage_summary(
    entry: dict[str, Any],
    records: Sequence[Mapping[str, Any]],
    auxiliary_records: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
) -> None:
    outcome = auxiliary_records[-1] if auxiliary_records else {}
    status = _clean_string(outcome.get("optioning_status"))
    if status is None:
        status = "options_produced" if records else "not_produced"
    current_research_raw = entry.get("current_research_proof")
    current_research = current_research_raw if isinstance(current_research_raw, Mapping) else {}
    option_records_sha256 = _canonical_content_sha256([dict(record) for record in records])
    source_evidence_atom_ids = _clean_string_list(entry.get("source_evidence_atom_ids"))
    summary: dict[str, Any] = {
        "stage_snapshot_id": snapshot.get("stage_snapshot_id"),
        "artifact_refs": deepcopy(snapshot.get("artifact_refs", [])),
        "downstream_contract_revision": DOWNSTREAM_CHAIN_CONTRACT_REVISION,
        "full_records_sha256": option_records_sha256,
        "stage_artifact_ref": _exact_stage_json_ref(
            snapshot,
            allowed_names={"solution_options_json"},
        ),
        "input_chain_sha256": downstream_chain_input_sha256(
            stage="solution_optioning",
            case_id=_clean_string(entry.get("case_id")),
            case_revision=max(1, int(entry.get("case_revision") or 1)),
            source_evidence_atom_ids=source_evidence_atom_ids,
            source_evidence_snapshot_sha256=_clean_string(
                entry.get("source_evidence_snapshot_sha256")
            ),
            research_dossier_sha256=_clean_string(current_research.get("full_dossier_sha256")),
        ),
        "case_id": _clean_string(entry.get("case_id")),
        "problem_id": (
            _clean_string(records[0].get("problem_id"))
            if records
            else _clean_string(outcome.get("problem_id"))
        ),
        "optioning_status": status,
        "option_ids": [
            option_id
            for record in records
            for option_id in [_clean_string(record.get("option_id"))]
            if option_id is not None
        ],
        "family_ids": list(
            dict.fromkeys(
                family_id
                for record in records
                for family_id in [_clean_string(record.get("family_id"))]
                if family_id is not None
            )
        ),
    }
    summary["optioning_outcome_count"] = len(auxiliary_records)
    if len(auxiliary_records) == 1:
        # A zero-option outcome is a real Stage-4 disposition, not an option record.
        # Retain its exact identity so later cycles can authenticate the full artifact
        # rather than treating every empty option set as a cache miss.
        summary["optioning_outcome_sha256"] = _canonical_content_sha256(outcome)
    actionability_disposition = _clean_string(
        outcome.get("research_actionability_disposition")
    )
    if actionability_disposition is not None:
        summary["research_actionability_disposition"] = actionability_disposition
    evidence_refs = _clean_string_list(outcome.get("evidence_refs"))
    if evidence_refs:
        summary["actionability_evidence_refs"] = evidence_refs
    blockers = _clean_string_list(outcome.get("research_readiness_blockers"))
    if blockers:
        summary["research_readiness_blockers"] = blockers
    warnings = [
        warning
        for record in records
        for warning in [_clean_string(record.get("_parse_warning"))]
        if warning is not None
    ]
    if warnings:
        summary["parse_warnings"] = warnings
    _append_snapshot_history(
        entry,
        history_field="option_set_history",
        current_field="current_option_set",
        snapshot=summary,
    )


def _selection_summary(
    record: Mapping[str, Any],
    outcome: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    review_raw = record.get("falsification_review")
    review = review_raw if isinstance(review_raw, Mapping) else {}
    material_risks = _clean_string_list(record.get("material_risks"))
    selected_option_raw = record.get("selected_option")
    selected_option = selected_option_raw if isinstance(selected_option_raw, Mapping) else {}
    coverage_raw = selected_option.get("causal_coverage")
    coverage = coverage_raw if isinstance(coverage_raw, Mapping) else {}
    for risk_field in (
        "unsupported_assumptions",
        "residual_recurrence_paths",
        "compatibility_risks",
    ):
        material_risks.extend(_clean_string_list(coverage.get(risk_field)))
    current_research_raw = entry.get("current_research_proof")
    current_research = current_research_raw if isinstance(current_research_raw, Mapping) else {}
    current_options_raw = entry.get("current_option_set")
    current_options = current_options_raw if isinstance(current_options_raw, Mapping) else {}
    selection_records = [dict(record)] if record else []
    summary: dict[str, Any] = {
        "stage_snapshot_id": snapshot.get("stage_snapshot_id"),
        "artifact_refs": deepcopy(snapshot.get("artifact_refs", [])),
        "downstream_contract_revision": DOWNSTREAM_CHAIN_CONTRACT_REVISION,
        "full_records_sha256": _canonical_content_sha256(selection_records),
        "stage_artifact_ref": _exact_stage_json_ref(
            snapshot,
            allowed_names={"solution_selection_json"},
        ),
        "input_chain_sha256": downstream_chain_input_sha256(
            stage="solution_selection",
            case_id=_clean_string(entry.get("case_id")),
            case_revision=max(1, int(entry.get("case_revision") or 1)),
            source_evidence_atom_ids=_clean_string_list(entry.get("source_evidence_atom_ids")),
            source_evidence_snapshot_sha256=_clean_string(
                entry.get("source_evidence_snapshot_sha256")
            ),
            research_dossier_sha256=_clean_string(current_research.get("full_dossier_sha256")),
            option_records_sha256=_clean_string(current_options.get("full_records_sha256")),
        ),
        "problem_id": _clean_string(record.get("problem_id"))
        or _clean_string(outcome.get("problem_id")),
        "selection_status": _clean_string(outcome.get("selection_status"))
        or _clean_string(record.get("selection_status"))
        or "selected",
        "selected_option_id": _clean_string(record.get("selected_option_id"))
        or _clean_string(outcome.get("selected_option_id")),
        "selected_family_id": _clean_string(record.get("selected_family_id")),
        "falsification_status": _clean_string(review.get("verdict"))
        or _clean_string(outcome.get("falsification_verdict"))
        or "not_recorded",
        "falsification_evidence_refs": deepcopy(review.get("evidence_refs", []))
        if isinstance(review.get("evidence_refs"), list)
        else [],
        "material_risks": list(dict.fromkeys(material_risks)),
        "material_risk_dispositions": deepcopy(review.get("material_risk_dispositions", []))
        if isinstance(review.get("material_risk_dispositions"), list)
        else [],
    }
    warning = _clean_string(record.get("_parse_warning"))
    if warning is not None:
        summary["_parse_warning"] = warning
    return summary


def _update_selection_stage_summary(
    entry: dict[str, Any],
    records: Sequence[Mapping[str, Any]],
    auxiliary_records: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
) -> None:
    record = records[-1] if records else {}
    outcome = auxiliary_records[-1] if auxiliary_records else {}
    summary = _selection_summary(record, outcome, snapshot, entry=entry)
    _append_snapshot_history(
        entry,
        history_field="selection_history",
        current_field="current_selection",
        snapshot=summary,
    )
    selected_option_id = _clean_string(summary.get("selected_option_id"))
    if selected_option_id is not None:
        entry["selected_option_id"] = selected_option_id
    selected_family_id = _clean_string(summary.get("selected_family_id"))
    if selected_family_id is not None:
        entry["selected_family_id"] = selected_family_id
    entry["falsification_status"] = summary["falsification_status"]


def _update_planning_stage_summary(
    entry: dict[str, Any],
    records: Sequence[Mapping[str, Any]],
    auxiliary_records: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
) -> None:
    raw_revisions = entry.get("plan_revisions")
    revisions = dict(raw_revisions) if isinstance(raw_revisions, Mapping) else {}
    current_revision_ids: list[str] = []
    for record in records:
        revision_id = _clean_string(record.get("plan_revision_id"))
        if revision_id is None:
            continue
        revision_digest = revision_id.removeprefix("planrev:sha256:")
        if (
            not revision_id.startswith("planrev:sha256:")
            or len(revision_digest) != 64
            or any(character not in "0123456789abcdef" for character in revision_digest)
        ):
            raise ValueError(
                "update_case_registry_stage_lineage: invalid content-addressed "
                f"plan_revision_id {revision_id!r}"
            )
        revision_source = _clean_string(record.get("plan_revision_source"))
        if revision_source != "server_content_addressed_v1":
            raise ValueError(
                "update_case_registry_stage_lineage: invalid plan_revision_source "
                f"for {revision_id!r}: {revision_source!r}"
            )
        expected_revision_id = plan_revision_id_for(record)
        if revision_id != expected_revision_id:
            raise ValueError(
                "update_case_registry_stage_lineage: plan revision content mismatch "
                f"expected={expected_revision_id!r} got={revision_id!r}"
            )
        current_revision_ids.append(revision_id)
        prior_revision_raw = revisions.get(revision_id)
        prior_revision = prior_revision_raw if isinstance(prior_revision_raw, Mapping) else {}
        evidence_at_plan = _clean_string_list(entry.get("evidence_atom_ids"))
        derived_at_plan = set(_clean_string_list(entry.get("derived_evidence_atom_ids")))
        revision: dict[str, Any] = {
            "plan_revision_id": revision_id,
            "plan_revision_source": revision_source,
            "stage_snapshot_id": snapshot.get("stage_snapshot_id"),
            "artifact_refs": deepcopy(snapshot.get("artifact_refs", [])),
            "change_plan_id": _clean_string(record.get("change_plan_id")),
            "problem_id": _clean_string(record.get("problem_id")),
            "selected_option_id": _clean_string(record.get("selected_option_id")),
            "repo_revision": _clean_string(record.get("repo_revision")),
            "change_plan_status": _clean_string(record.get("change_plan_status")) or "planned",
            # This immutable case/evidence baseline lets later, fresh shadow cycles
            # distinguish "no recurrence" from a stable graph that already contains
            # evidence discovered after this exact plan was authored.
            "case_revision_at_plan": max(
                1,
                int(prior_revision.get("case_revision_at_plan") or entry.get("case_revision") or 1),
            ),
            "evidence_atom_ids_at_plan": _clean_string_list(
                prior_revision.get("evidence_atom_ids_at_plan")
            )
            or evidence_at_plan,
            "source_evidence_atom_ids_at_plan": _clean_string_list(
                prior_revision.get("source_evidence_atom_ids_at_plan")
            )
            or _clean_string_list(entry.get("source_evidence_atom_ids"))
            or [atom_id for atom_id in evidence_at_plan if atom_id not in derived_at_plan],
        }
        warning = _clean_string(record.get("_parse_warning"))
        if warning is not None:
            revision["_parse_warning"] = warning
        revisions[revision_id] = revision
    entry["plan_revisions"] = revisions
    entry["plan_revision_ids"] = sorted(revisions)
    entry["current_plan_revision_ids"] = list(dict.fromkeys(current_revision_ids))
    current_research_raw = entry.get("current_research_proof")
    current_research = current_research_raw if isinstance(current_research_raw, Mapping) else {}
    current_options_raw = entry.get("current_option_set")
    current_options = current_options_raw if isinstance(current_options_raw, Mapping) else {}
    current_selection_raw = entry.get("current_selection")
    current_selection = current_selection_raw if isinstance(current_selection_raw, Mapping) else {}
    planning_summary = {
        "stage_snapshot_id": snapshot.get("stage_snapshot_id"),
        "artifact_refs": deepcopy(snapshot.get("artifact_refs", [])),
        "downstream_contract_revision": DOWNSTREAM_CHAIN_CONTRACT_REVISION,
        "full_records_sha256": _canonical_content_sha256([dict(record) for record in records]),
        "stage_artifact_ref": _exact_stage_json_ref(
            snapshot,
            allowed_names={"change_plans_json"},
        ),
        "input_chain_sha256": downstream_chain_input_sha256(
            stage="implementation_planning",
            case_id=_clean_string(entry.get("case_id")),
            case_revision=max(1, int(entry.get("case_revision") or 1)),
            source_evidence_atom_ids=_clean_string_list(entry.get("source_evidence_atom_ids")),
            source_evidence_snapshot_sha256=_clean_string(
                entry.get("source_evidence_snapshot_sha256")
            ),
            research_dossier_sha256=_clean_string(current_research.get("full_dossier_sha256")),
            option_records_sha256=_clean_string(current_options.get("full_records_sha256")),
            selection_records_sha256=_clean_string(current_selection.get("full_records_sha256")),
        ),
        "problem_id": _clean_string(records[0].get("problem_id")) if records else None,
        "plan_revision_ids": list(dict.fromkeys(current_revision_ids)),
        "rejected_plans": [_compact_stage_item_reference(record) for record in auxiliary_records],
    }
    _append_snapshot_history(
        entry,
        history_field="planning_history",
        current_field="current_planning",
        snapshot=planning_summary,
    )
    if current_revision_ids and (_clean_string(entry.get("state")) or "active") == "active":
        entry["state"] = "planned"
        entry["current_lifecycle"] = {
            "state": "planned",
            "outcome_reference": {
                "source": "validated_change_plan_artifact",
                "validation_status": "stage_validated",
                "stage_snapshot_id": snapshot.get("stage_snapshot_id"),
                "plan_revision_ids": list(dict.fromkeys(current_revision_ids)),
            },
        }


def _update_ticket_stage_summary(
    registry: dict[str, Any],
    entry: dict[str, Any],
    records: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
) -> None:
    raw_ticket_records = entry.get("ticket_records")
    ticket_records = dict(raw_ticket_records) if isinstance(raw_ticket_records, Mapping) else {}
    current_fingerprints: list[str] = []
    fingerprint_map = registry.get("ticket_fingerprint_to_case_id")
    if not isinstance(fingerprint_map, dict):
        fingerprint_map = {}
        registry["ticket_fingerprint_to_case_id"] = fingerprint_map
    case_id = _clean_string(entry.get("case_id"))
    for record in records:
        fingerprint = _clean_string(record.get("ticket_fingerprint"))
        if fingerprint is None:
            continue
        current_fingerprints.append(fingerprint)
        ticket_records[fingerprint] = {
            "ticket_fingerprint": fingerprint,
            "stage_snapshot_id": snapshot.get("stage_snapshot_id"),
            "artifact_refs": deepcopy(snapshot.get("artifact_refs", [])),
            "plan_revision_id": _clean_string(record.get("plan_revision_id")),
            "ticket_stage": _clean_string(record.get("ticket_stage")),
        }
        if case_id is not None:
            fingerprint_map[fingerprint] = case_id
    entry["ticket_records"] = ticket_records
    entry["ticket_fingerprints"] = sorted(ticket_records)
    entry["current_ticket_fingerprints"] = list(dict.fromkeys(current_fingerprints))


def update_case_registry_stage_lineage(
    registry: Mapping[str, Any],
    *,
    stage_doc: Mapping[str, Any],
    strict: bool = True,
) -> dict[str, Any]:
    """Persist compact cumulative lineage for one completed pipeline stage.

    The stage artifact remains the source of full prose and evidence.  Registry entries
    store content-addressed pointers plus the small validated summaries needed to carry a
    case forward without discarding proof context or deriving identity from generated text.
    """

    if registry.get("schema_version") != CASE_REGISTRY_SCHEMA_VERSION:
        raise ValueError("update_case_registry_stage_lineage: unsupported case registry")
    stage = _clean_string(stage_doc.get("stage"))
    if stage is None:
        raise ValueError("update_case_registry_stage_lineage: stage_doc missing stage")

    updated = deepcopy(dict(registry))
    raw_cases = updated.get("cases")
    cases = raw_cases if isinstance(raw_cases, dict) else {}
    updated["cases"] = cases

    raw_items = stage_doc.get("items")
    items = (
        [dict(item) for item in raw_items if isinstance(item, Mapping)]
        if isinstance(raw_items, list)
        else []
    )
    auxiliary = _stage_auxiliary_records(stage_doc)
    records_by_case: dict[str, list[dict[str, Any]]] = {}
    auxiliary_by_case: dict[str, list[dict[str, Any]]] = {}

    for record, destination in [
        *((record, records_by_case) for record in items),
        *((record, auxiliary_by_case) for record in auxiliary),
    ]:
        case_id = _case_id_for_stage_record(updated, record)
        if case_id is None:
            if strict:
                raise ValueError(
                    "update_case_registry_stage_lineage: stage record has no known case "
                    f"identity: stage={stage} record={_compact_stage_item_reference(record)!r}"
                )
            continue
        if case_id not in cases:
            if strict:
                raise ValueError(
                    "update_case_registry_stage_lineage: stage record resolves to unknown "
                    f"case {case_id!r}"
                )
            continue
        destination.setdefault(case_id, []).append(record)

    raw_artifacts = stage_doc.get("artifacts")
    artifact_refs = _flatten_stage_artifact_refs(raw_artifacts)
    generated_at = _clean_string(stage_doc.get("generated_at"))
    touched_case_ids = sorted(set(records_by_case) | set(auxiliary_by_case))
    for case_id in touched_case_ids:
        raw_entry = cases.get(case_id)
        if not isinstance(raw_entry, Mapping):
            continue
        entry = deepcopy(dict(raw_entry))
        records = records_by_case.get(case_id, [])
        auxiliary_records = auxiliary_by_case.get(case_id, [])
        snapshot = _stage_snapshot_reference(
            stage=stage,
            generated_at=generated_at,
            artifact_refs=artifact_refs,
            records=records,
            auxiliary_records=auxiliary_records,
        )

        raw_stage_history = entry.get("stage_artifact_refs")
        stage_history = (
            {
                str(key): [deepcopy(dict(item)) for item in value if isinstance(item, Mapping)]
                for key, value in raw_stage_history.items()
                if isinstance(value, list)
            }
            if isinstance(raw_stage_history, Mapping)
            else {}
        )
        history = stage_history.setdefault(stage, [])
        snapshot_id = _clean_string(snapshot.get("stage_snapshot_id"))
        if not any(_clean_string(item.get("stage_snapshot_id")) == snapshot_id for item in history):
            history.append(deepcopy(snapshot))
        entry["stage_artifact_refs"] = stage_history

        raw_current = entry.get("current_stage_artifact_refs")
        current = dict(raw_current) if isinstance(raw_current, Mapping) else {}
        current[stage] = deepcopy(snapshot)
        entry["current_stage_artifact_refs"] = current
        entry["stage_lineage_schema_version"] = 1
        entry["last_pipeline_stage"] = stage

        if stage == "repro_research":
            _update_research_stage_summary(entry, records, snapshot)
        elif stage == "solution_optioning":
            _update_optioning_stage_summary(entry, records, auxiliary_records, snapshot)
        elif stage == "solution_selection":
            _update_selection_stage_summary(entry, records, auxiliary_records, snapshot)
        elif stage == "implementation_planning":
            _update_planning_stage_summary(entry, records, auxiliary_records, snapshot)
        elif stage == "ticket_assembly":
            _update_ticket_stage_summary(updated, entry, records, snapshot)

        cases[case_id] = entry

    return updated


def verified_mechanism_identities_from_case_registry(
    registry: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Return only runner-persisted, content-addressed mechanism identities."""

    if not isinstance(registry, Mapping):
        return {}
    cases_raw = registry.get("cases")
    if not isinstance(cases_raw, Mapping):
        return {}
    identities: dict[str, str] = {}
    for raw_case_id, raw_entry in cases_raw.items():
        if not isinstance(raw_entry, Mapping):
            continue
        case_id = _clean_string(raw_entry.get("case_id")) or _clean_string(raw_case_id)
        projection = raw_entry.get("verified_mechanism")
        digest = _clean_string(raw_entry.get("verified_mechanism_sha256"))
        provenance = raw_entry.get("verified_mechanism_provenance")
        provenance_digest = _clean_string(raw_entry.get("verified_mechanism_provenance_sha256"))
        receipt_digest = _clean_string(raw_entry.get("verified_mechanism_receipt_sha256"))
        if (
            case_id is None
            or raw_entry.get("verified_mechanism_source")
            != "runner_research_evidence_verification_v1"
            or raw_entry.get("root_cause_status") != "established"
            or not isinstance(projection, Mapping)
            or not isinstance(provenance, Mapping)
            or digest is None
            or digest != _canonical_content_sha256(projection)
            or provenance_digest is None
            or provenance_digest != _canonical_content_sha256(provenance)
            or receipt_digest is None
            or len(receipt_digest) != 64
            or any(character not in "0123456789abcdef" for character in receipt_digest.casefold())
        ):
            continue
        identities[case_id] = digest.casefold()
    return identities


def verified_causal_identities_from_case_registry(
    registry: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Return runner-persisted full causal identities, not mechanism surface hashes."""

    if not isinstance(registry, Mapping):
        return {}
    cases_raw = registry.get("cases")
    if not isinstance(cases_raw, Mapping):
        return {}
    identities: dict[str, str] = {}
    for raw_case_id, raw_entry in cases_raw.items():
        if not isinstance(raw_entry, Mapping):
            continue
        case_id = _clean_string(raw_entry.get("case_id")) or _clean_string(raw_case_id)
        digest = _clean_string(raw_entry.get("verified_causal_signature_sha256"))
        if (
            case_id is None
            or raw_entry.get("verified_causal_signature_source")
            != "runner_verified_causal_signature_v1"
            or not _valid_sha256(digest)
            or raw_entry.get("root_cause_status") != "established"
        ):
            continue
        identities[case_id] = str(digest).casefold()
    return identities


def problem_case_records_from_registry(
    registry: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return active canonical case records suitable for relation-review context.

    Registry entries are intentionally richer than an identity lookup.  This adapter
    gives a later cycle enough problem and symptom context to decide whether new
    observational evidence updates an existing case.  Alias entries are excluded
    because their canonical target is already represented.
    """

    if not isinstance(registry, Mapping):
        return []
    raw_cases = registry.get("cases")
    if not isinstance(raw_cases, Mapping):
        return []

    records: list[dict[str, Any]] = []
    for raw_case_id in sorted(raw_cases):
        raw_entry = raw_cases.get(raw_case_id)
        if not isinstance(raw_entry, Mapping):
            continue
        case_id = _clean_string(raw_entry.get("case_id")) or _clean_string(raw_case_id)
        problem_id = _clean_string(raw_entry.get("canonical_problem_id"))
        if case_id is None or problem_id is None:
            continue
        state = _clean_string(raw_entry.get("state")) or "active"
        if (
            state in {"alias", "split", "superseded"}
            or _clean_string(raw_entry.get("alias_of")) is not None
        ):
            continue
        evidence_atom_ids = _clean_string_list(raw_entry.get("evidence_atom_ids"))
        derived_evidence_atom_ids = _clean_string_list(raw_entry.get("derived_evidence_atom_ids"))
        source_evidence_atom_ids = _clean_string_list(
            raw_entry.get("source_evidence_atom_ids")
        ) or [
            atom_id
            for atom_id in evidence_atom_ids
            if atom_id not in set(derived_evidence_atom_ids)
        ]
        record: dict[str, Any] = {
            "problem_id": problem_id,
            "canonical_problem_id": problem_id,
            "case_id": case_id,
            "case_member_problem_ids": _clean_string_list(raw_entry.get("problem_ids"))
            or [problem_id],
            "evidence_atom_ids": evidence_atom_ids,
            "source_evidence_atom_ids": source_evidence_atom_ids,
            "source_evidence_projection_version": raw_entry.get(
                "source_evidence_projection_version"
            ),
            "source_evidence_atom_sha256_by_id": deepcopy(
                dict(raw_entry.get("source_evidence_atom_sha256_by_id"))
            )
            if isinstance(raw_entry.get("source_evidence_atom_sha256_by_id"), Mapping)
            else {},
            "source_evidence_snapshot_complete": raw_entry.get("source_evidence_snapshot_complete")
            is True,
            "source_evidence_snapshot_missing_atom_ids": _clean_string_list(
                raw_entry.get("source_evidence_snapshot_missing_atom_ids")
            ),
            "source_evidence_snapshot_sha256": _clean_string(
                raw_entry.get("source_evidence_snapshot_sha256")
            ),
            "derived_evidence_atom_ids": derived_evidence_atom_ids,
            "case_revision": max(1, int(raw_entry.get("case_revision") or 1)),
            "case_state": state,
            "title": _clean_string(raw_entry.get("title")) or problem_id,
            "problem": _clean_string(raw_entry.get("problem")) or "",
            "user_impact": _clean_string(raw_entry.get("user_impact")) or "",
            "severity": _clean_string(raw_entry.get("severity")) or "medium",
            "confidence": _confidence_value(raw_entry.get("confidence"), default=0.5),
            "problem_status": _clean_string(raw_entry.get("problem_status")) or "identified",
            "evidence_summary": _clean_string(raw_entry.get("evidence_summary")) or "",
            "canonical_symptoms": _clean_string_list(raw_entry.get("canonical_symptoms")),
            "root_cause_status": _clean_string(raw_entry.get("root_cause_status"))
            or "unestablished",
            "root_cause_confidence": _confidence_value(
                raw_entry.get("root_cause_confidence"), default=0.0
            ),
            "related_case_ids": _clean_string_list(raw_entry.get("related_case_ids")),
            "_historical_case_context": True,
        }
        identity_status = _clean_string(raw_entry.get("case_identity_status"))
        if identity_status is not None:
            record["case_identity_status"] = identity_status
        identity_candidates = _clean_string_list(raw_entry.get("case_identity_candidate_ids"))
        if identity_candidates:
            record["case_identity_candidate_ids"] = identity_candidates
        provisional_group = raw_entry.get("provisional_same_cause_group")
        if isinstance(provisional_group, Mapping):
            record["provisional_same_cause_group"] = deepcopy(provisional_group)
        provisional_integrity_errors = _clean_string_list(
            raw_entry.get("provisional_same_cause_integrity_errors")
        )
        if provisional_integrity_errors:
            record["provisional_same_cause_integrity_errors"] = provisional_integrity_errors
        verified_mechanisms = verified_mechanism_identities_from_case_registry(
            {"cases": {case_id: raw_entry}}
        )
        if case_id in verified_mechanisms:
            record["verified_mechanism_sha256"] = verified_mechanisms[case_id]
            record["verified_mechanism_source"] = "runner_research_evidence_verification_v1"
        verified_causal_identities = verified_causal_identities_from_case_registry(
            {"cases": {case_id: raw_entry}}
        )
        if case_id in verified_causal_identities:
            record["verified_causal_signature_sha256"] = verified_causal_identities[case_id]
            record["verified_causal_signature_source"] = "runner_verified_causal_signature_v1"
        prior_stage_context: dict[str, Any] = {}
        current_research = raw_entry.get("current_research_proof")
        best_research = raw_entry.get("best_research_proof")
        if isinstance(current_research, Mapping):
            research_context: dict[str, Any] = {"current": deepcopy(dict(current_research))}
            if isinstance(best_research, Mapping) and best_research.get(
                "stage_snapshot_id"
            ) != current_research.get("stage_snapshot_id"):
                research_context["best_available"] = deepcopy(dict(best_research))
            prior_stage_context["research"] = research_context
        current_option_set = raw_entry.get("current_option_set")
        if isinstance(current_option_set, Mapping):
            prior_stage_context["optioning"] = deepcopy(dict(current_option_set))
        current_selection = raw_entry.get("current_selection")
        if isinstance(current_selection, Mapping):
            prior_stage_context["selection"] = deepcopy(dict(current_selection))
        current_planning = raw_entry.get("current_planning")
        if isinstance(current_planning, Mapping):
            planning_context = deepcopy(dict(current_planning))
            raw_revisions = raw_entry.get("plan_revisions")
            if isinstance(raw_revisions, Mapping):
                current_revision_ids = _clean_string_list(
                    raw_entry.get("current_plan_revision_ids")
                )
                planning_context["current_plan_revisions"] = {
                    revision_id: deepcopy(dict(raw_revisions[revision_id]))
                    for revision_id in current_revision_ids
                    if isinstance(raw_revisions.get(revision_id), Mapping)
                }
            prior_stage_context["planning"] = planning_context
        current_lifecycle = raw_entry.get("current_lifecycle")
        if isinstance(current_lifecycle, Mapping):
            prior_stage_context["lifecycle"] = deepcopy(dict(current_lifecycle))
        elif state:
            prior_stage_context["lifecycle"] = {"state": state}
        current_stage_refs = raw_entry.get("current_stage_artifact_refs")
        if isinstance(current_stage_refs, Mapping):
            prior_stage_context["artifact_refs"] = deepcopy(dict(current_stage_refs))
        if prior_stage_context:
            record["prior_stage_context"] = prior_stage_context
        selected_option_id = _clean_string(raw_entry.get("selected_option_id"))
        if selected_option_id is not None:
            record["selected_option_id"] = selected_option_id
        selected_family_id = _clean_string(raw_entry.get("selected_family_id"))
        if selected_family_id is not None:
            record["selected_family_id"] = selected_family_id
        last_pipeline_stage = _clean_string(raw_entry.get("last_pipeline_stage"))
        if last_pipeline_stage is not None:
            record["last_pipeline_stage"] = last_pipeline_stage
        group_id = _clean_string(raw_entry.get("same_cause_group_id"))
        if group_id is not None:
            record["same_cause_group_id"] = group_id
        split_parent = _clean_string(raw_entry.get("split_from_case_id"))
        if split_parent is not None:
            record["split_from_case_id"] = split_parent
        split_parent_problem = _clean_string(raw_entry.get("split_parent_problem_id"))
        if split_parent_problem is not None:
            record["split_parent_problem_id"] = split_parent_problem
        occurrence_evidence_atom_ids = _clean_string_list(
            raw_entry.get("occurrence_evidence_atom_ids")
        )
        if occurrence_evidence_atom_ids:
            record["occurrence_evidence_atom_ids"] = occurrence_evidence_atom_ids
        split_receipt = raw_entry.get("post_research_split_receipt")
        if isinstance(split_receipt, Mapping):
            record["post_research_split_receipt"] = deepcopy(dict(split_receipt))
        suggested_owner = _clean_string(raw_entry.get("suggested_owner"))
        if suggested_owner is not None:
            record["suggested_owner"] = suggested_owner
        records.append(record)
    return records


def propagate_case_lineage(
    items: Sequence[dict[str, Any]],
    problem_cases: Sequence[dict[str, Any]],
    *,
    strict_new_output: bool = True,
) -> list[dict[str, Any]]:
    """Copy canonical case fields from problem work units onto downstream stage items."""

    direct_by_problem: dict[str, dict[str, Any]] = {}
    member_by_problem: dict[str, dict[str, Any]] = {}
    for case in problem_cases:
        direct_problem_id = _clean_string(case.get("problem_id"))
        if direct_problem_id is not None:
            direct_by_problem[direct_problem_id] = case
        for problem_id in _clean_string_list(case.get("case_member_problem_ids")):
            member_by_problem[problem_id] = case

    propagated: list[dict[str, Any]] = []
    for index, raw_item in enumerate(items):
        item = dict(raw_item)
        item_problem_id = _clean_string(item.get("problem_id"))
        # A provisional group keeps each durable problem/case record while exposing the
        # full member set on both records.  Prefer the record whose own problem_id matches;
        # use member aliases only when no direct record exists (the canonical-merge case).
        matching_case = direct_by_problem.get(item_problem_id or "") or member_by_problem.get(
            item_problem_id or ""
        )
        if matching_case is None:
            if strict_new_output:
                raise ValueError(
                    f"downstream_items[{index}]: unknown or missing problem_id {item_problem_id!r}"
                )
            propagated.append(item)
            continue
        expected_case_id = _clean_string(matching_case.get("case_id"))
        existing_case_id = _clean_string(item.get("case_id"))
        if (
            strict_new_output
            and existing_case_id is not None
            and expected_case_id is not None
            and existing_case_id != expected_case_id
        ):
            raise ValueError(
                f"downstream_items[{index}] {item_problem_id}: case_id mismatch "
                f"expected={expected_case_id} got={existing_case_id}"
            )
        item["case_id"] = expected_case_id
        item["canonical_problem_id"] = (
            _clean_string(matching_case.get("canonical_problem_id")) or item_problem_id
        )
        item["case_member_problem_ids"] = _clean_string_list(
            matching_case.get("case_member_problem_ids")
        )
        group_id = _clean_string(matching_case.get("same_cause_group_id"))
        if group_id is not None:
            item["same_cause_group_id"] = group_id
        propagated.append(item)
    return propagated


def atom_disposition_summary(atoms: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Return disposition counters suitable for stage/backlog metadata."""

    counts = {name: 0 for name in sorted(ATOM_DISPOSITIONS)}
    high_unresolved: list[str] = []
    high_pending: list[str] = []
    decided = 0
    pending = 0
    for atom in atoms:
        disposition = _clean_string(atom.get("disposition")) or "unresolved"
        counts[disposition] = counts.get(disposition, 0) + 1
        if _clean_string(atom.get("disposition_status")) == "decided":
            decided += 1
        else:
            pending += 1
        if disposition == "unresolved" and _clean_string(atom.get("severity_hint")) in {
            "high",
            "blocker",
        }:
            atom_id = _clean_string(atom.get("atom_id"))
            if atom_id is not None:
                high_unresolved.append(atom_id)
                if atom_disposition_receipt_errors(atom, require_decided=True):
                    high_pending.append(atom_id)
    return {
        "total": len(atoms),
        "counts": counts,
        "decision_status_counts": {"decided": decided, "pending": pending},
        "high_severity_unresolved_count": len(high_unresolved),
        "high_severity_unresolved_atom_ids": high_unresolved,
        "high_severity_pending_count": len(high_pending),
        "high_severity_pending_atom_ids": high_pending,
    }


__all__ = [
    "ATOM_DISPOSITIONS",
    "ATOM_DISPOSITION_RECEIPT_SCHEMA_VERSION",
    "ATOM_DISPOSITION_SOURCES",
    "ATOM_DISPOSITION_STATUSES",
    "CASE_REGISTRY_SCHEMA_VERSION",
    "DOWNSTREAM_CHAIN_CONTRACT_REVISION",
    "SOURCE_EVIDENCE_PROJECTION_VERSION",
    "EVIDENCE_ROLES",
    "TERMINAL_CASE_STATES",
    "apply_atom_dispositions",
    "apply_atom_disposition_decision",
    "atom_disposition_receipt_errors",
    "attach_supporting_atoms_to_problem_cases",
    "assign_problem_case_ids",
    "atom_disposition_summary",
    "atom_is_independent_problem_evidence",
    "atom_is_idea_originated",
    "build_case_registry",
    "build_source_evidence_snapshot",
    "downstream_chain_input_sha256",
    "eligible_problem_mining_atoms",
    "empty_case_registry",
    "load_case_registry",
    "make_atom_disposition_receipt",
    "mint_case_id",
    "normalize_atom_lineage",
    "propagate_case_lineage",
    "problem_case_records_from_registry",
    "source_evidence_atom_projection",
    "source_evidence_atom_sha256",
    "source_evidence_snapshot_sha256",
    "provisional_same_cause_clearance_errors",
    "provisional_same_cause_group_errors",
    "record_lineage_context",
    "update_case_registry_stage_lineage",
    "verified_causal_identities_from_case_registry",
    "verified_mechanism_identities_from_case_registry",
    "validate_atom_lineage",
    "write_case_registry",
]
