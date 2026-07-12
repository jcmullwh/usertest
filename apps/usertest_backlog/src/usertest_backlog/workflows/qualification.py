"""Independent qualification contracts for shadow backlog cycles.

Qualification uses two deliberately separate artifacts:

* ``QualificationCorpusManifest`` is sealed before the pipeline runs. It binds the
  exact non-IDEA atom corpus and independently groups/labels that evidence. It is
  never supplied to a model stage.
* ``QualificationOutputAdjudication`` is produced after the run. It binds the exact
  accepted outputs and independently rates them good, bad, or unknown.

Shadow scoring joins those artifacts. A pipeline therefore cannot redefine the
actionable denominator after seeing its output, and green syntax/readiness cannot be
mistaken for semantic quality.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from hashlib import sha256
from typing import Any, TypedDict

from backlog_core.case_lineage import (
    atom_is_idea_originated,
    atom_is_independent_problem_evidence,
)

from usertest_backlog.workflows.problem_mining_evidence import immutable_atom_evidence_projection

_MANIFEST_SCHEMA_VERSION = 1
_OUTPUT_ADJUDICATION_SCHEMA_VERSION = 1
_NO_ACTIONABLE_RECEIPT_SCHEMA_VERSION = 1
_ATOM_CLASSIFICATIONS = frozenset({"actionable", "non_actionable", "ambiguous", "unknown"})
_OUTPUT_QUALITIES = frozenset({"good", "bad", "unknown"})
_REPAIR_STATUSES = frozenset({"repaired", "not_repaired", "unknown"})
_BAD_SEVERITIES = frozenset({"critical", "noncritical"})
_CORRECTABILITIES = frozenset({"correctable", "uncorrectable", "unknown"})
_OUTPUT_KINDS = (
    "problem",
    "relation",
    "priority",
    "research",
    "option",
    "selection",
    "plan",
    "ticket",
)
_QUALIFICATION_LIMITATIONS = (
    "independent_adjudication_is_sampled_ground_truth_not_a_complete_specification",
    "adjudicator_calibration_is_not_proven_by_contract_integrity",
    "false_rejection_and_non_authoritative_unknown_metrics_are_observational",
    "same_corpus_repairs_are_not_clean_first_pass_evidence_and_require_fresh_independent_"
    "readjudication_with_raw_metrics_preserved",
)


class QualificationCorpusManifest(TypedDict):
    """Sealed pre-run denominator and independent atom/case labels."""

    schema_version: int
    contract_kind: str
    provenance: dict[str, Any]
    eligible_atom_corpus: dict[str, Any]
    atom_labels: list[dict[str, Any]]
    content_sha256: str


class QualificationOutputAdjudication(TypedDict):
    """Post-run semantic judgments bound to exact accepted output content."""

    schema_version: int
    contract_kind: str
    qualification_manifest_sha256: str
    pending_run_sha256: str
    provenance: dict[str, Any]
    accepted_output_corpus: dict[str, Any]
    output_adjudications: list[dict[str, Any]]
    false_rejections: list[dict[str, Any]]
    content_sha256: str


class NoActionableEvidenceReceipt(TypedDict):
    """Independent certification that a sealed corpus contains no actionable case."""

    schema_version: int
    contract_kind: str
    qualification_manifest_sha256: str
    eligible_atom_corpus_sha256: str
    atom_labels_sha256: str
    provenance: dict[str, Any]
    content_sha256: str


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode()).hexdigest()


def _content_hash(document: Mapping[str, Any]) -> str:
    return _canonical_hash(
        {key: value for key, value in document.items() if key != "content_sha256"}
    )


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


def _eligible_atom_receipts(atoms: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    receipts: list[dict[str, str]] = []
    seen: set[str] = set()
    for atom in atoms:
        if atom_is_idea_originated(dict(atom)):
            continue
        atom_id = _text(atom.get("atom_id"))
        if atom_id is None:
            raise ValueError("qualification_eligible_atom_id_missing")
        if atom_id in seen:
            raise ValueError(f"qualification_eligible_atom_id_duplicate:{atom_id}")
        seen.add(atom_id)
        receipts.append(
            {
                "atom_id": atom_id,
                "atom_sha256": _canonical_hash(
                    immutable_atom_evidence_projection(atom)
                ),
            }
        )
    return sorted(receipts, key=lambda item: item["atom_id"])


def _accepted_output_receipts(
    accepted_outputs_by_kind: Mapping[str, Iterable[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    unknown_kinds = sorted(set(accepted_outputs_by_kind) - set(_OUTPUT_KINDS))
    if unknown_kinds:
        raise ValueError("qualification_output_kinds_invalid:" + ",".join(unknown_kinds))
    seen: set[tuple[str, str]] = set()
    for output_kind in _OUTPUT_KINDS:
        raw_outputs = accepted_outputs_by_kind.get(output_kind, ())
        for output in raw_outputs:
            output_sha256 = _canonical_hash(output)
            identity = (output_kind, output_sha256)
            if identity in seen:
                raise ValueError(
                    f"qualification_accepted_output_duplicate:{output_kind}:{output_sha256}"
                )
            seen.add(identity)
            receipts.append(
                {
                    "output_kind": output_kind,
                    "output_sha256": output_sha256,
                    "case_id": _text(output.get("case_id")),
                    "problem_id": _text(output.get("problem_id")),
                    "plan_revision_id": _text(output.get("plan_revision_id")),
                }
            )
    return sorted(receipts, key=lambda item: (item["output_kind"], item["output_sha256"]))


def qualification_output_causal_target(
    output_kind: str,
    output: Mapping[str, Any],
) -> dict[str, list[str]]:
    """Project the case identity from output content, without author provenance."""

    def strings(*values: Any) -> list[str]:
        return list(
            dict.fromkeys(
                normalized
                for value in values
                for candidate in (value if isinstance(value, list) else [value])
                for normalized in [_text(candidate)]
                if normalized is not None
            )
        )

    problem_ids = strings(
        output.get("problem_id"),
        output.get("canonical_problem_id"),
        output.get("case_member_problem_ids"),
        output.get("split_parent_problem_ids"),
    )
    if output_kind == "relation":
        problem_ids = strings(
            problem_ids,
            output.get("focus_id"),
            output.get("target_ids"),
            output.get("member_ids"),
            output.get("alias_target_id"),
        )
    case_ids = strings(
        output.get("case_id"),
        output.get("absorbed_case_ids"),
        output.get("split_from_case_id"),
    )
    expected_item_keys: list[str] = []
    problem_id = _text(output.get("problem_id"))
    if output_kind == "problem" and problem_id is not None:
        expected_item_keys = [f"problem:{problem_id}"]
    elif output_kind == "relation":
        focus_id = _text(output.get("focus_id"))
        if focus_id is not None:
            expected_item_keys = [f"problem:{focus_id}"]
    elif output_kind == "priority" and problem_id is not None:
        expected_item_keys = [f"priority_decision:{problem_id}"]
    elif output_kind == "research" and problem_id is not None:
        expected_item_keys = [f"research:{problem_id}"]
    elif output_kind == "option":
        expected_item_keys = strings(output.get("option_id"))
    elif output_kind == "selection":
        expected_item_keys = strings(output.get("selected_option_id"))
    elif output_kind in {"plan", "ticket"}:
        expected_item_keys = strings(output.get("plan_revision_id"))
    return {
        "problem_ids": problem_ids,
        "case_ids": case_ids,
        "evidence_atom_ids": strings(
            output.get("evidence_atom_ids"),
            output.get("supporting_atom_ids"),
        ),
        "expected_item_keys": expected_item_keys,
    }


def _accepted_output_causal_targets(
    accepted_outputs_by_kind: Mapping[str, Iterable[Mapping[str, Any]]],
) -> dict[str, dict[str, list[str]]]:
    return {
        f"{output_kind}:{_canonical_hash(output)}": qualification_output_causal_target(
            output_kind,
            output,
        )
        for output_kind in _OUTPUT_KINDS
        for output in accepted_outputs_by_kind.get(output_kind, ())
    }


def _materialize_outputs(
    accepted_outputs_by_kind: Mapping[str, Iterable[Mapping[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    unknown_kinds = sorted(set(accepted_outputs_by_kind) - set(_OUTPUT_KINDS))
    if unknown_kinds:
        raise ValueError("qualification_output_kinds_invalid:" + ",".join(unknown_kinds))
    return {
        kind: [dict(output) for output in accepted_outputs_by_kind.get(kind, ())]
        for kind in _OUTPUT_KINDS
    }


def _corpus_projection(receipts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    projected = [dict(receipt) for receipt in receipts]
    return {
        "count": len(projected),
        "sha256": _canonical_hash(projected),
        "receipts": projected,
    }


def _manifest_provenance(*, adjudicator: str, method: str) -> dict[str, Any]:
    if _text(adjudicator) is None:
        raise ValueError("qualification_adjudicator_missing")
    if _text(method) is None:
        raise ValueError("qualification_method_missing")
    return {
        "adjudicator": adjudicator.strip(),
        "method": method.strip(),
        "independent_from_pipeline": True,
        "labels_withheld_from_model_stages": True,
        "sealed_before_pipeline": True,
    }


def _post_run_provenance(*, adjudicator: str, method: str) -> dict[str, Any]:
    if _text(adjudicator) is None:
        raise ValueError("qualification_output_adjudicator_missing")
    if _text(method) is None:
        raise ValueError("qualification_output_method_missing")
    return {
        "adjudicator": adjudicator.strip(),
        "method": method.strip(),
        "independent_from_pipeline": True,
        "not_supplied_to_model_stages": True,
        "completed_after_pipeline": True,
    }


def build_qualification_corpus_manifest(
    *,
    atoms: Iterable[Mapping[str, Any]],
    atom_labels: Iterable[Mapping[str, Any]],
    adjudicator: str,
    method: str,
) -> QualificationCorpusManifest:
    """Build and validate a sealed, content-addressed pre-run denominator."""

    atoms_materialized = [dict(atom) for atom in atoms]
    manifest: dict[str, Any] = {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "contract_kind": "qualification_corpus_manifest",
        "provenance": _manifest_provenance(adjudicator=adjudicator, method=method),
        "eligible_atom_corpus": _corpus_projection(_eligible_atom_receipts(atoms_materialized)),
        "atom_labels": [dict(item) for item in atom_labels],
    }
    manifest["content_sha256"] = _content_hash(manifest)
    errors = qualification_manifest_errors(manifest, atoms=atoms_materialized)
    if errors:
        raise ValueError("qualification_manifest_invalid:" + ",".join(errors))
    return manifest  # type: ignore[return-value]


def build_qualification_output_adjudication(
    *,
    manifest: Mapping[str, Any],
    accepted_outputs_by_kind: Mapping[str, Iterable[Mapping[str, Any]]],
    output_adjudications: Iterable[Mapping[str, Any]],
    false_rejections: Iterable[Mapping[str, Any]] = (),
    pending_run_sha256: str,
    adjudicator: str,
    method: str,
) -> QualificationOutputAdjudication:
    """Build and validate post-run semantic judgments over exact output hashes."""

    outputs_materialized = _materialize_outputs(accepted_outputs_by_kind)
    adjudication: dict[str, Any] = {
        "schema_version": _OUTPUT_ADJUDICATION_SCHEMA_VERSION,
        "contract_kind": "qualification_output_adjudication",
        "qualification_manifest_sha256": manifest.get("content_sha256"),
        "pending_run_sha256": pending_run_sha256,
        "provenance": _post_run_provenance(adjudicator=adjudicator, method=method),
        "accepted_output_corpus": _corpus_projection(
            _accepted_output_receipts(outputs_materialized)
        ),
        "output_adjudications": [dict(item) for item in output_adjudications],
        "false_rejections": [dict(item) for item in false_rejections],
    }
    adjudication["content_sha256"] = _content_hash(adjudication)
    errors = qualification_output_adjudication_errors(
        adjudication,
        manifest=manifest,
        accepted_outputs_by_kind=outputs_materialized,
        expected_pending_run_sha256=pending_run_sha256,
    )
    if errors:
        raise ValueError("qualification_output_adjudication_invalid:" + ",".join(errors))
    return adjudication  # type: ignore[return-value]


def build_no_actionable_evidence_receipt(
    *,
    manifest: Mapping[str, Any],
    adjudicator: str,
    method: str,
) -> NoActionableEvidenceReceipt:
    """Certify that a completely labeled, sealed manifest has no actionable case."""

    labels_raw = manifest.get("atom_labels")
    labels = labels_raw if isinstance(labels_raw, list) else []
    if any(
        isinstance(label, Mapping) and _text(label.get("classification")) == "actionable"
        for label in labels
    ):
        raise ValueError("no_actionable_receipt_manifest_contains_actionable_label")
    corpus_raw = manifest.get("eligible_atom_corpus")
    corpus = corpus_raw if isinstance(corpus_raw, Mapping) else {}
    receipt: dict[str, Any] = {
        "schema_version": _NO_ACTIONABLE_RECEIPT_SCHEMA_VERSION,
        "contract_kind": "no_actionable_evidence_receipt",
        "qualification_manifest_sha256": manifest.get("content_sha256"),
        "eligible_atom_corpus_sha256": corpus.get("sha256"),
        "atom_labels_sha256": _canonical_hash(labels),
        "provenance": _manifest_provenance(adjudicator=adjudicator, method=method),
    }
    receipt["content_sha256"] = _content_hash(receipt)
    errors = no_actionable_evidence_receipt_errors(receipt, manifest=manifest)
    if errors:
        raise ValueError("no_actionable_receipt_invalid:" + ",".join(errors))
    return receipt  # type: ignore[return-value]


def _manifest_provenance_errors(raw: Any) -> list[str]:
    if not isinstance(raw, Mapping):
        return ["qualification_provenance_invalid"]
    errors: list[str] = []
    if _text(raw.get("adjudicator")) is None:
        errors.append("qualification_adjudicator_missing")
    if _text(raw.get("method")) is None:
        errors.append("qualification_method_missing")
    if raw.get("independent_from_pipeline") is not True:
        errors.append("qualification_independence_not_certified")
    if raw.get("labels_withheld_from_model_stages") is not True:
        errors.append("qualification_label_withholding_not_certified")
    if raw.get("sealed_before_pipeline") is not True:
        errors.append("qualification_pre_run_seal_not_certified")
    return errors


def _post_run_provenance_errors(raw: Any) -> list[str]:
    if not isinstance(raw, Mapping):
        return ["qualification_output_provenance_invalid"]
    errors: list[str] = []
    if _text(raw.get("adjudicator")) is None:
        errors.append("qualification_output_adjudicator_missing")
    if _text(raw.get("method")) is None:
        errors.append("qualification_output_method_missing")
    if raw.get("independent_from_pipeline") is not True:
        errors.append("qualification_output_independence_not_certified")
    if raw.get("not_supplied_to_model_stages") is not True:
        errors.append("qualification_output_stage_isolation_not_certified")
    if raw.get("completed_after_pipeline") is not True:
        errors.append("qualification_output_post_run_completion_not_certified")
    return errors


def _label_partition(
    manifest: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], set[str], list[str], list[str]]:
    errors: list[str] = []
    labels_raw = manifest.get("atom_labels")
    labels = (
        [item for item in labels_raw if isinstance(item, Mapping)]
        if isinstance(labels_raw, list)
        else []
    )
    if not isinstance(labels_raw, list) or len(labels) != len(labels_raw):
        errors.append("qualification_manifest_atom_labels_invalid")
    label_ids: set[str] = set()
    actionable_ids: set[str] = set()
    atom_ids: list[str] = []
    for index, label in enumerate(labels):
        label_id = _text(label.get("label_id"))
        classification = _text(label.get("classification"))
        raw_atom_ids = label.get("atom_ids")
        current_atom_ids = (
            [item.strip() for item in raw_atom_ids if isinstance(item, str) and item.strip()]
            if isinstance(raw_atom_ids, list)
            else []
        )
        if label_id is None:
            errors.append(f"qualification_atom_label_id_missing:{index}")
        elif label_id in label_ids:
            errors.append(f"qualification_atom_label_id_duplicate:{label_id}")
        else:
            label_ids.add(label_id)
            if classification == "actionable":
                actionable_ids.add(label_id)
        if classification not in _ATOM_CLASSIFICATIONS:
            errors.append(f"qualification_atom_label_classification_invalid:{label_id or index}")
        if not current_atom_ids or len(current_atom_ids) != len(set(current_atom_ids)):
            errors.append(f"qualification_atom_label_atom_ids_invalid:{label_id or index}")
        atom_ids.extend(current_atom_ids)
        if _text(label.get("rationale")) is None:
            errors.append(f"qualification_atom_label_rationale_missing:{label_id or index}")
    return labels, actionable_ids, atom_ids, errors


def qualification_manifest_errors(
    manifest: Any,
    *,
    atoms: Iterable[Mapping[str, Any]],
) -> list[str]:
    """Verify pre-run manifest integrity, provenance, and complete atom coverage."""

    if not isinstance(manifest, Mapping):
        return ["qualification_manifest_invalid"]
    errors: list[str] = []
    if manifest.get("schema_version") != _MANIFEST_SCHEMA_VERSION:
        errors.append("qualification_manifest_schema_invalid")
    if manifest.get("contract_kind") != "qualification_corpus_manifest":
        errors.append("qualification_manifest_kind_invalid")
    content_sha256 = manifest.get("content_sha256")
    if not _valid_sha256(content_sha256):
        errors.append("qualification_manifest_content_sha256_invalid")
    elif content_sha256 != _content_hash(manifest):
        errors.append("qualification_manifest_content_sha256_mismatch")
    errors.extend(_manifest_provenance_errors(manifest.get("provenance")))

    atoms_materialized = [dict(atom) for atom in atoms]
    try:
        expected_atoms = _eligible_atom_receipts(atoms_materialized)
    except ValueError as exc:
        errors.append(str(exc))
        return list(dict.fromkeys(errors))
    corpus_raw = manifest.get("eligible_atom_corpus")
    corpus = corpus_raw if isinstance(corpus_raw, Mapping) else {}
    if corpus != _corpus_projection(expected_atoms):
        errors.append("qualification_manifest_atom_corpus_mismatch")

    labels, _actionable_ids, labeled_atom_ids, partition_errors = _label_partition(manifest)
    errors.extend(partition_errors)
    expected_atom_ids = [receipt["atom_id"] for receipt in expected_atoms]
    if sorted(labeled_atom_ids) != sorted(expected_atom_ids):
        errors.append("qualification_manifest_atom_label_partition_incomplete")
    if len(labeled_atom_ids) != len(set(labeled_atom_ids)):
        errors.append("qualification_manifest_atom_label_partition_overlapping")
    del labels
    return list(dict.fromkeys(errors))


def qualification_output_adjudication_errors(
    adjudication: Any,
    *,
    manifest: Mapping[str, Any],
    accepted_outputs_by_kind: Mapping[str, Iterable[Mapping[str, Any]]],
    expected_pending_run_sha256: str | None = None,
) -> list[str]:
    """Verify post-run output bindings and independent quality judgments."""

    if not isinstance(adjudication, Mapping):
        return ["qualification_output_adjudication_invalid"]
    errors: list[str] = []
    if adjudication.get("schema_version") != _OUTPUT_ADJUDICATION_SCHEMA_VERSION:
        errors.append("qualification_output_adjudication_schema_invalid")
    if adjudication.get("contract_kind") != "qualification_output_adjudication":
        errors.append("qualification_output_adjudication_kind_invalid")
    content_sha256 = adjudication.get("content_sha256")
    if not _valid_sha256(content_sha256):
        errors.append("qualification_output_adjudication_content_sha256_invalid")
    elif content_sha256 != _content_hash(adjudication):
        errors.append("qualification_output_adjudication_content_sha256_mismatch")
    if adjudication.get("qualification_manifest_sha256") != manifest.get("content_sha256"):
        errors.append("qualification_output_adjudication_manifest_mismatch")
    pending_run_sha256 = adjudication.get("pending_run_sha256")
    if not _valid_sha256(pending_run_sha256):
        errors.append("qualification_output_adjudication_pending_run_hash_invalid")
    if (
        expected_pending_run_sha256 is not None
        and pending_run_sha256 != expected_pending_run_sha256
    ):
        errors.append("qualification_output_adjudication_pending_run_mismatch")
    errors.extend(_post_run_provenance_errors(adjudication.get("provenance")))

    try:
        outputs_materialized = _materialize_outputs(accepted_outputs_by_kind)
        expected_outputs = _accepted_output_receipts(outputs_materialized)
    except ValueError as exc:
        errors.append(str(exc))
        return list(dict.fromkeys(errors))
    corpus_raw = adjudication.get("accepted_output_corpus")
    corpus = corpus_raw if isinstance(corpus_raw, Mapping) else {}
    if corpus != _corpus_projection(expected_outputs):
        errors.append("qualification_output_adjudication_corpus_mismatch")

    _labels, actionable_ids, _atom_ids, partition_errors = _label_partition(manifest)
    errors.extend(partition_errors)
    items_raw = adjudication.get("output_adjudications")
    items = (
        [item for item in items_raw if isinstance(item, Mapping)]
        if isinstance(items_raw, list)
        else []
    )
    if not isinstance(items_raw, list) or len(items) != len(items_raw):
        errors.append("qualification_output_adjudications_invalid")
    adjudicated_ids: list[str] = []
    good_ticket_refs: set[str] = set()
    for index, item in enumerate(items):
        output_kind = _text(item.get("output_kind"))
        output_sha256 = _text(item.get("output_sha256"))
        quality = _text(item.get("quality"))
        repair_status = _text(item.get("repair_status"))
        correctability = _text(item.get("correctability"))
        bad_severity = _text(item.get("bad_severity"))
        bad_categories_raw = item.get("bad_categories")
        author_component_target_raw = item.get("author_component_target")
        if (
            author_component_target_raw is not None
            and _text(author_component_target_raw) is None
        ):
            errors.append(
                f"qualification_output_author_component_target_invalid:"
                f"{output_sha256 or index}"
            )
        bad_categories = (
            [
                category.strip()
                for category in bad_categories_raw
                if isinstance(category, str) and category.strip()
            ]
            if isinstance(bad_categories_raw, list)
            else []
        )
        refs_raw = item.get("actionable_label_ids")
        refs = (
            [ref.strip() for ref in refs_raw if isinstance(ref, str) and ref.strip()]
            if isinstance(refs_raw, list)
            else []
        )
        if output_kind not in _OUTPUT_KINDS:
            errors.append(f"qualification_output_kind_invalid:{output_kind or index}")
        if not _valid_sha256(output_sha256):
            errors.append(f"qualification_output_sha256_invalid:{index}")
        else:
            adjudicated_ids.append(f"{output_kind}:{output_sha256}")
        if quality not in _OUTPUT_QUALITIES:
            errors.append(f"qualification_output_quality_invalid:{output_sha256 or index}")
        if repair_status not in _REPAIR_STATUSES:
            errors.append(f"qualification_output_repair_status_invalid:{output_sha256 or index}")
        if correctability is not None and correctability not in _CORRECTABILITIES:
            errors.append(f"qualification_output_correctability_invalid:{output_sha256 or index}")
        if quality == "bad":
            if bad_severity not in _BAD_SEVERITIES:
                errors.append(f"qualification_bad_output_severity_invalid:{output_sha256 or index}")
            if not bad_categories or len(bad_categories) != len(set(bad_categories)):
                errors.append(
                    f"qualification_bad_output_categories_invalid:{output_sha256 or index}"
                )
        elif bad_severity is not None or bad_categories_raw is not None:
            errors.append(f"qualification_nonbad_output_has_bad_metadata:{output_sha256 or index}")
        if len(refs) != len(set(refs)) or any(ref not in actionable_ids for ref in refs):
            errors.append(f"qualification_output_actionable_refs_invalid:{output_sha256 or index}")
        if output_kind == "ticket" and quality == "good":
            good_ticket_refs.update(refs)
        if quality == "good" and not refs:
            errors.append(
                f"qualification_good_output_without_actionable_ref:{output_sha256 or index}"
            )
        if _text(item.get("rationale")) is None:
            errors.append(f"qualification_output_rationale_missing:{output_sha256 or index}")
    expected_ids = [
        f"{receipt['output_kind']}:{receipt['output_sha256']}" for receipt in expected_outputs
    ]
    if sorted(adjudicated_ids) != sorted(expected_ids):
        errors.append("qualification_output_adjudication_partition_incomplete")
    if len(adjudicated_ids) != len(set(adjudicated_ids)):
        errors.append("qualification_output_adjudication_partition_overlapping")

    rejections_raw = adjudication.get("false_rejections")
    rejections = (
        [item for item in rejections_raw if isinstance(item, Mapping)]
        if isinstance(rejections_raw, list)
        else []
    )
    if not isinstance(rejections_raw, list) or len(rejections) != len(rejections_raw):
        errors.append("qualification_false_rejections_invalid")
    rejected_ids: set[str] = set()
    for index, item in enumerate(rejections):
        label_id = _text(item.get("label_id"))
        if label_id not in actionable_ids:
            errors.append(f"qualification_false_rejection_ref_invalid:{label_id or index}")
        elif label_id in rejected_ids:
            errors.append(f"qualification_false_rejection_duplicate:{label_id}")
        elif label_id in good_ticket_refs:
            errors.append(f"qualification_false_rejection_also_recovered_by_good_ticket:{label_id}")
        else:
            rejected_ids.add(label_id)
        if _text(item.get("rationale")) is None:
            errors.append(f"qualification_false_rejection_rationale_missing:{label_id or index}")
    return list(dict.fromkeys(errors))


def no_actionable_evidence_receipt_errors(
    receipt: Any,
    *,
    manifest: Mapping[str, Any],
) -> list[str]:
    """Verify a clean-corpus receipt against the exact sealed manifest."""

    if not isinstance(receipt, Mapping):
        return ["no_actionable_evidence_receipt_invalid"]
    errors: list[str] = []
    if receipt.get("schema_version") != _NO_ACTIONABLE_RECEIPT_SCHEMA_VERSION:
        errors.append("no_actionable_evidence_receipt_schema_invalid")
    if receipt.get("contract_kind") != "no_actionable_evidence_receipt":
        errors.append("no_actionable_evidence_receipt_kind_invalid")
    content_sha256 = receipt.get("content_sha256")
    if not _valid_sha256(content_sha256):
        errors.append("no_actionable_evidence_receipt_content_sha256_invalid")
    elif content_sha256 != _content_hash(receipt):
        errors.append("no_actionable_evidence_receipt_content_sha256_mismatch")
    errors.extend(_manifest_provenance_errors(receipt.get("provenance")))
    if receipt.get("qualification_manifest_sha256") != manifest.get("content_sha256"):
        errors.append("no_actionable_evidence_receipt_manifest_mismatch")
    corpus_raw = manifest.get("eligible_atom_corpus")
    corpus = corpus_raw if isinstance(corpus_raw, Mapping) else {}
    if receipt.get("eligible_atom_corpus_sha256") != corpus.get("sha256"):
        errors.append("no_actionable_evidence_receipt_atom_corpus_mismatch")
    labels_raw = manifest.get("atom_labels")
    labels = labels_raw if isinstance(labels_raw, list) else []
    if receipt.get("atom_labels_sha256") != _canonical_hash(labels):
        errors.append("no_actionable_evidence_receipt_atom_labels_mismatch")
    if any(
        isinstance(label, Mapping) and _text(label.get("classification")) == "actionable"
        for label in labels
    ):
        errors.append("no_actionable_evidence_receipt_actionable_label_present")
    return list(dict.fromkeys(errors))


def _rate(numerator: int | None, denominator: int | None) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": (
            None
            if numerator is None or denominator is None or denominator == 0
            else numerator / denominator
        ),
    }


_CORRECTION_RESTART_STAGES: Mapping[str, tuple[str, ...]] = {
    "problem": (
        "problem_mining",
        "problem_prioritization",
        "repro_research",
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
        "ticket_assembly",
    ),
    "relation": (
        "problem_mining",
        "problem_prioritization",
        "repro_research",
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
        "ticket_assembly",
    ),
    "priority": (
        "problem_prioritization",
        "repro_research",
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
        "ticket_assembly",
    ),
    "research": (
        "repro_research",
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
        "ticket_assembly",
    ),
    "option": (
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
        "ticket_assembly",
    ),
    "selection": (
        "solution_selection",
        "implementation_planning",
        "ticket_assembly",
    ),
    "plan": ("implementation_planning", "ticket_assembly"),
    "ticket": ("implementation_planning", "ticket_assembly"),
}


def _route_causal_target(
    provenance: Mapping[str, Any] | None,
    *,
    actionable_label_ids: Sequence[Any],
) -> dict[str, list[str]]:
    """Retain what the finding is about separately from who can correct it."""

    embedded_raw = provenance.get("causal_target") if isinstance(provenance, Mapping) else None
    embedded = embedded_raw if isinstance(embedded_raw, Mapping) else {}

    def strings(field: str, *fallback: Any) -> list[str]:
        raw = embedded.get(field)
        values = raw if isinstance(raw, list) else [*fallback]
        return list(
            dict.fromkeys(
                normalized
                for value in values
                for normalized in [_text(value)]
                if normalized is not None
            )
        )

    return {
        "problem_ids": strings(
            "problem_ids",
            provenance.get("problem_id") if isinstance(provenance, Mapping) else None,
            *(
                provenance.get("relation_review_focus_ids", [])
                if isinstance(provenance, Mapping)
                and isinstance(provenance.get("relation_review_focus_ids"), list)
                else []
            ),
        ),
        "case_ids": strings(
            "case_ids",
            provenance.get("case_id") if isinstance(provenance, Mapping) else None,
        ),
        "evidence_atom_ids": strings(
            "evidence_atom_ids",
            *(
                provenance.get("evidence_atom_ids", [])
                if isinstance(provenance, Mapping)
                and isinstance(provenance.get("evidence_atom_ids"), list)
                else []
            ),
        ),
        "actionable_label_ids": list(
            dict.fromkeys(
                value.strip()
                for value in actionable_label_ids
                if isinstance(value, str) and value.strip()
            )
        ),
        "expected_item_keys": strings("expected_item_keys"),
    }


def _false_rejection_route(
    *,
    label_id: str,
    rejection: Mapping[str, Any],
    provenance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    causal_target = _route_causal_target(
        provenance,
        actionable_label_ids=[label_id],
    )
    if not causal_target["expected_item_keys"]:
        causal_target["expected_item_keys"] = [
            "atom:" + atom_id for atom_id in causal_target["evidence_atom_ids"]
        ]
    stage = (
        _text(provenance.get("authoring_stage"))
        if isinstance(provenance, Mapping)
        else None
    ) or "problem_mining"
    downstream_raw = (
        provenance.get("rerun_downstream_stages")
        if isinstance(provenance, Mapping)
        else None
    )
    downstream = (
        [item.strip() for item in downstream_raw if isinstance(item, str) and item.strip()]
        if isinstance(downstream_raw, list)
        else [
            stage,
            "problem_prioritization",
            "repro_research",
            "solution_optioning",
            "solution_selection",
            "implementation_planning",
            "ticket_assembly",
        ]
    )
    if not downstream or downstream[0] != stage:
        downstream = [stage, *[item for item in downstream if item != stage]]
    session_id = (
        _text(provenance.get("agent_session_id"))
        if isinstance(provenance, Mapping)
        else None
    )
    workspace_dir = (
        _text(provenance.get("workspace_dir"))
        if isinstance(provenance, Mapping)
        else None
    )
    continuity_verified = bool(
        isinstance(provenance, Mapping)
        and provenance.get("exact_session_continuation") is True
        and provenance.get("workspace_continuity_verified") is True
        and session_id is not None
        and workspace_dir is not None
    )
    correctability = _text(rejection.get("correctability")) or "unknown"
    if correctability == "uncorrectable":
        route_status = "uncorrectable"
    elif continuity_verified:
        route_status = "same_author_resume"
    else:
        route_status = "author_provenance_unavailable"
    route: dict[str, Any] = {
        "schema_version": 1,
        "feedback_kind": "false_rejection",
        "authoring_stage": stage,
        "target_identity": (
            f"actionable_label:{label_id}:"
            + _canonical_hash(
                {
                    "component": (
                        provenance.get("author_component_id")
                        if isinstance(provenance, Mapping)
                        else None
                    ),
                    "evidence_atom_ids": causal_target["evidence_atom_ids"],
                }
            )[:16]
        ),
        "output_kind": None,
        "output_sha256": None,
        "quality": "bad",
        "bad_severity": _text(rejection.get("bad_severity")) or "noncritical",
        "bad_categories": list(rejection.get("bad_categories") or ["false_rejection"]),
        "rationale": _text(rejection.get("rationale")),
        "actionable_label_ids": [label_id],
        "correctability": correctability,
        "route_status": route_status,
        "agent_session_id": session_id,
        "workspace_dir": workspace_dir,
        "author_attempt_identity": (
            provenance.get("author_attempt_identity")
            if isinstance(provenance, Mapping)
            else None
        ),
        "author_provenance": dict(provenance) if isinstance(provenance, Mapping) else None,
        "causal_target": causal_target,
        "restart_from_stage": stage,
        "rerun_downstream_stages": downstream,
        "consumption_status": "pending_orchestration",
        "consumption_receipt": None,
    }
    route["route_sha256"] = _canonical_hash(route)
    return route


def _qualification_correction_routes(
    adjudications: Sequence[Mapping[str, Any]],
    *,
    output_author_provenance: Mapping[str, Mapping[str, Any]] | None,
    output_causal_targets: Mapping[str, Mapping[str, Any]] | None = None,
    false_rejections: Sequence[Mapping[str, Any]] = (),
    false_rejection_author_provenance: Mapping[
        str,
        Mapping[str, Any] | Sequence[Mapping[str, Any]],
    ]
    | None = None,
) -> list[dict[str, Any]]:
    """Route semantic misses back to their exact author when continuation is proven."""

    provenance_index = output_author_provenance or {}
    causal_target_index = output_causal_targets or {}
    routes: list[dict[str, Any]] = []
    for item in adjudications:
        quality = _text(item.get("quality"))
        if quality not in {"bad", "unknown"}:
            continue
        output_kind = _text(item.get("output_kind"))
        output_sha256 = _text(item.get("output_sha256"))
        if output_kind not in _OUTPUT_KINDS or not _valid_sha256(output_sha256):
            continue
        identity = f"{output_kind}:{output_sha256}"
        provenance_raw = provenance_index.get(identity)
        provenance_entry = (
            dict(provenance_raw) if isinstance(provenance_raw, Mapping) else None
        )
        direct_causal_target = causal_target_index.get(identity)
        causal_target_source = (
            {"causal_target": direct_causal_target}
            if isinstance(direct_causal_target, Mapping)
            else provenance_entry
        )
        causal_target = _route_causal_target(
            causal_target_source,
            actionable_label_ids=list(item.get("actionable_label_ids") or []),
        )
        requested_component = _text(item.get("author_component_target"))
        component_frontiers_raw = (
            provenance_entry.get("author_component_frontiers")
            if isinstance(provenance_entry, Mapping)
            else None
        )
        component_frontiers = (
            [
                dict(frontier)
                for frontier in component_frontiers_raw
                if isinstance(frontier, Mapping)
                and _text(frontier.get("component_id")) is not None
                and isinstance(frontier.get("author_provenance"), Mapping)
            ]
            if isinstance(component_frontiers_raw, list)
            else []
        )
        available_components = [
            str(frontier["component_id"]) for frontier in component_frontiers
        ]
        selected_component = requested_component
        component_resolution_status = "not_composite"
        if component_frontiers:
            if selected_component is None:
                selected_component = _text(
                    provenance_entry.get("default_author_component_target")
                )
                component_resolution_status = "defaulted_to_output_owner"
            matching_frontier = next(
                (
                    frontier
                    for frontier in component_frontiers
                    if _text(frontier.get("component_id")) == selected_component
                ),
                None,
            )
            if isinstance(matching_frontier, Mapping):
                provenance = dict(matching_frontier["author_provenance"])
                component_resolution_status = (
                    "explicit_component"
                    if requested_component is not None
                    else component_resolution_status
                )
            else:
                # An unknown hint is retained as an unrouteable ambiguity. Never fall
                # back to a different author and claim the finding was delivered.
                provenance = None
                component_resolution_status = "invalid_component_target"
        else:
            provenance = provenance_entry
        session_id = (
            _text(provenance.get("agent_session_id")) if isinstance(provenance, Mapping) else None
        )
        continuity_verified = bool(
            isinstance(provenance, Mapping)
            and provenance.get("exact_session_continuation") is True
            and provenance.get("workspace_continuity_verified") is True
            and session_id is not None
            and _text(provenance.get("workspace_dir")) is not None
        )
        correctability = _text(item.get("correctability")) or "unknown"
        if correctability == "uncorrectable":
            route_status = "uncorrectable"
        elif continuity_verified:
            route_status = "same_author_resume"
        else:
            route_status = "author_provenance_unavailable"
        restart_stages = list(_CORRECTION_RESTART_STAGES[output_kind])
        route: dict[str, Any] = {
            "schema_version": 1,
            "feedback_kind": "accepted_output_quality",
            "authoring_stage": restart_stages[0],
            "target_identity": identity,
            "output_kind": output_kind,
            "output_sha256": output_sha256,
            "quality": quality,
            "bad_severity": _text(item.get("bad_severity")),
            "bad_categories": list(item.get("bad_categories") or []),
            "rationale": _text(item.get("rationale")),
            "actionable_label_ids": list(item.get("actionable_label_ids") or []),
            "correctability": correctability,
            "route_status": route_status,
            "agent_session_id": session_id,
            "workspace_dir": (
                _text(provenance.get("workspace_dir"))
                if isinstance(provenance, Mapping)
                else None
            ),
            "author_attempt_identity": (
                provenance.get("author_attempt_identity")
                if isinstance(provenance, Mapping)
                else None
            ),
            "author_provenance": provenance,
            "causal_target": causal_target,
            "requested_author_component_target": requested_component,
            "selected_author_component_target": (
                selected_component if provenance is not None else None
            ),
            "available_author_component_targets": available_components,
            "author_component_resolution_status": component_resolution_status,
            "restart_from_stage": restart_stages[0],
            "rerun_downstream_stages": restart_stages,
            "consumption_status": "pending_orchestration",
            "consumption_receipt": None,
        }
        route["route_sha256"] = _canonical_hash(route)
        routes.append(route)

    # A bad/unknown accepted output already routes every actionable label it cites to
    # the deepest author that produced it.  Emit a separate false-rejection route only
    # for source groups not covered by one of those deeper output routes.
    labels_already_routed = {
        label_id
        for route in routes
        for label_id in route.get("actionable_label_ids", [])
        if isinstance(label_id, str) and label_id.strip()
    }
    rejection_provenance_index = false_rejection_author_provenance or {}
    for rejection in false_rejections:
        label_id = _text(rejection.get("label_id"))
        if label_id is None or label_id in labels_already_routed:
            continue
        provenance_raw = rejection_provenance_index.get(label_id)
        provenances = (
            [dict(item) for item in provenance_raw if isinstance(item, Mapping)]
            if isinstance(provenance_raw, Sequence)
            and not isinstance(provenance_raw, (str, bytes, Mapping))
            else [dict(provenance_raw)]
            if isinstance(provenance_raw, Mapping)
            else [None]
        )
        routes.extend(
            _false_rejection_route(
                label_id=label_id,
                rejection=rejection,
                provenance=provenance,
            )
            for provenance in provenances
        )
    return sorted(
        routes,
        key=lambda item: (
            str(item.get("authoring_stage") or ""),
            str(item.get("output_kind") or ""),
            str(item.get("output_sha256") or item.get("target_identity") or ""),
        ),
    )


def _empty_kind_metrics(accepted: int) -> dict[str, Any]:
    return {
        "counts": {
            "accepted": accepted,
            "good": None,
            "bad": None,
            "unknown": None,
            "critical_bad": None,
            "noncritical_bad": None,
            "repaired": None,
            "repair_unknown": None,
        },
        "rates": {
            "quality_coverage": _rate(None, accepted),
            "good_among_adjudicated": _rate(None, None),
            "repair_coverage": _rate(None, accepted),
            "repair_among_known": _rate(None, None),
        },
        "good_to_bad_ratio": {
            "good": None,
            "bad": None,
            "value": None,
            "status": "unknown",
        },
    }


def _empty_metrics(
    *,
    eligible_atoms: int,
    accepted_outputs_by_kind: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    accepted_by_kind = {kind: len(accepted_outputs_by_kind.get(kind, ())) for kind in _OUTPUT_KINDS}
    accepted_outputs = sum(accepted_by_kind.values())
    accepted_tickets = accepted_by_kind["ticket"]
    by_kind = {kind: _empty_kind_metrics(accepted_by_kind[kind]) for kind in _OUTPUT_KINDS}
    return {
        "counts": {
            "eligible_atoms": eligible_atoms,
            "independently_labeled_atoms": None,
            "actionable_cases": None,
            "derived_only_actionable_groups_excluded": None,
            "non_actionable_groups": None,
            "ambiguous_groups": None,
            "unknown_groups": None,
            "accepted_outputs": accepted_outputs,
            "accepted_end_to_end_tickets": accepted_tickets,
            "accepted_good": None,
            "accepted_bad": None,
            "accepted_unknown": None,
            "accepted_critical_bad": None,
            "accepted_noncritical_bad": None,
            "recovered_actionable_cases": None,
            "accepted_ticket_referenced_actionable_cases": None,
            "unrecovered_actionable_cases": None,
            "false_rejected_good": None,
            "undispositioned_actionable_cases": None,
            "repaired": None,
            "repair_unknown": None,
            "zero_output": int(accepted_tickets == 0),
            "zero_accepted_artifacts": int(accepted_outputs == 0),
            "actionable_zero_output": None,
            "exhausted_corpus": None,
            "positive_qualifying_corpus": None,
        },
        "rates": {
            "actionable_recovery": _rate(None, None),
            "false_rejected_good_share_of_actionable": _rate(None, None),
            "recovered_to_missed": _rate(None, None),
            "accepted_quality_coverage": _rate(None, accepted_outputs),
            "accepted_good_among_adjudicated": _rate(None, None),
            "repair_coverage": _rate(None, accepted_outputs),
            "repair_among_known": _rate(None, None),
        },
        "good_to_bad_ratio": {
            "good": None,
            "bad": None,
            "value": None,
            "status": "unknown",
        },
        "by_kind": by_kind,
        "end_to_end": by_kind["ticket"],
        "unknowns": ["independent_qualification_unavailable"],
    }


def evaluate_independent_qualification(
    *,
    atoms: Iterable[Mapping[str, Any]],
    accepted_outputs_by_kind: Mapping[str, Iterable[Mapping[str, Any]]],
    manifest: Any = None,
    qualification_manifest_sha256_expected: str | None = None,
    qualification_manifest_sha256_observed: str | None = None,
    output_adjudication: Any = None,
    output_adjudication_sha256_pre_run: str | None = None,
    output_adjudication_sha256_post_run: str | None = None,
    pending_run_sha256: str | None = None,
    no_actionable_receipt: Any = None,
    output_author_provenance: Mapping[str, Mapping[str, Any]] | None = None,
    false_rejection_author_provenance: Mapping[
        str,
        Mapping[str, Any] | Sequence[Mapping[str, Any]],
    ]
    | None = None,
    same_corpus_feedback_exposed: bool = False,
    correction_metrics: Mapping[str, Any] | None = None,
    positive_throughput_required: bool,
    minimum_good_ticket_count: int = 1,
    minimum_good_to_bad_ratio: float = 2.0,
    minimum_recovered_to_missed_ratio: float = 2.0,
    require_zero_unknown_authoritative_tickets: bool = True,
) -> dict[str, Any]:
    """Join held-out actionability and post-run output quality without circularity."""

    if not isinstance(same_corpus_feedback_exposed, bool):
        raise ValueError("same_corpus_feedback_exposed must be a boolean")
    normalized_correction_metrics = (
        {
            str(key): item
            for key, item in correction_metrics.items()
            if isinstance(key, str)
            and key
            and isinstance(item, int)
            and not isinstance(item, bool)
            and item >= 0
        }
        if isinstance(correction_metrics, Mapping)
        else {}
    )

    if (
        isinstance(minimum_good_ticket_count, bool)
        or not isinstance(minimum_good_ticket_count, int)
        or minimum_good_ticket_count < 1
    ):
        raise ValueError("minimum_good_ticket_count must be a positive integer")
    if (
        isinstance(minimum_good_to_bad_ratio, bool)
        or not isinstance(minimum_good_to_bad_ratio, (int, float))
        or float(minimum_good_to_bad_ratio) <= 1.0
    ):
        raise ValueError("minimum_good_to_bad_ratio must be greater than 1")
    if (
        isinstance(minimum_recovered_to_missed_ratio, bool)
        or not isinstance(minimum_recovered_to_missed_ratio, (int, float))
        or float(minimum_recovered_to_missed_ratio) <= 1.0
    ):
        raise ValueError("minimum_recovered_to_missed_ratio must be greater than 1")
    if not isinstance(require_zero_unknown_authoritative_tickets, bool):
        raise ValueError("require_zero_unknown_authoritative_tickets must be a boolean")
    policy = {
        "positive_throughput_required": positive_throughput_required,
        "minimum_good_ticket_count": minimum_good_ticket_count,
        "minimum_good_to_bad_ratio": float(minimum_good_to_bad_ratio),
        "minimum_recovered_to_missed_ratio": float(minimum_recovered_to_missed_ratio),
        "maximum_critical_bad_tickets": 0,
        "require_zero_unknown_authoritative_tickets": (require_zero_unknown_authoritative_tickets),
    }
    atoms_materialized = [dict(atom) for atom in atoms]
    try:
        outputs_materialized = _materialize_outputs(accepted_outputs_by_kind)
        eligible_count = len(_eligible_atom_receipts(atoms_materialized))
        output_receipts = _accepted_output_receipts(outputs_materialized)
    except ValueError as exc:
        metrics = _empty_metrics(
            eligible_atoms=0,
            accepted_outputs_by_kind={kind: [] for kind in _OUTPUT_KINDS},
        )
        return {
            "schema_version": 1,
            "qualification_class": "unqualified",
            "policy": policy,
            "limitations": list(_QUALIFICATION_LIMITATIONS),
            "status": "invalid",
            "failures": [str(exc)],
            "correction_routes": [],
            "correction_routing_status": "not_evaluated",
            "stability_sha256": _canonical_hash({"status": "invalid", "policy": policy}),
            "basis_sha256": _canonical_hash(
                {
                    "manifest": None,
                    "manifest_sha256_expected": qualification_manifest_sha256_expected,
                    "manifest_sha256_observed": qualification_manifest_sha256_observed,
                    "output": None,
                    "output_sha256_pre_run": output_adjudication_sha256_pre_run,
                    "output_sha256_post_run": output_adjudication_sha256_post_run,
                    "pending_run_sha256": pending_run_sha256,
                    "receipt": None,
                }
            ),
            **metrics,
        }

    independent_atom_ids = {
        atom_id
        for atom in atoms_materialized
        if atom_is_independent_problem_evidence(atom)
        for atom_id in [_text(atom.get("atom_id"))]
        if atom_id is not None
    }
    unqualified_stability_sha256 = _canonical_hash(
        {
            "source_atom_ids": sorted(independent_atom_ids),
            "policy": policy,
            "status": "unqualified",
        }
    )

    basis = {
        "manifest": manifest.get("content_sha256") if isinstance(manifest, Mapping) else None,
        "manifest_sha256_expected": qualification_manifest_sha256_expected,
        "manifest_sha256_observed": qualification_manifest_sha256_observed,
        "output_adjudication": (
            output_adjudication.get("content_sha256")
            if isinstance(output_adjudication, Mapping)
            else None
        ),
        "output_adjudication_sha256_pre_run": output_adjudication_sha256_pre_run,
        "output_adjudication_sha256_post_run": output_adjudication_sha256_post_run,
        "pending_run_sha256": pending_run_sha256,
        "no_actionable_receipt": (
            no_actionable_receipt.get("content_sha256")
            if isinstance(no_actionable_receipt, Mapping)
            else None
        ),
    }
    if manifest is None:
        failures: list[str] = []
        if qualification_manifest_sha256_expected is not None:
            failures.append("qualification_manifest_missing_after_pre_run_anchor")
        elif positive_throughput_required:
            failures.append("independent_qualification_manifest_missing")
        metrics = _empty_metrics(
            eligible_atoms=eligible_count,
            accepted_outputs_by_kind=outputs_materialized,
        )
        return {
            "schema_version": 1,
            "qualification_class": "unqualified",
            "policy": policy,
            "limitations": list(_QUALIFICATION_LIMITATIONS),
            "status": "missing",
            "failures": failures,
            "correction_routes": [],
            "correction_routing_status": "not_evaluated",
            "stability_sha256": unqualified_stability_sha256,
            "basis_sha256": _canonical_hash(basis),
            **metrics,
        }

    anchor_errors: list[str] = []
    if not _valid_sha256(qualification_manifest_sha256_expected):
        anchor_errors.append("qualification_manifest_pre_run_anchor_missing_or_invalid")
    if not _valid_sha256(qualification_manifest_sha256_observed):
        anchor_errors.append("qualification_manifest_post_run_hash_missing_or_invalid")
    elif (
        _valid_sha256(qualification_manifest_sha256_expected)
        and qualification_manifest_sha256_observed != qualification_manifest_sha256_expected
    ):
        anchor_errors.append("qualification_manifest_changed_after_pre_run_anchor")
    manifest_errors = [
        *anchor_errors,
        *qualification_manifest_errors(manifest, atoms=atoms_materialized),
    ]
    if manifest_errors:
        metrics = _empty_metrics(
            eligible_atoms=eligible_count,
            accepted_outputs_by_kind=outputs_materialized,
        )
        return {
            "schema_version": 1,
            "qualification_class": "unqualified",
            "policy": policy,
            "limitations": list(_QUALIFICATION_LIMITATIONS),
            "status": "invalid",
            "failures": manifest_errors,
            "correction_routes": [],
            "correction_routing_status": "not_evaluated",
            "stability_sha256": unqualified_stability_sha256,
            "basis_sha256": _canonical_hash(basis),
            **metrics,
        }

    labels, all_actionable_ids, _atom_ids, partition_errors = _label_partition(manifest)
    # The manifest validator above proved partition validity.
    del partition_errors
    actionable_ids = {
        str(label["label_id"])
        for label in labels
        if label.get("classification") == "actionable"
        and any(atom_id in independent_atom_ids for atom_id in label.get("atom_ids", []))
    }
    derived_only_actionable_ids = all_actionable_ids - actionable_ids
    actionable_count = len(actionable_ids)
    source_label_by_id = {
        str(label["label_id"]): {
            "classification": label.get("classification"),
            "source_atom_ids": sorted(
                atom_id
                for atom_id in label.get("atom_ids", [])
                if atom_id in independent_atom_ids
            ),
        }
        for label in labels
        if any(atom_id in independent_atom_ids for atom_id in label.get("atom_ids", []))
    }
    source_group_key_by_label_id = {
        label_id: _canonical_hash(projection) for label_id, projection in source_label_by_id.items()
    }
    source_label_projection = sorted(source_label_by_id.values(), key=_canonical_hash)
    output_count = len(output_receipts)
    ticket_count = sum(receipt["output_kind"] == "ticket" for receipt in output_receipts)
    receipt_errors: list[str] = []
    exhausted = False
    if actionable_count == 0:
        if no_actionable_receipt is None:
            receipt_errors.append("no_actionable_evidence_receipt_missing")
        else:
            receipt_errors.extend(
                no_actionable_evidence_receipt_errors(
                    no_actionable_receipt,
                    manifest=manifest,
                )
            )
            # Exhaustion describes the independently sealed source corpus, not the
            # pipeline's behavior. Model outputs over a certified clean corpus are
            # false-positive throughput to adjudicate; they must not erase the clean
            # denominator or hide a nonempty exported backlog.
            exhausted = not receipt_errors
    elif no_actionable_receipt is not None:
        receipt_errors.append("no_actionable_evidence_receipt_unexpected_for_actionable_corpus")

    output_errors: list[str] = []
    if output_adjudication is None:
        if output_count > 0 or (positive_throughput_required and actionable_count > 0):
            output_errors.append("qualification_output_adjudication_missing")
    else:
        if not _valid_sha256(pending_run_sha256):
            output_errors.append("qualification_pending_run_hash_missing_or_invalid")
        if output_adjudication_sha256_pre_run is not None and not _valid_sha256(
            output_adjudication_sha256_pre_run
        ):
            output_errors.append("qualification_output_adjudication_pre_run_hash_invalid")
        if not _valid_sha256(output_adjudication_sha256_post_run):
            output_errors.append(
                "qualification_output_adjudication_post_run_hash_missing_or_invalid"
            )
        elif (
            _valid_sha256(output_adjudication_sha256_pre_run)
            and output_adjudication_sha256_post_run == output_adjudication_sha256_pre_run
        ):
            output_errors.append("qualification_output_adjudication_not_fresh_for_materialized_run")
        output_errors.extend(
            qualification_output_adjudication_errors(
                output_adjudication,
                manifest=manifest,
                accepted_outputs_by_kind=outputs_materialized,
                expected_pending_run_sha256=pending_run_sha256,
            )
        )
    if output_errors:
        metrics = _empty_metrics(
            eligible_atoms=eligible_count,
            accepted_outputs_by_kind=outputs_materialized,
        )
        metrics["counts"].update(
            {
                "independently_labeled_atoms": sum(
                    len(label.get("atom_ids", [])) for label in labels
                ),
                "actionable_cases": actionable_count,
                "derived_only_actionable_groups_excluded": len(derived_only_actionable_ids),
                "non_actionable_groups": sum(
                    label.get("classification") == "non_actionable" for label in labels
                ),
                "ambiguous_groups": sum(
                    label.get("classification") == "ambiguous" for label in labels
                ),
                "unknown_groups": sum(label.get("classification") == "unknown" for label in labels),
                "actionable_zero_output": int(actionable_count > 0 and ticket_count == 0),
                "exhausted_corpus": int(exhausted),
            }
        )
        metrics["rates"]["actionable_recovery"] = _rate(None, actionable_count)
        metrics["rates"]["false_rejected_good_share_of_actionable"] = _rate(
            None,
            actionable_count,
        )
        metrics["unknowns"] = [
            "output_quality_adjudication_invalid",
            *(
                ["ambiguous_atom_groups_present"]
                if any(label.get("classification") == "ambiguous" for label in labels)
                else []
            ),
            *(
                ["unknown_atom_groups_present"]
                if any(label.get("classification") == "unknown" for label in labels)
                else []
            ),
        ]
        failures = [*receipt_errors, *output_errors]
        if positive_throughput_required and actionable_count > 0 and ticket_count == 0:
            failures.append("independent_qualification_actionable_zero_output")
        return {
            "schema_version": 1,
            "qualification_class": "unqualified",
            "policy": policy,
            "limitations": list(_QUALIFICATION_LIMITATIONS),
            "status": "invalid",
            "failures": list(dict.fromkeys(failures)),
            "correction_routes": [],
            "correction_routing_status": "not_evaluated",
            "stability_sha256": _canonical_hash(
                {
                    "source_labels": source_label_projection,
                    "policy": policy,
                    "status": "output_adjudication_invalid",
                }
            ),
            "basis_sha256": _canonical_hash(basis),
            **metrics,
        }

    adjudications = (
        [
            item
            for item in output_adjudication.get("output_adjudications", [])
            if isinstance(item, Mapping)
        ]
        if isinstance(output_adjudication, Mapping)
        else []
    )
    false_rejections = (
        [
            item
            for item in output_adjudication.get("false_rejections", [])
            if isinstance(item, Mapping)
        ]
        if isinstance(output_adjudication, Mapping)
        else []
    )
    accepted_good = sum(item.get("quality") == "good" for item in adjudications)
    accepted_bad = sum(item.get("quality") == "bad" for item in adjudications)
    accepted_unknown = sum(item.get("quality") == "unknown" for item in adjudications)
    accepted_critical_bad = sum(
        item.get("quality") == "bad" and item.get("bad_severity") == "critical"
        for item in adjudications
    )
    accepted_noncritical_bad = sum(
        item.get("quality") == "bad" and item.get("bad_severity") == "noncritical"
        for item in adjudications
    )
    ticket_adjudications = [item for item in adjudications if item.get("output_kind") == "ticket"]
    ticket_good = sum(item.get("quality") == "good" for item in ticket_adjudications)
    ticket_bad = sum(item.get("quality") == "bad" for item in ticket_adjudications)
    ticket_unknown = sum(item.get("quality") == "unknown" for item in ticket_adjudications)
    ticket_critical_bad = sum(
        item.get("quality") == "bad" and item.get("bad_severity") == "critical"
        for item in ticket_adjudications
    )
    accepted_ticket_ref_ids = {
        str(label_id)
        for item in ticket_adjudications
        for label_id in item.get("actionable_label_ids", [])
        if isinstance(label_id, str) and label_id in actionable_ids
    }
    recovered_ids = {
        str(label_id)
        for item in ticket_adjudications
        if item.get("quality") == "good"
        for label_id in item.get("actionable_label_ids", [])
        if isinstance(label_id, str) and label_id in actionable_ids
    }
    false_rejected_ids = {
        str(item["label_id"])
        for item in false_rejections
        if isinstance(item.get("label_id"), str) and item.get("label_id") in actionable_ids
    }
    missed_ids = actionable_ids - recovered_ids
    undispositioned_ids = missed_ids - false_rejected_ids
    unrecovered_feedback = [dict(item) for item in false_rejections]
    unrecovered_feedback.extend(
        {
            "label_id": label_id,
            "rationale": (
                "No good authoritative ticket recovered this independently actionable "
                "source group, and post-run adjudication supplied no explicit disposition."
            ),
            "correctability": "unknown",
            "bad_severity": "noncritical",
            "bad_categories": ["unrecovered_actionable_source_group"],
        }
        for label_id in sorted(undispositioned_ids)
    )
    correction_routes = _qualification_correction_routes(
        adjudications,
        output_author_provenance=output_author_provenance,
        output_causal_targets=_accepted_output_causal_targets(outputs_materialized),
        false_rejections=unrecovered_feedback,
        false_rejection_author_provenance=false_rejection_author_provenance,
    )
    repaired = sum(item.get("repair_status") == "repaired" for item in adjudications)
    repair_unknown = sum(item.get("repair_status") == "unknown" for item in adjudications)
    known_repair = output_count - repair_unknown

    failures = list(receipt_errors)
    if positive_throughput_required:
        # A sealed no-actionable receipt is a successful operational exhaustion,
        # not a failed attempt to fabricate positive throughput.  It is classified
        # separately below so release qualification can require real recovered work.
        if actionable_count > 0 and ticket_count == 0:
            failures.append("independent_qualification_actionable_zero_output")
        elif actionable_count > 0:
            if undispositioned_ids:
                failures.append(
                    "independent_qualification_source_actionable_disposition_incomplete:"
                    + ",".join(sorted(undispositioned_ids))
                )
            if missed_ids and len(recovered_ids) / len(missed_ids) < float(
                minimum_recovered_to_missed_ratio
            ):
                failures.append(
                    "independent_qualification_recovered_to_missed_ratio_below_minimum:"
                    f"observed={len(recovered_ids) / len(missed_ids):g}:"
                    f"required={float(minimum_recovered_to_missed_ratio):g}"
                )
            if ticket_good < minimum_good_ticket_count:
                failures.append(
                    "independent_qualification_good_ticket_count_below_minimum:"
                    f"observed={ticket_good}:required={minimum_good_ticket_count}"
                )
            if ticket_critical_bad:
                failures.append(
                    "independent_qualification_critical_bad_ticket_accepted:"
                    f"count={ticket_critical_bad}"
                )
            if require_zero_unknown_authoritative_tickets and ticket_unknown:
                failures.append(
                    "independent_qualification_unknown_authoritative_ticket_accepted:"
                    f"count={ticket_unknown}"
                )
            if ticket_bad > 0 and ticket_good / ticket_bad < float(minimum_good_to_bad_ratio):
                failures.append(
                    "independent_qualification_good_to_bad_ratio_below_minimum:"
                    f"observed={ticket_good / ticket_bad:g}:"
                    f"required={float(minimum_good_to_bad_ratio):g}"
                )
    non_ticket_critical_bad = accepted_critical_bad - ticket_critical_bad
    if non_ticket_critical_bad:
        failures.append(
            "independent_qualification_critical_bad_output_accepted:"
            f"count={non_ticket_critical_bad}"
        )

    adjudicated_quality = accepted_good + accepted_bad
    if accepted_bad == 0 and accepted_good > 0:
        ratio_status, ratio_value = "infinite", None
    elif accepted_bad == 0:
        ratio_status, ratio_value = "undefined", None
    else:
        ratio_status, ratio_value = "finite", accepted_good / accepted_bad
    unknowns: list[str] = []
    if any(label.get("classification") == "ambiguous" for label in labels):
        unknowns.append("ambiguous_atom_groups_present")
    if any(label.get("classification") == "unknown" for label in labels):
        unknowns.append("unknown_atom_groups_present")
    if accepted_unknown:
        unknowns.append("accepted_output_quality_unknown")
    if repair_unknown:
        unknowns.append("accepted_output_repair_status_unknown")
    if undispositioned_ids:
        unknowns.append("unrecovered_actionable_disposition_unknown")

    by_kind: dict[str, Any] = {}
    for output_kind in _OUTPUT_KINDS:
        kind_items = [item for item in adjudications if item.get("output_kind") == output_kind]
        kind_accepted = len(outputs_materialized[output_kind])
        kind_good = sum(item.get("quality") == "good" for item in kind_items)
        kind_bad = sum(item.get("quality") == "bad" for item in kind_items)
        kind_unknown = sum(item.get("quality") == "unknown" for item in kind_items)
        kind_critical_bad = sum(
            item.get("quality") == "bad" and item.get("bad_severity") == "critical"
            for item in kind_items
        )
        kind_noncritical_bad = sum(
            item.get("quality") == "bad" and item.get("bad_severity") == "noncritical"
            for item in kind_items
        )
        kind_repaired = sum(item.get("repair_status") == "repaired" for item in kind_items)
        kind_repair_unknown = sum(item.get("repair_status") == "unknown" for item in kind_items)
        kind_adjudicated = kind_good + kind_bad
        kind_known_repair = kind_accepted - kind_repair_unknown
        if kind_bad == 0 and kind_good > 0:
            kind_ratio_status, kind_ratio_value = "infinite", None
        elif kind_bad == 0:
            kind_ratio_status, kind_ratio_value = "undefined", None
        else:
            kind_ratio_status, kind_ratio_value = "finite", kind_good / kind_bad
        by_kind[output_kind] = {
            "counts": {
                "accepted": kind_accepted,
                "good": kind_good,
                "bad": kind_bad,
                "unknown": kind_unknown,
                "critical_bad": kind_critical_bad,
                "noncritical_bad": kind_noncritical_bad,
                "repaired": kind_repaired,
                "repair_unknown": kind_repair_unknown,
            },
            "rates": {
                "quality_coverage": _rate(kind_adjudicated, kind_accepted),
                "good_among_adjudicated": _rate(kind_good, kind_adjudicated),
                "repair_coverage": _rate(kind_known_repair, kind_accepted),
                "repair_among_known": _rate(kind_repaired, kind_known_repair),
            },
            "good_to_bad_ratio": {
                "good": kind_good,
                "bad": kind_bad,
                "value": kind_ratio_value,
                "status": kind_ratio_status,
            },
        }

    source_actionable_outcomes = sorted(
        [
            {
                "source_actionable_group_key": source_group_key_by_label_id[label_id],
                "disposition": (
                    "recovered"
                    if label_id in recovered_ids
                    else "false_rejected"
                    if label_id in false_rejected_ids
                    else "undispositioned"
                ),
            }
            for label_id in actionable_ids
            if label_id in source_group_key_by_label_id
        ],
        key=_canonical_hash,
    )
    stability_sha256 = _canonical_hash(
        {
            "source_labels": source_label_projection,
            "source_actionable_outcomes": source_actionable_outcomes,
            "policy": policy,
        }
    )

    status = "invalid" if receipt_errors else "failed" if failures else "verified"
    qualification_class = (
        "verified_exhaustion"
        if status == "verified" and exhausted
        else "positive_throughput"
        if status == "verified" and positive_throughput_required and actionable_count > 0
        else "unqualified"
    )
    useful_output_verified = bool(
        status == "verified"
        and qualification_class in {"positive_throughput", "verified_exhaustion"}
    )

    return {
        "schema_version": 1,
        "qualification_class": qualification_class,
        "policy": policy,
        "limitations": list(_QUALIFICATION_LIMITATIONS),
        "status": status,
        "failures": list(dict.fromkeys(failures)),
        # Same-corpus feedback is part of the system under test. It makes the
        # result a corrected final output rather than a clean first pass, but a
        # fresh post-correction adjudication can still verify that final output.
        "clean_first_pass": bool(
            not same_corpus_feedback_exposed and not correction_routes
        ),
        "correction_required": bool(
            same_corpus_feedback_exposed or correction_routes
        ),
        "correction_metrics": normalized_correction_metrics,
        "independent_release_evidence": not same_corpus_feedback_exposed,
        "final_output_independently_adjudicated": status in {"verified", "failed"},
        "useful_output_verified": useful_output_verified,
        "correction_routes": correction_routes,
        "correction_routing_status": (
            "pending_orchestration" if correction_routes else "not_required"
        ),
        "stability_sha256": stability_sha256,
        "basis_sha256": _canonical_hash(basis),
        "counts": {
            "eligible_atoms": eligible_count,
            "independently_labeled_atoms": sum(len(label.get("atom_ids", [])) for label in labels),
            "actionable_cases": actionable_count,
            "derived_only_actionable_groups_excluded": len(derived_only_actionable_ids),
            "non_actionable_groups": sum(
                label.get("classification") == "non_actionable" for label in labels
            ),
            "ambiguous_groups": sum(label.get("classification") == "ambiguous" for label in labels),
            "unknown_groups": sum(label.get("classification") == "unknown" for label in labels),
            "accepted_outputs": output_count,
            "accepted_end_to_end_tickets": ticket_count,
            "accepted_good": accepted_good,
            "accepted_bad": accepted_bad,
            "accepted_unknown": accepted_unknown,
            "accepted_critical_bad": accepted_critical_bad,
            "accepted_noncritical_bad": accepted_noncritical_bad,
            "recovered_actionable_cases": len(recovered_ids),
            "accepted_ticket_referenced_actionable_cases": len(accepted_ticket_ref_ids),
            "unrecovered_actionable_cases": actionable_count - len(recovered_ids),
            "false_rejected_good": len(false_rejected_ids),
            "undispositioned_actionable_cases": len(undispositioned_ids),
            "repaired": repaired,
            "repair_unknown": repair_unknown,
            "correction_routes": len(correction_routes),
            "same_author_correction_routes": sum(
                route.get("route_status") == "same_author_resume" for route in correction_routes
            ),
            "unrouteable_corrections": sum(
                route.get("route_status") == "author_provenance_unavailable"
                for route in correction_routes
            ),
            "zero_output": int(ticket_count == 0),
            "zero_accepted_artifacts": int(output_count == 0),
            "actionable_zero_output": int(actionable_count > 0 and ticket_count == 0),
            "exhausted_corpus": int(exhausted),
            "positive_qualifying_corpus": int(qualification_class == "positive_throughput"),
        },
        "rates": {
            "actionable_recovery": _rate(len(recovered_ids), actionable_count),
            "false_rejected_good_share_of_actionable": _rate(
                len(false_rejected_ids),
                actionable_count,
            ),
            "recovered_to_missed": _rate(len(recovered_ids), len(missed_ids)),
            "accepted_quality_coverage": _rate(adjudicated_quality, output_count),
            "accepted_good_among_adjudicated": _rate(accepted_good, adjudicated_quality),
            "repair_coverage": _rate(known_repair, output_count),
            "repair_among_known": _rate(repaired, known_repair),
        },
        "good_to_bad_ratio": {
            "good": accepted_good,
            "bad": accepted_bad,
            "value": ratio_value,
            "status": ratio_status,
        },
        "by_kind": by_kind,
        "end_to_end": by_kind["ticket"],
        "unknowns": unknowns,
    }


__all__ = [
    "NoActionableEvidenceReceipt",
    "QualificationCorpusManifest",
    "QualificationOutputAdjudication",
    "build_no_actionable_evidence_receipt",
    "build_qualification_corpus_manifest",
    "build_qualification_output_adjudication",
    "evaluate_independent_qualification",
    "no_actionable_evidence_receipt_errors",
    "qualification_manifest_errors",
    "qualification_output_causal_target",
    "qualification_output_adjudication_errors",
]
