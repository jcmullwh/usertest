"""Runner-owned same-mechanism consolidation between research and optioning."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

from backlog_core.case_lineage import (
    apply_atom_disposition_decision,
    atom_disposition_receipt_errors,
    mint_case_id,
    validate_atom_lineage,
    verified_mechanism_identities_from_case_registry,
)
from backlog_core.stage_contracts import (
    assess_research_readiness,
    research_claims_sha256,
    research_evidence_role_partition,
    research_relation_assessment_errors,
)
from backlog_miner.research_evidence import verify_persisted_research_evidence
from backlog_repo import validate_case_relation_receipt

POST_RESEARCH_SPLIT_RECEIPT_SCHEMA_VERSION = 1
POST_RESEARCH_SPLIT_RECEIPT_KIND = "post_research_case_split"
PROBLEM_MINING_RELATION_SPLIT_RECEIPT_SCHEMA_VERSION = 1
PROBLEM_MINING_RELATION_SPLIT_RECEIPT_KIND = "problem_mining_relation_split"


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item.strip() for item in value if _text(item) is not None))


def _canonical_sha256(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _unique_sorted_records(value: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_json = {json.dumps(item, sort_keys=True, separators=(",", ":")): item for item in value}
    return [by_json[key] for key in sorted(by_json)]


def _argument_identity(value: Any) -> dict[str, Any] | None:
    argument = value if isinstance(value, dict) else None
    if argument is None:
        return None
    return {
        key: argument.get(key) for key in ("slot", "ast_sha256") if argument.get(key) is not None
    }


def _controlled_input_identity(value: Any) -> dict[str, Any] | None:
    controlled = value if isinstance(value, dict) else None
    if controlled is None:
        return None
    difference_raw = controlled.get("difference")
    difference = difference_raw if isinstance(difference_raw, dict) else None
    if difference is None:
        return None
    projected_difference = {
        key: difference.get(key)
        for key in (
            "mechanism_symbol",
            "slot",
            "difference_kind",
            "baseline_argument",
            "challenge_argument",
            "baseline_file_sha256",
            "challenge_file_sha256",
            "content_relation",
        )
        if difference.get(key) is not None
    }
    for field in ("support_argument", "control_argument"):
        argument = _argument_identity(difference.get(field))
        if argument is not None:
            projected_difference[field] = argument
    if not projected_difference.get("slot"):
        return None
    return {
        "difference_count": controlled.get("difference_count"),
        "difference": projected_difference,
    }


def _observation_identity(value: Any) -> dict[str, Any] | None:
    observation = value if isinstance(value, dict) else None
    if observation is None:
        return None
    projection: dict[str, Any] = {
        key: observation.get(key)
        for key in (
            "polarity",
            "source",
            "difference_kind",
            "exit_code",
        )
        if observation.get(key) is not None
    }
    for field in ("baseline", "challenge", "support", "control"):
        raw_side = observation.get(field)
        if not isinstance(raw_side, dict):
            continue
        projection[field] = {
            key: raw_side.get(key) for key in ("exit_code",) if raw_side.get(key) is not None
        }
    assertion = observation.get("assertion")
    if isinstance(assertion, dict):
        projection["assertion"] = {
            key: assertion.get(key)
            for key in ("source", "operator", "expected")
            if assertion.get(key) is not None
        }
    return projection or None


def _control_point_identity(value: Any) -> dict[str, Any] | None:
    point = value if isinstance(value, dict) else None
    if point is None:
        return None
    projection = {
        key: point.get(key)
        for key in (
            "mechanism_symbol",
            "slot",
            "path",
            "code_path",
        )
        if point.get(key) is not None
    }
    symbols = sorted(_strings(point.get("mechanism_symbols")))
    if symbols:
        projection["mechanism_symbols"] = symbols
    return projection or None


def _code_path_identity(value: Any) -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, dict):
            continue
        path = {
            key: raw.get(key)
            for key in (
                "symbol",
                "path",
            )
            if raw.get(key) is not None
        }
        if path:
            paths.append(path)
    return sorted(paths, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))


def verified_causal_evidence_projection(
    dossier: dict[str, Any],
    *,
    verified_mechanism_sha256: str,
) -> dict[str, Any] | None:
    """Return cross-case identity from runner-attested causal facts.

    Model relationship wording and case-local hypothesis/experiment IDs remain in the
    retained research receipt for audit, but do not make two independent proofs of the
    same mechanism look different.
    """

    verification_raw = dossier.get("evidence_verification")
    verification = verification_raw if isinstance(verification_raw, dict) else {}
    provenance_raw = verification.get("verified_mechanism_provenance")
    provenance = provenance_raw if isinstance(provenance_raw, dict) else None
    if provenance is None or verification.get(
        "verified_mechanism_provenance_sha256"
    ) != _canonical_sha256(provenance):
        return None
    hypothesis_id = _text(provenance.get("primary_hypothesis_id"))
    if hypothesis_id is None:
        return None

    interventions: list[dict[str, Any]] = []
    for field in ("control_verifications", "falsification_interventions"):
        raw = verification.get(field)
        for receipt in raw if isinstance(raw, list) else []:
            if not isinstance(receipt, dict) or receipt.get("hypothesis_id") != hypothesis_id:
                continue
            controlled = receipt.get("controlled_input_difference")
            if not isinstance(controlled, dict):
                continue
            interventions.append(
                {
                    "mechanism_symbols": sorted(_strings(receipt.get("mechanism_symbols"))),
                    "controlled_input_difference": _controlled_input_identity(controlled),
                    "observed_polarity": _observation_identity(receipt.get("observed_polarity")),
                    "observable_difference": _observation_identity(
                        receipt.get("observable_difference")
                    ),
                }
            )

    closures: list[dict[str, Any]] = []
    raw_closures = verification.get("deterministic_mechanism_closures")
    for closure in raw_closures if isinstance(raw_closures, list) else []:
        if not isinstance(closure, dict) or closure.get("hypothesis_id") != hypothesis_id:
            continue
        closures.append(
            {
                "scenario_kind": closure.get("scenario_kind"),
                "closure_basis": closure.get("closure_basis"),
                "mechanism_symbols": sorted(_strings(closure.get("mechanism_symbols"))),
                "code_path": _code_path_identity(closure.get("code_path")),
                "observed_result": _observation_identity(closure.get("observed_result")),
            }
        )
    control_points = provenance.get("research_probe_control_points")
    control_points = control_points if isinstance(control_points, list) else []
    projected_control_points = [
        projected
        for value in control_points
        for projected in [_control_point_identity(value)]
        if projected is not None
    ]
    if not interventions and not closures and not projected_control_points:
        return None
    return {
        "schema_version": 2,
        "repo_revision": _text(dossier.get("repo_revision")),
        "verified_mechanism_sha256": verified_mechanism_sha256,
        "research_probe_control_points": sorted(
            _unique_sorted_records(projected_control_points),
            key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
        ),
        "interventions": sorted(
            _unique_sorted_records(interventions),
            key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
        ),
        "deterministic_closures": sorted(
            _unique_sorted_records(closures),
            key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
        ),
    }


def _verified_causal_signature(
    dossier: dict[str, Any],
    *,
    verified_mechanism_sha256: str,
) -> str | None:
    projection = verified_causal_evidence_projection(
        dossier,
        verified_mechanism_sha256=verified_mechanism_sha256,
    )
    return _canonical_sha256(projection) if projection is not None else None


def _primary_verified_mechanism_origin_atom_ids(dossier: dict[str, Any]) -> set[str]:
    """Return source atoms covered by selected evidence for the primary mechanism."""

    verification_raw = dossier.get("evidence_verification")
    verification = verification_raw if isinstance(verification_raw, dict) else {}
    if verification.get("status") != "verified":
        return set()
    provenance_raw = verification.get("verified_mechanism_provenance")
    provenance = provenance_raw if isinstance(provenance_raw, dict) else {}
    primary_hypothesis_id = _text(provenance.get("primary_hypothesis_id"))
    selected_evidence_ids = set(_strings(provenance.get("mechanism_evidence_ids")))
    if primary_hypothesis_id is None or not selected_evidence_ids:
        return set()
    mechanism_evidence_raw = verification.get("mechanism_evidence")
    mechanism_evidence = mechanism_evidence_raw if isinstance(mechanism_evidence_raw, list) else []
    return {
        atom_id
        for evidence in mechanism_evidence
        if isinstance(evidence, dict)
        and _text(evidence.get("mechanism_evidence_id")) in selected_evidence_ids
        and _text(evidence.get("hypothesis_id")) == primary_hypothesis_id
        and evidence.get("adversarial_effect") == "supports_selection"
        for atom_id in _strings(evidence.get("origin_atom_ids"))
    }


def _write_immutable_split_receipt(
    requested_path: Path,
    *,
    payload_without_hash: dict[str, Any],
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    """Persist one content-addressed runner split decision."""

    content_sha256 = _canonical_sha256(payload_without_hash)
    payload = {**payload_without_hash, "content_sha256": content_sha256}
    requested_path = requested_path.expanduser().resolve()
    receipt_path = requested_path.with_name(
        f"{requested_path.stem}.{content_sha256[:16]}{requested_path.suffix}"
    )
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if receipt_path.exists():
        if not receipt_path.is_file() or receipt_path.read_text(encoding="utf-8") != text:
            raise OSError(f"Immutable post-research split receipt mismatch: {receipt_path}")
    else:
        receipt_path.write_text(text, encoding="utf-8")
    receipt_sha256 = sha256(receipt_path.read_bytes()).hexdigest()
    return (
        payload,
        receipt_path,
        {
            "schema_version": POST_RESEARCH_SPLIT_RECEIPT_SCHEMA_VERSION,
            "receipt_kind": POST_RESEARCH_SPLIT_RECEIPT_KIND,
            "receipt_path": str(receipt_path),
            "receipt_sha256": receipt_sha256,
            "content_sha256": content_sha256,
        },
    )


def _facet_context_atom(
    *,
    parent_case_id: str,
    child_case_id: str,
    facet: Mapping[str, Any],
    dossier: Mapping[str, Any],
) -> dict[str, Any]:
    occurrence_ids = sorted(_strings(facet.get("occurrence_evidence_atom_ids")))
    boundary = dict(facet.get("boundary")) if isinstance(facet.get("boundary"), Mapping) else {}
    content = {
        "parent_case_id": parent_case_id,
        "child_case_id": child_case_id,
        "title": facet.get("title"),
        "problem": facet.get("problem"),
        "user_impact": facet.get("user_impact"),
        "occurrence_evidence_atom_ids": occurrence_ids,
        "boundary": boundary,
    }
    content_sha256 = _canonical_sha256(content)
    atom_id = f"atom:post-research-facet:{content_sha256[:24]}"
    atom = {
        "atom_id": atom_id,
        "run_id": f"post_research_relation:{content_sha256[:16]}",
        "run_rel": f"post_research_relation/{content_sha256[:16]}",
        "origin_run_id": f"post_research_relation:{content_sha256[:16]}",
        "origin_stage": "post_research_relation",
        # The title/problem/impact/boundary are a research conclusion over signed
        # occurrences, not a fresh observation.  Keeping this as proposal context
        # prevents a useful split from being re-mined as if it were independent
        # runtime evidence.
        "evidence_role": "research",
        "evidence_class": "proposal",
        "source": "post_research_facet_context",
        "status": "identified",
        "severity_hint": dossier.get("severity") or "medium",
        "text": (
            f"{facet.get('title')}\n\n{facet.get('problem')}\n\n"
            f"User impact: {facet.get('user_impact')}\n\n"
            f"Authenticated boundary: {boundary.get('statement')}"
        ),
        "derived_from_atom_ids": occurrence_ids,
        "parent_case_id": parent_case_id,
        "case_id": child_case_id,
        "supporting_case_ids": [child_case_id],
        "disposition": "novel_case",
        "disposition_status": "pending",
        "disposition_receipt": None,
        "novel_case_rationale": (
            "Authenticated Stage-3 evidence established a distinct causal/action "
            f"boundary from parent case {parent_case_id}."
        ),
        "post_research_split_facet_id": facet.get("facet_id"),
        "occurrence_evidence_atom_ids": occurrence_ids,
        "authenticated_boundary": boundary,
        "authenticated_boundary_sha256": _canonical_sha256(boundary),
    }
    atom = apply_atom_disposition_decision(
        atom,
        disposition="novel_case",
        source="post_research_split",
        rationale=(
            "Runner authenticated an exact occurrence partition and distinct boundary "
            f"for split child {child_case_id}."
        ),
    )
    validate_atom_lineage(atom, context=f"post_research_split_atom:{atom_id}")
    return atom


def authenticated_split_child_occurrence_evidence(
    record: Mapping[str, Any],
    *,
    atoms_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[list[str], list[str]]:
    """Resolve a split child's original occurrences through runner-owned provenance.

    The child problem text and facet atom are research-derived context.  Stage 3 may
    inspect the original observations only after the immutable split receipt, the
    child identity, and the context atom lineage agree.  Merely copying occurrence
    IDs into a problem record is intentionally insufficient.
    """

    problem_mining_ref_raw = record.get("problem_mining_relation_split_receipt")
    if isinstance(problem_mining_ref_raw, Mapping):
        errors: list[str] = []
        split_ref = dict(problem_mining_ref_raw)
        supplied_content_sha = _text(split_ref.pop("content_sha256", None))
        if supplied_content_sha != _canonical_sha256(split_ref):
            errors.append("problem_mining_split_reference_content_sha256_mismatch")
        split_ref["content_sha256"] = supplied_content_sha
        occurrence_ids = sorted(_strings(record.get("occurrence_evidence_atom_ids")))
        evidence_ids = sorted(_strings(record.get("evidence_atom_ids")))
        if (
            split_ref.get("schema_version")
            != PROBLEM_MINING_RELATION_SPLIT_RECEIPT_SCHEMA_VERSION
            or split_ref.get("producer") != "usertest_backlog.problem_mining"
            or split_ref.get("receipt_kind")
            != PROBLEM_MINING_RELATION_SPLIT_RECEIPT_KIND
        ):
            errors.append("problem_mining_split_reference_contract_invalid")
        if (
            _text(split_ref.get("parent_case_id"))
            != _text(record.get("split_from_case_id"))
            or _text(split_ref.get("parent_problem_id"))
            != _text(record.get("split_parent_problem_id"))
            or _text(split_ref.get("child_case_id")) != _text(record.get("case_id"))
            or _text(split_ref.get("child_problem_id"))
            != _text(record.get("problem_id"))
            or sorted(_strings(split_ref.get("evidence_atom_ids"))) != evidence_ids
            or sorted(_strings(split_ref.get("occurrence_evidence_atom_ids")))
            != occurrence_ids
            or not occurrence_ids
            or not set(occurrence_ids).issubset(evidence_ids)
        ):
            errors.append("problem_mining_split_reference_record_mismatch")

        receipt_path_raw = _text(split_ref.get("relation_receipt_path"))
        expected_receipt_sha = _text(split_ref.get("relation_receipt_sha256"))
        expected_receipt_content_sha = _text(
            split_ref.get("relation_receipt_content_sha256")
        )
        try:
            receipt_path = Path(receipt_path_raw or "").expanduser().resolve()
            receipt_bytes = receipt_path.read_bytes()
        except (OSError, RuntimeError):
            return [], [*errors, "problem_mining_split_relation_receipt_unavailable"]
        if sha256(receipt_bytes).hexdigest() != expected_receipt_sha:
            errors.append("problem_mining_split_relation_receipt_sha256_mismatch")
        try:
            receipt_raw = json.loads(receipt_bytes.decode("utf-8"))
            receipt = validate_case_relation_receipt(receipt_raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            return [], [*errors, "problem_mining_split_relation_receipt_invalid"]
        if (
            receipt.get("stage") != "problem_mining"
            or receipt.get("content_sha256") != expected_receipt_content_sha
        ):
            errors.append("problem_mining_split_relation_receipt_contract_mismatch")
        response_path = Path(str(receipt["relation_review_response_path"]))
        try:
            response_bytes = response_path.read_bytes()
        except OSError:
            return [], [*errors, "problem_mining_split_response_unavailable"]
        if sha256(response_bytes).hexdigest() != receipt["relation_review_response_sha256"]:
            errors.append("problem_mining_split_response_sha256_mismatch")
        try:
            decisions = json.loads(response_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return [], [*errors, "problem_mining_split_response_invalid"]
        decision_index = split_ref.get("decision_index")
        split_group_index = split_ref.get("split_group_index")
        if (
            not isinstance(decisions, list)
            or not isinstance(decision_index, int)
            or isinstance(decision_index, bool)
            or not 0 <= decision_index < len(decisions)
        ):
            return [], [*errors, "problem_mining_split_decision_reference_invalid"]
        decision = decisions[decision_index]
        decision_sha = _canonical_sha256(decision)
        groups = decision.get("split_groups") if isinstance(decision, Mapping) else None
        if (
            not isinstance(decision, Mapping)
            or decision.get("action") != "split"
            or _text(decision.get("focus_id"))
            != _text(record.get("split_parent_problem_id"))
            or decision_sha != _text(split_ref.get("decision_sha256"))
            or not isinstance(groups, list)
            or not isinstance(split_group_index, int)
            or isinstance(split_group_index, bool)
            or not 0 <= split_group_index < len(groups)
        ):
            return [], [*errors, "problem_mining_split_decision_binding_invalid"]
        split_group = groups[split_group_index]
        if (
            not isinstance(split_group, Mapping)
            or sorted(_strings(split_group.get("evidence_atom_ids"))) != evidence_ids
        ):
            errors.append("problem_mining_split_group_binding_invalid")
        for atom_id in occurrence_ids:
            if atom_id not in atoms_by_id:
                errors.append(f"split_occurrence_atom_missing:{atom_id}")
        if errors:
            return [], list(dict.fromkeys(errors))
        return occurrence_ids, []

    # Occurrence membership is useful on ordinary cases too. Enter the post-research
    # receipt path only when a record claims that distinct provenance contract.
    if (
        record.get("split_from_case_id") is None
        and record.get("post_research_split_receipt") is None
    ):
        return [], []

    errors: list[str] = []
    parent_case_id = _text(record.get("split_from_case_id"))
    child_case_id = _text(record.get("case_id"))
    child_problem_id = _text(record.get("problem_id"))
    recorded_occurrence_ids = sorted(_strings(record.get("occurrence_evidence_atom_ids")))
    if parent_case_id is None:
        errors.append("split_parent_case_id_missing")
    if child_case_id is None:
        errors.append("split_child_case_id_missing")
    if child_problem_id is None:
        errors.append("split_child_problem_id_missing")
    if not recorded_occurrence_ids:
        errors.append("split_occurrence_evidence_missing")

    receipt_ref_raw = record.get("post_research_split_receipt")
    receipt_ref = dict(receipt_ref_raw) if isinstance(receipt_ref_raw, Mapping) else None
    if receipt_ref is None:
        return [], [*errors, "split_receipt_reference_missing"]
    receipt_path_raw = _text(receipt_ref.get("receipt_path"))
    expected_receipt_sha = _text(receipt_ref.get("receipt_sha256"))
    expected_content_sha = _text(receipt_ref.get("content_sha256"))
    if receipt_ref.get("schema_version") != POST_RESEARCH_SPLIT_RECEIPT_SCHEMA_VERSION:
        errors.append("split_receipt_reference_schema_invalid")
    if receipt_ref.get("receipt_kind") != POST_RESEARCH_SPLIT_RECEIPT_KIND:
        errors.append("split_receipt_reference_kind_invalid")
    if receipt_path_raw is None or expected_receipt_sha is None or expected_content_sha is None:
        return [], [*errors, "split_receipt_reference_fields_missing"]

    try:
        receipt_path = Path(receipt_path_raw).expanduser().resolve()
        receipt_bytes = receipt_path.read_bytes()
    except (OSError, RuntimeError):
        return [], [*errors, "split_receipt_unavailable"]
    if sha256(receipt_bytes).hexdigest() != expected_receipt_sha:
        errors.append("split_receipt_sha256_mismatch")
    try:
        receipt_raw = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [], [*errors, "split_receipt_json_invalid"]
    receipt = dict(receipt_raw) if isinstance(receipt_raw, Mapping) else None
    if receipt is None:
        return [], [*errors, "split_receipt_document_invalid"]
    content_sha = _text(receipt.get("content_sha256"))
    content_without_hash = {key: value for key, value in receipt.items() if key != "content_sha256"}
    if content_sha != expected_content_sha or content_sha != _canonical_sha256(
        content_without_hash
    ):
        errors.append("split_receipt_content_sha256_mismatch")
    if (
        receipt.get("schema_version") != POST_RESEARCH_SPLIT_RECEIPT_SCHEMA_VERSION
        or receipt.get("producer") != "usertest_backlog"
        or receipt.get("receipt_kind") != POST_RESEARCH_SPLIT_RECEIPT_KIND
    ):
        errors.append("split_receipt_contract_invalid")
    if receipt.get("stage") != "repro_research":
        errors.append("split_receipt_stage_invalid")
    if _text(receipt.get("parent_case_id")) != parent_case_id:
        errors.append("split_receipt_parent_case_mismatch")
    if _text(receipt.get("parent_problem_id")) != _text(record.get("split_parent_problem_id")):
        errors.append("split_receipt_parent_problem_mismatch")
    assessment_raw = receipt.get("assessment")
    assessment = assessment_raw if isinstance(assessment_raw, Mapping) else {}
    if assessment.get("disposition") != "split":
        errors.append("split_receipt_assessment_invalid")

    facets_raw = receipt.get("facets")
    facets = (
        [dict(item) for item in facets_raw if isinstance(item, Mapping)]
        if isinstance(facets_raw, list)
        else []
    )
    if len(facets) < 2:
        errors.append("split_receipt_facets_invalid")
    seen_occurrences: set[str] = set()
    receipt_occurrences: list[str] = []
    for facet in facets:
        facet_occurrences = sorted(_strings(facet.get("occurrence_evidence_atom_ids")))
        overlap = seen_occurrences.intersection(facet_occurrences)
        if overlap:
            errors.append("split_receipt_facet_occurrence_overlap")
        seen_occurrences.update(facet_occurrences)
        receipt_occurrences.extend(facet_occurrences)
    top_level_occurrences = sorted(_strings(receipt.get("occurrence_evidence_atom_ids")))
    if sorted(receipt_occurrences) != top_level_occurrences:
        errors.append("split_receipt_occurrence_partition_mismatch")

    matching_facets = [
        facet
        for facet in facets
        if _text(facet.get("child_case_id")) == child_case_id
        and _text(facet.get("child_problem_id")) == child_problem_id
    ]
    if len(matching_facets) != 1:
        return [], [*errors, "split_receipt_child_facet_mismatch"]
    facet = matching_facets[0]
    facet_occurrence_ids = sorted(_strings(facet.get("occurrence_evidence_atom_ids")))
    if facet_occurrence_ids != recorded_occurrence_ids:
        errors.append("split_child_occurrence_membership_mismatch")

    context_atom_id = _text(facet.get("facet_context_atom_id"))
    context_atom = atoms_by_id.get(context_atom_id or "")
    if context_atom_id is None or not isinstance(context_atom, Mapping):
        errors.append("split_facet_context_atom_missing")
    else:
        evidence_ids = _strings(record.get("evidence_atom_ids"))
        derived_ids = _strings(record.get("derived_evidence_atom_ids"))
        if context_atom_id not in evidence_ids or context_atom_id not in derived_ids:
            errors.append("split_facet_context_membership_mismatch")
        if (
            context_atom.get("source") != "post_research_facet_context"
            or context_atom.get("origin_stage") != "post_research_relation"
            or context_atom.get("evidence_role") != "research"
            or context_atom.get("evidence_class") != "proposal"
            or context_atom.get("disposition") != "novel_case"
            or _text(context_atom.get("parent_case_id")) != parent_case_id
            or _text(context_atom.get("case_id")) != child_case_id
            or sorted(_strings(context_atom.get("derived_from_atom_ids"))) != facet_occurrence_ids
            or sorted(_strings(context_atom.get("occurrence_evidence_atom_ids")))
            != facet_occurrence_ids
        ):
            errors.append("split_facet_context_lineage_mismatch")
        errors.extend(
            f"split_facet_context_disposition_invalid:{error}"
            for error in atom_disposition_receipt_errors(context_atom, require_decided=True)
        )
        try:
            validate_atom_lineage(
                context_atom,
                context=f"post_research_split_child:{child_problem_id or 'unknown'}",
            )
        except ValueError:
            errors.append("split_facet_context_lineage_invalid")

    for atom_id in facet_occurrence_ids:
        if atom_id not in atoms_by_id:
            errors.append(f"split_occurrence_atom_missing:{atom_id}")
    if errors:
        return [], list(dict.fromkeys(errors))
    return facet_occurrence_ids, []


def apply_post_research_relation_assessments(
    *,
    problem_records: list[dict[str, Any]],
    priority_decisions: list[dict[str, Any]],
    research_dossiers: list[dict[str, Any]],
    atoms: list[dict[str, Any]],
    receipt_dir: Path,
    verify_dossier: Callable[[dict[str, Any]], tuple[bool, list[str]]] = (
        verify_persisted_research_evidence
    ),
) -> dict[str, Any]:
    """Apply authenticated Stage-3 splits before optioning.

    Split children are intentionally returned without research dossiers. The split
    captures a better problem boundary; it is not evidence that either child is ready
    for solution optioning.
    """

    problems_by_case = {
        str(record.get("case_id")): record
        for record in problem_records
        if _text(record.get("case_id")) is not None
    }
    priorities_by_problem = {
        str(decision.get("problem_id")): decision
        for decision in priority_decisions
        if _text(decision.get("problem_id")) is not None
    }
    split_children_by_case: dict[str, list[dict[str, Any]]] = {}
    split_priorities_by_case: dict[str, list[dict[str, Any]]] = {}
    split_parent_case_ids: set[str] = set()
    new_atoms: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    split_groups: list[dict[str, Any]] = []

    for dossier in research_dossiers:
        assessment_raw = dossier.get("case_relation_assessment")
        assessment = assessment_raw if isinstance(assessment_raw, Mapping) else None
        if assessment is None or assessment.get("disposition") != "split":
            continue
        case_id = _text(dossier.get("case_id"))
        problem_id = _text(dossier.get("problem_id"))
        parent = problems_by_case.get(case_id or "")
        if case_id is None or problem_id is None or parent is None:
            raise ValueError(f"post_research_split_parent_missing:{problem_id or case_id}")
        relation_errors = research_relation_assessment_errors(
            dossier,
            pid=problem_id,
            authenticate=True,
        )
        receipt_ready, evidence_errors = verify_dossier(dossier)
        if relation_errors or not receipt_ready:
            raise ValueError(
                "post_research_split_evidence_invalid: "
                + ",".join([*relation_errors, *evidence_errors])
            )

        facets_raw = assessment.get("facets")
        facets = [dict(facet) for facet in facets_raw if isinstance(facet, Mapping)]
        facets.sort(
            key=lambda facet: (
                tuple(sorted(_strings(facet.get("occurrence_evidence_atom_ids")))),
                str(facet.get("facet_id")),
            )
        )
        child_records: list[dict[str, Any]] = []
        child_priorities: list[dict[str, Any]] = []
        receipt_facets: list[dict[str, Any]] = []
        parent_problem_ids = _strings(parent.get("case_member_problem_ids")) or [problem_id]
        parent_priority = priorities_by_problem.get(problem_id, {})
        for facet in facets:
            occurrence_ids = sorted(_strings(facet.get("occurrence_evidence_atom_ids")))
            child_case_id = mint_case_id(
                [case_id, *occurrence_ids],
                namespace="post_research_split",
            )
            child_problem_id = f"{problem_id}:facet:{child_case_id.removeprefix('case:')}"
            facet_atom = _facet_context_atom(
                parent_case_id=case_id,
                child_case_id=child_case_id,
                facet=facet,
                dossier=dossier,
            )
            new_atoms.append(facet_atom)
            child = {
                "case_id": child_case_id,
                "case_identity_status": "resolved",
                "problem_id": child_problem_id,
                "canonical_problem_id": child_problem_id,
                "case_member_problem_ids": [child_problem_id],
                "case_revision": 1,
                "title": facet["title"],
                "problem": facet["problem"],
                "user_impact": facet["user_impact"],
                "severity": parent.get("severity") or "medium",
                "confidence": parent.get("confidence")
                if parent.get("confidence") is not None
                else 0.5,
                "problem_status": "identified",
                "evidence_summary": (
                    "Runner-authenticated Stage-3 evidence split this facet from a broader "
                    "case; root-cause research for the child has not yet run."
                ),
                "canonical_symptoms": [str(facet["problem"])],
                "root_cause_status": "unestablished",
                "root_cause_confidence": 0.0,
                "evidence_atom_ids": [facet_atom["atom_id"]],
                # The generated facet description is downstream research context.
                # Original occurrences are authenticated separately by the split
                # receipt when this child returns to Stage 3.
                "source_evidence_atom_ids": [],
                "derived_evidence_atom_ids": [facet_atom["atom_id"]],
                "occurrence_evidence_atom_ids": occurrence_ids,
                "split_from_case_id": case_id,
                "split_parent_problem_id": problem_id,
                "split_parent_problem_ids": parent_problem_ids,
                "related_case_ids": [case_id],
                "case_relation_actions": [
                    {
                        "action": "split",
                        "rationale": assessment.get("rationale"),
                        "source": "runner_authenticated_post_research_split_v1",
                        "facet_id": facet.get("facet_id"),
                    }
                ],
            }
            if _text(parent.get("suggested_owner")) is not None:
                child["suggested_owner"] = parent["suggested_owner"]
            child_records.append(child)
            child_priority = {
                **deepcopy(parent_priority),
                "case_id": child_case_id,
                "problem_id": child_problem_id,
                "selected_for_research": False,
                "eligible_for_downstream": False,
                "research_route": "research_required",
                "priority_status": "pending_research",
                "route_reason": (
                    "Post-research split created a new causal work unit. It must receive its "
                    "own Stage-3 proof before optioning."
                ),
            }
            child_priorities.append(child_priority)
            receipt_facets.append(
                {
                    "facet_id": facet.get("facet_id"),
                    "child_case_id": child_case_id,
                    "child_problem_id": child_problem_id,
                    "facet_context_atom_id": facet_atom["atom_id"],
                    "occurrence_evidence_atom_ids": occurrence_ids,
                    "title": facet.get("title"),
                    "problem": facet.get("problem"),
                    "user_impact": facet.get("user_impact"),
                    "boundary": deepcopy(facet.get("boundary")),
                    "boundary_sha256": facet_atom["authenticated_boundary_sha256"],
                }
            )

        assignment = dossier.get("evidence_assignment")
        verification = dossier.get("evidence_verification")
        _case_evidence_ids, occurrence_evidence_ids, role_partition_source = (
            research_evidence_role_partition(assignment if isinstance(assignment, Mapping) else {})
        )
        payload_without_hash = {
            "schema_version": POST_RESEARCH_SPLIT_RECEIPT_SCHEMA_VERSION,
            "producer": "usertest_backlog",
            "receipt_kind": POST_RESEARCH_SPLIT_RECEIPT_KIND,
            "stage": "repro_research",
            "parent_case_id": case_id,
            "parent_problem_id": problem_id,
            "repo_revision": dossier.get("repo_revision"),
            "research_status": dossier.get("research_status"),
            "research_claims_sha256": research_claims_sha256(dossier),
            "evidence_assignment_sha256": (
                assignment.get("assignment_sha256") if isinstance(assignment, Mapping) else None
            ),
            "evidence_verification_receipt_sha256": (
                verification.get("receipt_sha256") if isinstance(verification, Mapping) else None
            ),
            "evidence_role_partition_source": role_partition_source,
            "occurrence_evidence_atom_ids": sorted(occurrence_evidence_ids),
            "assessment": deepcopy(dict(assessment)),
            "facets": receipt_facets,
        }
        receipt_key = sha256(case_id.encode("utf-8")).hexdigest()[:16]
        receipt, receipt_path, receipt_ref = _write_immutable_split_receipt(
            receipt_dir / f"post_research_split_{receipt_key}.json",
            payload_without_hash=payload_without_hash,
        )
        for child in child_records:
            child["post_research_split_receipt"] = deepcopy(receipt_ref)
        split_children_by_case[case_id] = child_records
        split_priorities_by_case[case_id] = child_priorities
        split_parent_case_ids.add(case_id)
        receipts.append({**receipt_ref, "parent_case_id": case_id})
        split_groups.append(
            {
                "parent_case_id": case_id,
                "parent_problem_id": problem_id,
                "child_case_ids": [child["case_id"] for child in child_records],
                "child_problem_ids": [child["problem_id"] for child in child_records],
                "occurrence_evidence_atom_ids": receipt["occurrence_evidence_atom_ids"],
                "receipt_path": str(receipt_path),
                "receipt_content_sha256": receipt["content_sha256"],
            }
        )

    if not split_parent_case_ids:
        return {
            "problem_records": [dict(item) for item in problem_records],
            "priority_decisions": [dict(item) for item in priority_decisions],
            "research_dossiers": [dict(item) for item in research_dossiers],
            "split_parent_dossiers": [],
            "atoms": [dict(item) for item in atoms],
            "split_groups": [],
            "split_receipts": [],
        }

    prior_split_child_case_ids = {
        child_case_id
        for record in problem_records
        if _text(record.get("split_from_case_id")) in split_parent_case_ids
        for child_case_id in [_text(record.get("case_id"))]
        if child_case_id is not None
    }
    prior_split_child_problem_ids = {
        child_problem_id
        for record in problem_records
        if _text(record.get("split_from_case_id")) in split_parent_case_ids
        for child_problem_id in [_text(record.get("problem_id"))]
        if child_problem_id is not None
    }
    revised_problems: list[dict[str, Any]] = []
    for record in problem_records:
        case_id = _text(record.get("case_id"))
        if case_id in split_children_by_case:
            revised_problems.extend(deepcopy(split_children_by_case[case_id]))
        elif _text(record.get("split_from_case_id")) in split_parent_case_ids:
            # A revised split replaces the parent's prior active partition.  The
            # durable registry retains those children and marks them superseded;
            # they must not remain in the current downstream working set.
            continue
        else:
            revised_problems.append(deepcopy(record))
    revised_priorities: list[dict[str, Any]] = []
    for decision in priority_decisions:
        case_id = _text(decision.get("case_id"))
        if case_id is None:
            problem_id = _text(decision.get("problem_id"))
            case_id = _text(
                next(
                    (
                        record.get("case_id")
                        for record in problem_records
                        if record.get("problem_id") == problem_id
                    ),
                    None,
                )
            )
        if case_id in split_priorities_by_case:
            revised_priorities.extend(deepcopy(split_priorities_by_case[case_id]))
        elif (
            case_id in prior_split_child_case_ids
            or _text(decision.get("problem_id")) in prior_split_child_problem_ids
        ):
            continue
        else:
            revised_priorities.append(deepcopy(decision))

    atoms_by_id = {
        str(atom.get("atom_id")): deepcopy(atom)
        for atom in atoms
        if _text(atom.get("atom_id")) is not None
    }
    atom_order = [str(atom["atom_id"]) for atom in atoms if _text(atom.get("atom_id")) is not None]
    for atom in new_atoms:
        atom_id = str(atom["atom_id"])
        previous = atoms_by_id.get(atom_id)
        if previous is not None and previous != atom:
            raise ValueError(f"post_research_split_atom_content_mismatch:{atom_id}")
        if previous is None:
            atom_order.append(atom_id)
        atoms_by_id[atom_id] = atom

    return {
        "problem_records": revised_problems,
        "priority_decisions": revised_priorities,
        "research_dossiers": [
            deepcopy(item)
            for item in research_dossiers
            if _text(item.get("case_id")) not in split_parent_case_ids
        ],
        "split_parent_dossiers": [
            deepcopy(item)
            for item in research_dossiers
            if _text(item.get("case_id")) in split_parent_case_ids
        ],
        "atoms": [atoms_by_id[atom_id] for atom_id in atom_order],
        "split_groups": split_groups,
        "split_receipts": receipts,
    }


def collapse_post_research_verified_mechanisms(
    *,
    problem_records: list[dict[str, Any]],
    priority_decisions: list[dict[str, Any]],
    research_dossiers: list[dict[str, Any]],
    case_registry: dict[str, Any],
    verify_dossier: Callable[[dict[str, Any]], tuple[bool, list[str]]] = (
        verify_persisted_research_evidence
    ),
    assess_dossier: Callable[[dict[str, Any]], tuple[bool, list[str]]] = (
        assess_research_readiness
    ),
) -> dict[str, Any]:
    """Collapse only dossiers with the same runner-verified mechanism and revision.

    Every original symptom and outcome oracle remains on the canonical problem packet.
    The original stage-3 dossiers remain persisted separately; downstream stages receive
    one representative dossier so they produce one option/plan unit for one mechanism.
    """

    identities = verified_mechanism_identities_from_case_registry(case_registry)
    eligible: dict[str, dict[str, Any]] = {}
    causal_signatures: dict[str, str] = {}
    research_ready_by_case: dict[str, bool] = {}
    for dossier in research_dossiers:
        case_id = _text(dossier.get("case_id"))
        verification = dossier.get("evidence_verification")
        receipt = verification if isinstance(verification, dict) else {}
        identity = identities.get(case_id or "")
        ready, _errors = verify_dossier(dossier)
        research_ready, _readiness_errors = assess_dossier(dossier)
        causal_signature = (
            _verified_causal_signature(
                dossier,
                verified_mechanism_sha256=identity,
            )
            if identity is not None
            else None
        )
        if (
            case_id is not None
            and ready
            and identity is not None
            and causal_signature is not None
            and receipt.get("status") == "verified"
            and receipt.get("verified_mechanism_sha256") == identity
            and _text(dossier.get("repo_revision")) is not None
        ):
            eligible[case_id] = dossier
            causal_signatures[case_id] = causal_signature
            research_ready_by_case[case_id] = bool(
                research_ready and dossier.get("research_status") == "evidence_sufficient"
            )

    problems_by_case = {
        str(item.get("case_id")): item
        for item in problem_records
        if _text(item.get("case_id")) is not None
    }
    priorities_by_case = {
        str(item.get("case_id")): item
        for item in priority_decisions
        if _text(item.get("case_id")) is not None
    }
    dossiers_by_case = {
        str(item.get("case_id")): item
        for item in research_dossiers
        if _text(item.get("case_id")) is not None
    }
    canonical_by_case: dict[str, str] = {}
    group_meta: list[dict[str, Any]] = []
    canonical_problem_overrides: dict[str, dict[str, Any]] = {}
    canonical_priority_overrides: dict[str, dict[str, Any]] = {}
    canonical_dossier_overrides: dict[str, dict[str, Any]] = {}
    bucket_rank = {"p0": 0, "p1": 1, "p2": 2, "watch": 3}
    registry_cases_raw = case_registry.get("cases")
    registry_cases = registry_cases_raw if isinstance(registry_cases_raw, dict) else {}

    # A pre-research same-cause judgment is deliberately only a research work unit.
    # Finalize its durable identity here, after one runner-verified dossier has bound
    # every member's source atoms to the established mechanism.  If that proof is
    # blocked or incomplete, no alias is written and the original stable IDs survive.
    for problem in problem_records:
        provisional_raw = problem.get("provisional_same_cause_group")
        provisional = provisional_raw if isinstance(provisional_raw, dict) else None
        canonical_case_id = _text(problem.get("case_id"))
        if provisional is None or canonical_case_id is None:
            continue
        member_case_ids = _strings(provisional.get("member_case_ids"))
        facets_raw = provisional.get("member_facets")
        facets = (
            [dict(item) for item in facets_raw if isinstance(item, dict)]
            if isinstance(facets_raw, list)
            else []
        )
        member_evidence_ids = {
            atom_id for facet in facets for atom_id in _strings(facet.get("evidence_atom_ids"))
        }
        member_source_evidence_ids = {
            atom_id
            for facet in facets
            for atom_id in _strings(facet.get("source_evidence_atom_ids"))
        }
        facet_case_ids = {
            case_id
            for facet in facets
            for case_id in [_text(facet.get("case_id"))]
            if case_id is not None
        }
        every_facet_has_source_evidence = all(
            bool(_strings(facet.get("source_evidence_atom_ids"))) for facet in facets
        )
        dossier = eligible.get(canonical_case_id)
        assignment_raw = dossier.get("evidence_assignment") if dossier is not None else None
        assignment = assignment_raw if isinstance(assignment_raw, dict) else {}
        assigned_ids = set(_strings(assignment.get("expected_atom_ids")))
        primary_origin_atom_ids = (
            _primary_verified_mechanism_origin_atom_ids(dossier) if dossier is not None else set()
        )
        if (
            len(member_case_ids) < 2
            or canonical_case_id not in member_case_ids
            or len(facets) != len(member_case_ids)
            or facet_case_ids != set(member_case_ids)
            or not every_facet_has_source_evidence
            or not member_evidence_ids
            or not member_source_evidence_ids
            or dossier is None
            or not research_ready_by_case.get(canonical_case_id, False)
            or assigned_ids != member_source_evidence_ids
            or not member_source_evidence_ids.issubset(primary_origin_atom_ids)
        ):
            continue
        mechanism_identity = identities.get(canonical_case_id)
        causal_signature = causal_signatures.get(canonical_case_id)
        if mechanism_identity is None or causal_signature is None:
            continue
        canonical = deepcopy(problem)
        absorbed_case_ids = [case_id for case_id in member_case_ids if case_id != canonical_case_id]
        canonical.pop("provisional_same_cause_group", None)
        canonical.pop("case_identity_candidate_ids", None)
        canonical["case_identity_status"] = "resolved"
        canonical["same_cause_group_id"] = f"cause:{causal_signature[:16]}"
        canonical["absorbed_case_ids"] = absorbed_case_ids
        canonical["symptom_facets"] = facets
        canonical["verified_mechanism_sha256"] = mechanism_identity
        canonical["verified_causal_signature_sha256"] = causal_signature
        canonical["verified_causal_signature_source"] = "runner_verified_causal_signature_v1"
        canonical["case_relation_actions"] = [
            {
                "action": "same_cause_group",
                "group_id": canonical["same_cause_group_id"],
                "rationale": (
                    "One runner-verified dossier covered every provisional member's "
                    "source evidence and established one causal mechanism."
                ),
                "review_confidence": 1.0,
                "source": "runner_verified_provisional_same_cause_v1",
            }
        ]
        canonical_problem_overrides[canonical_case_id] = canonical
        for absorbed_case_id in absorbed_case_ids:
            canonical_by_case[absorbed_case_id] = canonical_case_id
        if canonical_case_id in priorities_by_case:
            priority = deepcopy(priorities_by_case[canonical_case_id])
            priority["provisional_same_cause_resolution"] = "verified_common_mechanism"
            canonical_priority_overrides[canonical_case_id] = priority
        proof_ref = {
            "case_id": dossier.get("case_id"),
            "problem_id": dossier.get("problem_id"),
            "repo_revision": dossier.get("repo_revision"),
            "evidence_verification_receipt_sha256": (
                dossier.get("evidence_verification", {}).get("receipt_sha256")
                if isinstance(dossier.get("evidence_verification"), dict)
                else None
            ),
        }
        bundle = {
            "schema_version": 1,
            "relation_kind": "verified_provisional_same_cause",
            "provisional_group_id": provisional.get("group_id"),
            "canonical_case_id": canonical_case_id,
            "canonical_problem_id": canonical.get("problem_id"),
            "verified_mechanism_sha256": mechanism_identity,
            "verified_causal_signature_sha256": causal_signature,
            "repo_revision": dossier.get("repo_revision"),
            "member_case_ids": member_case_ids,
            "member_problem_ids": _strings(provisional.get("member_problem_ids")),
            "member_evidence_atom_ids": sorted(member_evidence_ids),
            "member_source_evidence_atom_ids": sorted(member_source_evidence_ids),
            "research_proof_refs": [proof_ref],
        }
        bundle["bundle_sha256"] = _canonical_sha256(bundle)
        group_meta.append(bundle)

    grouped: dict[tuple[str, str], list[str]] = {}
    for case_id, dossier in eligible.items():
        grouped.setdefault(
            (
                causal_signatures[case_id],
                str(dossier["repo_revision"]),
            ),
            [],
        ).append(case_id)
    groups = [
        (identity, revision, sorted(case_ids))
        for (identity, revision), case_ids in sorted(grouped.items())
        if len(case_ids) > 1
    ]
    if not groups and not group_meta:
        return {
            "problem_records": [dict(item) for item in problem_records],
            "priority_decisions": [dict(item) for item in priority_decisions],
            "research_dossiers": [dict(item) for item in research_dossiers],
            "groups": [],
            "case_aliases": {},
        }

    def _canonical_rank(case_id: str) -> tuple[int, int, int, int, str]:
        entry_raw = registry_cases.get(case_id)
        entry = entry_raw if isinstance(entry_raw, dict) else {}
        lifecycle = int(isinstance(entry.get("current_lifecycle"), dict))
        durable_records = sum(
            len(value) if isinstance(value, (dict, list)) else 0
            for value in (
                entry.get("plan_revisions"),
                entry.get("ticket_records"),
                entry.get("plan_outcomes"),
            )
        )
        revision_raw = entry.get("case_revision")
        revision = revision_raw if isinstance(revision_raw, int) else 0
        return (
            0 if case_id in canonical_problem_overrides else 1,
            -lifecycle,
            -durable_records,
            -revision,
            case_id,
        )

    for causal_signature, revision, case_ids in groups:
        effective_problems_by_case = {
            **problems_by_case,
            **canonical_problem_overrides,
        }
        if any(case_id not in effective_problems_by_case for case_id in case_ids):
            continue
        canonical_case_id = min(case_ids, key=_canonical_rank)
        case_ids = [canonical_case_id, *sorted(set(case_ids) - {canonical_case_id})]
        problem_group = [effective_problems_by_case[case_id] for case_id in case_ids]
        absorbed_case_ids = list(
            dict.fromkeys(
                [
                    *(
                        case_id
                        for item in problem_group
                        for case_id in _strings(item.get("absorbed_case_ids"))
                    ),
                    *case_ids[1:],
                ]
            )
        )
        absorbed_case_ids = [
            case_id for case_id in absorbed_case_ids if case_id != canonical_case_id
        ]
        for case_id in absorbed_case_ids:
            canonical_by_case[case_id] = canonical_case_id
        dossier_group = [dossiers_by_case[case_id] for case_id in case_ids]
        canonical = deepcopy(effective_problems_by_case[canonical_case_id])
        canonical_problem_id = str(canonical["problem_id"])
        facets: list[dict[str, Any]] = []
        for item in problem_group:
            existing_facets_raw = item.get("symptom_facets")
            existing_facets = (
                [deepcopy(dict(facet)) for facet in existing_facets_raw if isinstance(facet, dict)]
                if isinstance(existing_facets_raw, list)
                else []
            )
            if existing_facets:
                facets.extend(existing_facets)
                continue
            facets.append(
                {
                    "case_id": item.get("case_id"),
                    "problem_id": item.get("problem_id"),
                    "title": item.get("title"),
                    "problem": item.get("problem"),
                    "user_impact": item.get("user_impact"),
                    "canonical_symptoms": _strings(item.get("canonical_symptoms")),
                    "evidence_atom_ids": _strings(item.get("evidence_atom_ids")),
                    "source_evidence_atom_ids": _strings(item.get("source_evidence_atom_ids")),
                }
            )
        outcome_oracles = [
            deepcopy(oracle)
            for dossier in dossier_group
            for verification in [dossier.get("evidence_verification")]
            if isinstance(verification, dict)
            for oracle in (
                verification.get("outcome_oracles")
                if isinstance(verification.get("outcome_oracles"), list)
                else []
            )
            if isinstance(oracle, dict)
        ]
        canonical["case_member_problem_ids"] = list(
            dict.fromkeys(
                problem_id
                for item in problem_group
                for problem_id in [
                    str(item.get("problem_id")),
                    *_strings(item.get("case_member_problem_ids")),
                ]
                if problem_id
            )
        )
        for field in (
            "evidence_atom_ids",
            "source_evidence_atom_ids",
            "derived_evidence_atom_ids",
            "canonical_symptoms",
            "related_case_ids",
        ):
            canonical[field] = list(
                dict.fromkeys(
                    value for item in problem_group for value in _strings(item.get(field))
                )
            )
        canonical["symptom_facets"] = facets
        canonical["same_mechanism_outcome_oracles"] = outcome_oracles
        mechanism_identity = identities[canonical_case_id]
        canonical["verified_mechanism_sha256"] = mechanism_identity
        canonical["verified_causal_signature_sha256"] = causal_signature
        canonical["verified_causal_signature_source"] = "runner_verified_causal_signature_v1"
        canonical["same_cause_group_id"] = f"cause:{causal_signature[:16]}"
        canonical["absorbed_case_ids"] = absorbed_case_ids
        canonical["case_relation_actions"] = [
            *(
                deepcopy(action)
                for item in problem_group
                for action in (
                    item.get("case_relation_actions")
                    if isinstance(item.get("case_relation_actions"), list)
                    else []
                )
                if isinstance(action, dict)
            ),
            {
                "action": "same_cause_group",
                "group_id": canonical["same_cause_group_id"],
                "rationale": (
                    "Runner research established the same content-addressed code surface "
                    "and causal intervention or deterministic closure at the same revision."
                ),
                "review_confidence": 1.0,
                "source": "runner_verified_causal_signature_v1",
            },
        ]
        canonical_problem_overrides[canonical_case_id] = canonical

        effective_priorities_by_case = {
            **priorities_by_case,
            **canonical_priority_overrides,
        }
        priority_group = [
            effective_priorities_by_case[case_id]
            for case_id in case_ids
            if case_id in effective_priorities_by_case
        ]
        if priority_group:
            priority = deepcopy(
                effective_priorities_by_case.get(canonical_case_id, priority_group[0])
            )
            priority["case_id"] = canonical_case_id
            priority["problem_id"] = canonical_problem_id
            priority["selected_for_research"] = True
            priority["priority_status"] = "prioritized"
            priority["priority_bucket"] = min(
                (_text(item.get("priority_bucket")) or "watch" for item in priority_group),
                key=lambda value: bucket_rank.get(value, 99),
            )
            priority["same_mechanism_priority_facets"] = [
                {
                    "case_id": item.get("case_id"),
                    "problem_id": item.get("problem_id"),
                    "priority_bucket": item.get("priority_bucket"),
                    "priority_rationale": item.get("priority_rationale"),
                }
                for item in priority_group
            ]
            canonical_priority_overrides[canonical_case_id] = priority

        dossier = deepcopy(dossiers_by_case[canonical_case_id])
        proof_refs = [
            {
                "case_id": item.get("case_id"),
                "problem_id": item.get("problem_id"),
                "repo_revision": item.get("repo_revision"),
                "evidence_verification_receipt_sha256": (
                    item.get("evidence_verification", {}).get("receipt_sha256")
                    if isinstance(item.get("evidence_verification"), dict)
                    else None
                ),
            }
            for item in dossier_group
        ]
        bundle = {
            "schema_version": 1,
            "canonical_case_id": canonical_case_id,
            "canonical_problem_id": canonical_problem_id,
            "verified_mechanism_sha256": mechanism_identity,
            "verified_causal_signature_sha256": causal_signature,
            "repo_revision": revision,
            "member_case_ids": [canonical_case_id, *absorbed_case_ids],
            "member_problem_ids": canonical["case_member_problem_ids"],
            "research_proof_refs": proof_refs,
            "outcome_oracle_ids": sorted(
                str(oracle.get("outcome_oracle_id"))
                for oracle in outcome_oracles
                if _text(oracle.get("outcome_oracle_id")) is not None
            ),
            "member_research_dossiers": [deepcopy(item) for item in dossier_group],
        }
        bundle["bundle_sha256"] = _canonical_sha256(bundle)
        dossier["post_research_same_mechanism_bundle"] = bundle
        canonical_dossier_overrides[canonical_case_id] = dossier
        group_meta.append(
            {
                key: deepcopy(value)
                for key, value in bundle.items()
                if key != "member_research_dossiers"
            }
        )

    def _collapse(
        items: list[dict[str, Any]], overrides: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        emitted: set[str] = set()
        for item in items:
            case_id = _text(item.get("case_id"))
            canonical_case_id = canonical_by_case.get(case_id or "", case_id)
            if canonical_case_id is None or canonical_case_id in emitted:
                continue
            emitted.add(canonical_case_id)
            result.append(deepcopy(overrides.get(canonical_case_id, item)))
        return result

    return {
        "problem_records": _collapse(problem_records, canonical_problem_overrides),
        "priority_decisions": _collapse(priority_decisions, canonical_priority_overrides),
        "research_dossiers": _collapse(research_dossiers, canonical_dossier_overrides),
        "groups": group_meta,
        "case_aliases": canonical_by_case,
    }


__all__ = [
    "apply_post_research_relation_assessments",
    "collapse_post_research_verified_mechanisms",
    "verified_causal_evidence_projection",
]
