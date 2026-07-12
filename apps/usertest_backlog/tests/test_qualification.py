from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256

import pytest
from backlog_core.case_lineage import apply_atom_disposition_decision

import usertest_backlog.workflows.qualification as qualification_mod
from usertest_backlog.workflows.qualification import (
    build_no_actionable_evidence_receipt,
    build_qualification_corpus_manifest,
    build_qualification_output_adjudication,
    evaluate_independent_qualification,
    no_actionable_evidence_receipt_errors,
    qualification_manifest_errors,
    qualification_output_adjudication_errors,
)


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return sha256(encoded).hexdigest()


def _atom(atom_id: str) -> dict[str, object]:
    return {
        "atom_id": atom_id,
        "source": "run_failure",
        "evidence_role": "observation",
        "evidence_class": "observed_failure",
        "text": f"Observed automated failure {atom_id}.",
    }


def _output(case_id: str) -> dict[str, object]:
    return {
        "case_id": case_id,
        "problem_id": case_id.replace("case:", "problem:"),
        "plan_revision_id": f"plan:{case_id}",
        "stage": "ready_for_ticket",
        "change_plan": {"mechanism": f"mechanism:{case_id}"},
    }


def _labels(*records: tuple[str, str, list[str]]) -> list[dict[str, object]]:
    return [
        {
            "label_id": label_id,
            "classification": classification,
            "atom_ids": atom_ids,
            "rationale": f"Independent classification for {label_id}.",
        }
        for label_id, classification, atom_ids in records
    ]


def _manifest(
    atoms: list[dict[str, object]],
    labels: list[dict[str, object]],
) -> dict[str, object]:
    return build_qualification_corpus_manifest(
        atoms=atoms,
        atom_labels=labels,
        adjudicator="held-out-reviewer",
        method="independent retained-evidence review",
    )


def _adjudication(
    *,
    manifest: dict[str, object],
    outputs: list[dict[str, object]],
    qualities: list[tuple[str, str, list[str]]],
    false_rejections: list[str] | None = None,
) -> dict[str, object]:
    return build_qualification_output_adjudication(
        manifest=manifest,
        accepted_outputs_by_kind={"ticket": outputs},
        output_adjudications=[
            {
                "output_kind": "ticket",
                "output_sha256": _canonical_hash(output),
                "quality": quality,
                "actionable_label_ids": label_ids,
                "repair_status": repair_status,
                "rationale": f"Held-out assessment of {output['case_id']}.",
                **(
                    {
                        "bad_severity": "noncritical",
                        "bad_categories": ["limited_causal_coverage"],
                    }
                    if quality == "bad"
                    else {}
                ),
            }
            for output, (quality, repair_status, label_ids) in zip(outputs, qualities, strict=True)
        ],
        false_rejections=[
            {
                "label_id": label_id,
                "rationale": "A useful independently expected case was rejected.",
            }
            for label_id in (false_rejections or [])
        ],
        pending_run_sha256="d" * 64,
        adjudicator="post-run-reviewer",
        method="independent end-to-end output review",
    )


def _evaluate(
    *,
    atoms: list[dict[str, object]],
    outputs: list[dict[str, object]],
    manifest: dict[str, object] | None,
    adjudication: dict[str, object] | None,
    receipt: dict[str, object] | None = None,
    positive: bool = True,
    expected_anchor: str = "a" * 64,
    observed_anchor: str = "a" * 64,
    minimum_recovered_to_missed_ratio: float = 2.0,
    output_author_provenance: dict[str, dict[str, object]] | None = None,
    false_rejection_author_provenance: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    return evaluate_independent_qualification(
        atoms=atoms,
        accepted_outputs_by_kind={"ticket": outputs},
        manifest=manifest,
        qualification_manifest_sha256_expected=expected_anchor,
        qualification_manifest_sha256_observed=observed_anchor,
        output_adjudication=adjudication,
        output_adjudication_sha256_pre_run=None,
        output_adjudication_sha256_post_run="b" * 64,
        pending_run_sha256="d" * 64,
        no_actionable_receipt=receipt,
        output_author_provenance=output_author_provenance,
        false_rejection_author_provenance=false_rejection_author_provenance,
        positive_throughput_required=positive,
        minimum_recovered_to_missed_ratio=minimum_recovered_to_missed_ratio,
    )


def test_manifest_is_a_sealed_pre_run_atom_denominator_only() -> None:
    atoms = [_atom("atom:one")]
    manifest = _manifest(atoms, _labels(("label:one", "actionable", ["atom:one"])))

    assert manifest["provenance"]["sealed_before_pipeline"] is True
    assert manifest["provenance"]["labels_withheld_from_model_stages"] is True
    assert "accepted_output_corpus" not in manifest
    assert "output_adjudications" not in manifest
    assert qualification_manifest_errors(manifest, atoms=atoms) == []


def test_manifest_atom_binding_ignores_only_stage1_decisions() -> None:
    atom = {
        **_atom("atom:one"),
        "severity": "high",
        "origin_run_id": "run:source",
        "parent_case_id": None,
        "disposition": "unresolved",
        "disposition_status": "pending",
        "disposition_receipt": None,
        "disposition_revisit_when": None,
        "case_id": None,
        "supporting_case_ids": [],
    }
    manifest = _manifest(
        [atom],
        _labels(("label:one", "actionable", ["atom:one"])),
    )
    decided = deepcopy(atom)
    decided.update(
        {
            "case_id": "case:one",
            "supporting_case_ids": ["case:one"],
            "disposition": "supports_case",
            "disposition_status": "decided",
            "disposition_receipt": {"receipt_sha256": "a" * 64},
            "disposition_revisit_when": "new contradictory evidence appears",
        }
    )

    assert qualification_manifest_errors(manifest, atoms=[decided]) == []

    for field, changed_value in (
        ("text", "Different evidence content."),
        ("severity", "low"),
        ("origin_run_id", "run:different"),
        ("parent_case_id", "case:different-parent"),
    ):
        changed = deepcopy(decided)
        changed[field] = changed_value
        assert "qualification_manifest_atom_corpus_mismatch" in (
            qualification_manifest_errors(manifest, atoms=[changed])
        )


def test_post_run_adjudication_binds_manifest_and_exact_output_content() -> None:
    atoms = [_atom("atom:one")]
    outputs = [_output("case:one")]
    manifest = _manifest(atoms, _labels(("label:one", "actionable", ["atom:one"])))
    adjudication = _adjudication(
        manifest=manifest,
        outputs=outputs,
        qualities=[("good", "not_repaired", ["label:one"])],
    )

    assert adjudication["provenance"]["completed_after_pipeline"] is True
    assert (
        qualification_output_adjudication_errors(
            adjudication,
            manifest=manifest,
            accepted_outputs_by_kind={"ticket": outputs},
        )
        == []
    )

    changed_outputs = deepcopy(outputs)
    changed_outputs[0]["change_plan"] = {"mechanism": "surface-only-patch"}
    assert "qualification_output_adjudication_corpus_mismatch" in (
        qualification_output_adjudication_errors(
            adjudication,
            manifest=manifest,
            accepted_outputs_by_kind={"ticket": changed_outputs},
        )
    )


def test_manifest_tamper_and_forged_provenance_are_rejected() -> None:
    atoms = [_atom("atom:one")]
    manifest = _manifest(atoms, _labels(("label:one", "actionable", ["atom:one"])))
    tampered = deepcopy(manifest)
    tampered["atom_labels"][0]["rationale"] = "Changed after sealing."

    errors = qualification_manifest_errors(tampered, atoms=atoms)

    assert "qualification_manifest_content_sha256_mismatch" in errors

    forged = deepcopy(manifest)
    forged["provenance"]["independent_from_pipeline"] = False
    forged["content_sha256"] = qualification_mod._content_hash(forged)

    errors = qualification_manifest_errors(forged, atoms=atoms)

    assert "qualification_independence_not_certified" in errors


def test_recomputed_replacement_hash_cannot_defeat_pre_run_anchor() -> None:
    atoms = [_atom("atom:one")]
    manifest = _manifest(atoms, _labels(("label:one", "actionable", ["atom:one"])))
    replacement = deepcopy(manifest)
    replacement["atom_labels"][0]["rationale"] = "Replaced after Stage 1 began."
    replacement["content_sha256"] = qualification_mod._content_hash(replacement)

    report = _evaluate(
        atoms=atoms,
        outputs=[],
        manifest=replacement,
        adjudication=None,
        expected_anchor="a" * 64,
        observed_anchor="b" * 64,
    )

    assert report["status"] == "invalid"
    assert "qualification_manifest_changed_after_pre_run_anchor" in report["failures"]


def test_preexisting_output_adjudication_cannot_qualify_new_materialized_run() -> None:
    atoms = [_atom("atom:one")]
    ticket = _output("case:one")
    manifest = _manifest(atoms, _labels(("label:one", "actionable", ["atom:one"])))
    adjudication = _adjudication(
        manifest=manifest,
        outputs=[ticket],
        qualities=[("good", "not_repaired", ["label:one"])],
    )

    report = evaluate_independent_qualification(
        atoms=atoms,
        accepted_outputs_by_kind={"ticket": [ticket]},
        manifest=manifest,
        qualification_manifest_sha256_expected="a" * 64,
        qualification_manifest_sha256_observed="a" * 64,
        output_adjudication=adjudication,
        output_adjudication_sha256_pre_run="b" * 64,
        output_adjudication_sha256_post_run="b" * 64,
        pending_run_sha256="d" * 64,
        positive_throughput_required=True,
    )

    assert report["status"] == "invalid"
    assert "qualification_output_adjudication_not_fresh_for_materialized_run" in report["failures"]


def test_stability_uses_source_actionability_not_derived_atom_or_artifact_hash_drift() -> None:
    source = _atom("atom:source")
    derived = {
        **_atom("atom:derived"),
        "evidence_role": "research",
        "parent_case_id": "case:source",
    }
    atoms = [source, derived]
    first_manifest = _manifest(
        atoms,
        _labels(
            ("label:source", "actionable", ["atom:source"]),
            ("label:derived", "non_actionable", ["atom:derived"]),
        ),
    )
    second_manifest = _manifest(
        atoms,
        _labels(
            ("label:source-reminted", "actionable", ["atom:source"]),
            ("label:derived-reminted", "unknown", ["atom:derived"]),
        ),
    )
    first = evaluate_independent_qualification(
        atoms=atoms,
        accepted_outputs_by_kind={},
        manifest=first_manifest,
        qualification_manifest_sha256_expected="a" * 64,
        qualification_manifest_sha256_observed="a" * 64,
        positive_throughput_required=False,
    )
    second = evaluate_independent_qualification(
        atoms=atoms,
        accepted_outputs_by_kind={},
        manifest=second_manifest,
        qualification_manifest_sha256_expected="b" * 64,
        qualification_manifest_sha256_observed="b" * 64,
        positive_throughput_required=False,
    )

    assert first["basis_sha256"] != second["basis_sha256"]
    assert first["stability_sha256"] == second["stability_sha256"]
    assert first["counts"]["actionable_cases"] == 1
    assert second["counts"]["actionable_cases"] == 1

    changed_source_manifest = _manifest(
        atoms,
        _labels(
            ("label:source", "unknown", ["atom:source"]),
            ("label:derived", "unknown", ["atom:derived"]),
        ),
    )
    receipt = build_no_actionable_evidence_receipt(
        manifest=changed_source_manifest,
        adjudicator="held-out-reviewer",
        method="complete source-actionability review",
    )
    changed_source = evaluate_independent_qualification(
        atoms=atoms,
        accepted_outputs_by_kind={},
        manifest=changed_source_manifest,
        qualification_manifest_sha256_expected="c" * 64,
        qualification_manifest_sha256_observed="c" * 64,
        no_actionable_receipt=receipt,
        positive_throughput_required=False,
    )

    assert changed_source["stability_sha256"] != first["stability_sha256"]


def test_receipted_novel_derived_failure_counts_as_actionable_recovery_work() -> None:
    derived = apply_atom_disposition_decision(
        {
            **_atom("atom:novel-research-failure"),
            "evidence_role": "research",
            "evidence_class": "observed",
            "origin_run_id": "run:research-infrastructure",
            "origin_stage": "repro_research",
            "parent_case_id": "case:original",
            "case_id": "case:research-infrastructure",
            "derived_from_atom_ids": ["atom:original"],
            "supporting_case_ids": ["case:research-infrastructure"],
            "disposition": "novel_case",
            "novel_case_rationale": (
                "The runner failed before it could inspect the assigned parent case."
            ),
        },
        disposition="novel_case",
        source="runner_novel_case_classification",
        rationale="The runner failed before it could inspect the assigned parent case.",
    )
    manifest = _manifest(
        [derived],
        _labels(
            (
                "label:research-infrastructure",
                "actionable",
                ["atom:novel-research-failure"],
            )
        ),
    )
    adjudication = _adjudication(
        manifest=manifest,
        outputs=[],
        qualities=[],
        false_rejections=["label:research-infrastructure"],
    )

    report = _evaluate(
        atoms=[derived],
        outputs=[],
        manifest=manifest,
        adjudication=adjudication,
    )

    assert report["status"] == "failed"
    assert "independent_qualification_actionable_zero_output" in report["failures"]
    assert report["counts"]["actionable_cases"] == 1
    assert report["counts"]["derived_only_actionable_groups_excluded"] == 0
    assert report["counts"]["false_rejected_good"] == 1


def test_stability_tolerates_different_independently_good_ticket_mechanisms() -> None:
    atoms = [_atom("atom:one")]
    manifest = _manifest(atoms, _labels(("label:one", "actionable", ["atom:one"])))
    first_output = _output("case:one")
    second_output = deepcopy(first_output)
    second_output["change_plan"] = {"mechanism": "different-but-independently-good"}
    first_adjudication = _adjudication(
        manifest=manifest,
        outputs=[first_output],
        qualities=[("good", "not_repaired", ["label:one"])],
    )
    second_adjudication = _adjudication(
        manifest=manifest,
        outputs=[second_output],
        qualities=[("good", "not_repaired", ["label:one"])],
    )

    first = _evaluate(
        atoms=atoms,
        outputs=[first_output],
        manifest=manifest,
        adjudication=first_adjudication,
    )
    second = _evaluate(
        atoms=atoms,
        outputs=[second_output],
        manifest=manifest,
        adjudication=second_adjudication,
    )

    assert first["basis_sha256"] != second["basis_sha256"]
    assert first["stability_sha256"] == second["stability_sha256"]


@pytest.mark.parametrize(
    ("expected_anchor", "observed_anchor", "failure"),
    [
        (None, "a" * 64, "qualification_manifest_pre_run_anchor_missing_or_invalid"),
        ("a" * 64, None, "qualification_manifest_post_run_hash_missing_or_invalid"),
    ],
)
def test_manifest_requires_both_pre_and_post_run_hash_receipts(
    expected_anchor: str | None,
    observed_anchor: str | None,
    failure: str,
) -> None:
    atoms = [_atom("atom:one")]
    manifest = _manifest(atoms, _labels(("label:one", "actionable", ["atom:one"])))

    report = evaluate_independent_qualification(
        atoms=atoms,
        accepted_outputs_by_kind={},
        manifest=manifest,
        qualification_manifest_sha256_expected=expected_anchor,
        qualification_manifest_sha256_observed=observed_anchor,
        positive_throughput_required=True,
    )

    assert failure in report["failures"]


def test_actionable_corpus_with_zero_output_fails_even_with_false_rejection_record() -> None:
    atoms = [_atom("atom:one")]
    manifest = _manifest(atoms, _labels(("label:one", "actionable", ["atom:one"])))
    adjudication = _adjudication(
        manifest=manifest,
        outputs=[],
        qualities=[],
        false_rejections=["label:one"],
    )

    report = _evaluate(
        atoms=atoms,
        outputs=[],
        manifest=manifest,
        adjudication=adjudication,
    )

    assert report["status"] == "failed"
    assert report["failures"] == ["independent_qualification_actionable_zero_output"]
    assert report["counts"]["actionable_cases"] == 1
    assert report["counts"]["false_rejected_good"] == 1
    assert report["counts"]["zero_output"] == 1
    assert report["counts"]["actionable_zero_output"] == 1


def test_one_recovered_and_nine_missed_fails_recovery_throughput() -> None:
    atoms = [_atom(f"atom:{index}") for index in range(10)]
    labels = _labels(*((f"label:{index}", "actionable", [f"atom:{index}"]) for index in range(10)))
    manifest = _manifest(atoms, labels)
    output = _output("case:recovered")
    adjudication = _adjudication(
        manifest=manifest,
        outputs=[output],
        qualities=[("good", "repaired", ["label:0"])],
        false_rejections=[f"label:{index}" for index in range(1, 10)],
    )

    report = _evaluate(
        atoms=atoms,
        outputs=[output],
        manifest=manifest,
        adjudication=adjudication,
    )

    assert report["status"] == "failed"
    assert report["failures"] == [
        "independent_qualification_recovered_to_missed_ratio_below_minimum:"
        "observed=0.111111:required=2"
    ]
    assert report["counts"]["recovered_actionable_cases"] == 1
    assert report["counts"]["unrecovered_actionable_cases"] == 9
    assert report["counts"]["undispositioned_actionable_cases"] == 0
    assert report["rates"]["recovered_to_missed"]["value"] == pytest.approx(1 / 9)


def test_two_recovered_and_one_explicit_miss_meets_default_recovery_ratio() -> None:
    atoms = [_atom(f"atom:{index}") for index in range(3)]
    manifest = _manifest(
        atoms,
        _labels(*((f"label:{index}", "actionable", [f"atom:{index}"]) for index in range(3))),
    )
    outputs = [_output("case:one"), _output("case:two")]
    adjudication = _adjudication(
        manifest=manifest,
        outputs=outputs,
        qualities=[
            ("good", "repaired", ["label:0"]),
            ("good", "not_repaired", ["label:1"]),
        ],
        false_rejections=["label:2"],
    )

    report = _evaluate(
        atoms=atoms,
        outputs=outputs,
        manifest=manifest,
        adjudication=adjudication,
    )

    assert report["status"] == "verified"
    assert report["qualification_class"] == "positive_throughput"
    assert report["failures"] == []
    assert report["rates"]["recovered_to_missed"]["value"] == 2.0


def test_source_actionable_case_requires_recovered_or_false_rejected_disposition() -> None:
    atoms = [_atom("atom:one"), _atom("atom:two")]
    manifest = _manifest(
        atoms,
        _labels(
            ("label:one", "actionable", ["atom:one"]),
            ("label:two", "actionable", ["atom:two"]),
        ),
    )
    output = _output("case:one")
    adjudication = _adjudication(
        manifest=manifest,
        outputs=[output],
        qualities=[("good", "not_repaired", ["label:one"])],
    )

    report = _evaluate(
        atoms=atoms,
        outputs=[output],
        manifest=manifest,
        adjudication=adjudication,
    )

    assert (
        "independent_qualification_source_actionable_disposition_incomplete:label:two"
        in report["failures"]
    )
    assert report["counts"]["undispositioned_actionable_cases"] == 1


def test_clean_zero_corpus_can_be_exhausted_but_not_positive_qualification() -> None:
    atoms = [_atom("atom:noise")]
    manifest = _manifest(
        atoms,
        _labels(("label:noise", "non_actionable", ["atom:noise"])),
    )
    receipt = build_no_actionable_evidence_receipt(
        manifest=manifest,
        adjudicator="held-out-reviewer",
        method="complete clean-corpus confirmation",
    )

    informational = _evaluate(
        atoms=atoms,
        outputs=[],
        manifest=manifest,
        adjudication=None,
        receipt=receipt,
        positive=False,
    )
    positive = _evaluate(
        atoms=atoms,
        outputs=[],
        manifest=manifest,
        adjudication=None,
        receipt=receipt,
        positive=True,
    )

    assert informational["failures"] == []
    assert informational["counts"]["exhausted_corpus"] == 1
    assert informational["qualification_class"] == "verified_exhaustion"
    assert positive["counts"]["exhausted_corpus"] == 1
    assert positive["status"] == "verified"
    assert positive["qualification_class"] == "verified_exhaustion"
    assert positive["failures"] == []
    assert positive["counts"]["positive_qualifying_corpus"] == 0


def test_clean_receipt_is_hash_and_manifest_bound() -> None:
    atoms = [_atom("atom:noise")]
    manifest = _manifest(
        atoms,
        _labels(("label:noise", "non_actionable", ["atom:noise"])),
    )
    receipt = build_no_actionable_evidence_receipt(
        manifest=manifest,
        adjudicator="held-out-reviewer",
        method="complete clean-corpus confirmation",
    )
    tampered = deepcopy(receipt)
    tampered["eligible_atom_corpus_sha256"] = "f" * 64

    errors = no_actionable_evidence_receipt_errors(tampered, manifest=manifest)

    assert "no_actionable_evidence_receipt_content_sha256_mismatch" in errors
    assert "no_actionable_evidence_receipt_atom_corpus_mismatch" in errors


def test_ambiguous_and_unknown_labels_remain_explicit_unknowns() -> None:
    atoms = [_atom("atom:action"), _atom("atom:ambiguous"), _atom("atom:unknown")]
    output = _output("case:action")
    manifest = _manifest(
        atoms,
        _labels(
            ("label:action", "actionable", ["atom:action"]),
            ("label:ambiguous", "ambiguous", ["atom:ambiguous"]),
            ("label:unknown", "unknown", ["atom:unknown"]),
        ),
    )
    adjudication = _adjudication(
        manifest=manifest,
        outputs=[output],
        qualities=[("unknown", "unknown", ["label:action"])],
    )

    report = _evaluate(
        atoms=atoms,
        outputs=[output],
        manifest=manifest,
        adjudication=adjudication,
    )

    assert set(report["failures"]) == {
        "independent_qualification_source_actionable_disposition_incomplete:label:action",
        "independent_qualification_recovered_to_missed_ratio_below_minimum:observed=0:required=2",
        "independent_qualification_good_ticket_count_below_minimum:observed=0:required=1",
        "independent_qualification_unknown_authoritative_ticket_accepted:count=1",
    }
    assert report["counts"]["ambiguous_groups"] == 1
    assert report["counts"]["unknown_groups"] == 1
    assert report["counts"]["accepted_unknown"] == 1
    assert set(report["unknowns"]) >= {
        "ambiguous_atom_groups_present",
        "unknown_atom_groups_present",
        "accepted_output_quality_unknown",
        "accepted_output_repair_status_unknown",
    }


def test_mixed_good_bad_and_repaired_throughput_is_measured_without_perfection_gate() -> None:
    atoms = [_atom("atom:one"), _atom("atom:two")]
    outputs = [_output("case:one"), _output("case:two"), _output("case:surface")]
    manifest = _manifest(
        atoms,
        _labels(
            ("label:one", "actionable", ["atom:one"]),
            ("label:two", "actionable", ["atom:two"]),
        ),
    )
    adjudication = _adjudication(
        manifest=manifest,
        outputs=outputs,
        qualities=[
            ("good", "repaired", ["label:one"]),
            ("good", "not_repaired", ["label:two"]),
            ("bad", "unknown", []),
        ],
    )

    report = _evaluate(
        atoms=atoms,
        outputs=outputs,
        manifest=manifest,
        adjudication=adjudication,
    )

    assert report["failures"] == []
    assert report["counts"]["recovered_actionable_cases"] == 2
    assert report["counts"]["accepted_good"] == 2
    assert report["counts"]["accepted_bad"] == 1
    assert report["counts"]["accepted_unknown"] == 0
    assert report["counts"]["repaired"] == 1
    assert report["counts"]["repair_unknown"] == 1
    assert report["rates"]["actionable_recovery"]["value"] == 1.0
    assert report["rates"]["accepted_good_among_adjudicated"]["value"] == pytest.approx(2 / 3)
    assert report["good_to_bad_ratio"] == {
        "good": 2,
        "bad": 1,
        "value": 2.0,
        "status": "finite",
    }


def test_typed_outputs_report_stage_quality_and_end_to_end_ticket_quality_separately() -> None:
    atoms = [_atom("atom:one")]
    manifest = _manifest(atoms, _labels(("label:one", "actionable", ["atom:one"])))
    outputs_by_kind = {
        "research": [{"case_id": "case:one", "research_status": "evidence_sufficient"}],
        "selection": [{"case_id": "case:one", "selected_option_id": "option:one"}],
        "plan": [{"case_id": "case:one", "plan_revision_id": "plan:one"}],
        "ticket": [_output("case:one")],
    }
    quality_by_kind = {
        "research": ("good", "repaired", ["label:one"]),
        "selection": ("unknown", "unknown", []),
        "plan": ("bad", "not_repaired", ["label:one"]),
        "ticket": ("good", "not_repaired", ["label:one"]),
    }
    output_adjudications = []
    for output_kind, records in outputs_by_kind.items():
        quality, repair_status, label_ids = quality_by_kind[output_kind]
        output_adjudications.append(
            {
                "output_kind": output_kind,
                "output_sha256": _canonical_hash(records[0]),
                "quality": quality,
                "actionable_label_ids": label_ids,
                "repair_status": repair_status,
                "rationale": f"Independent {output_kind} assessment.",
                **(
                    {
                        "bad_severity": "noncritical",
                        "bad_categories": ["limited_causal_coverage"],
                    }
                    if quality == "bad"
                    else {}
                ),
            }
        )
    adjudication = build_qualification_output_adjudication(
        manifest=manifest,
        accepted_outputs_by_kind=outputs_by_kind,
        output_adjudications=output_adjudications,
        pending_run_sha256="d" * 64,
        adjudicator="post-run-reviewer",
        method="typed artifact quality review",
    )

    report = evaluate_independent_qualification(
        atoms=atoms,
        accepted_outputs_by_kind=outputs_by_kind,
        manifest=manifest,
        qualification_manifest_sha256_expected="a" * 64,
        qualification_manifest_sha256_observed="a" * 64,
        output_adjudication=adjudication,
        output_adjudication_sha256_pre_run=None,
        output_adjudication_sha256_post_run="b" * 64,
        pending_run_sha256="d" * 64,
        positive_throughput_required=True,
    )

    assert report["failures"] == []
    assert report["counts"]["accepted_good"] == 2
    assert report["counts"]["accepted_bad"] == 1
    assert report["counts"]["accepted_unknown"] == 1
    assert report["by_kind"]["research"]["counts"]["good"] == 1
    assert report["by_kind"]["selection"]["counts"]["unknown"] == 1
    assert report["by_kind"]["plan"]["counts"]["bad"] == 1
    assert report["end_to_end"]["counts"] == {
        "accepted": 1,
        "good": 1,
        "bad": 0,
        "unknown": 0,
        "critical_bad": 0,
        "noncritical_bad": 0,
        "repaired": 0,
        "repair_unknown": 0,
    }


def _ticket_policy_report(
    ticket_specs: list[tuple[str, str | None]],
    *,
    minimum_ratio: float = 3.0,
) -> dict[str, object]:
    atoms = [_atom("atom:one")]
    manifest = _manifest(atoms, _labels(("label:one", "actionable", ["atom:one"])))
    tickets = [_output(f"case:ticket-{index}") for index in range(len(ticket_specs))]
    output_adjudications = []
    for ticket, (quality, severity) in zip(tickets, ticket_specs, strict=True):
        item: dict[str, object] = {
            "output_kind": "ticket",
            "output_sha256": _canonical_hash(ticket),
            "quality": quality,
            "actionable_label_ids": ["label:one"],
            "repair_status": "not_repaired",
            "rationale": "Independent authoritative-ticket quality assessment.",
        }
        if quality == "bad":
            item.update(
                {
                    "bad_severity": severity,
                    "bad_categories": [
                        "fabrication" if severity == "critical" else "limited_coverage"
                    ],
                }
            )
        output_adjudications.append(item)
    adjudication = build_qualification_output_adjudication(
        manifest=manifest,
        accepted_outputs_by_kind={"ticket": tickets},
        output_adjudications=output_adjudications,
        pending_run_sha256="d" * 64,
        adjudicator="post-run-reviewer",
        method="authoritative ticket policy benchmark",
    )
    return evaluate_independent_qualification(
        atoms=atoms,
        accepted_outputs_by_kind={"ticket": tickets},
        manifest=manifest,
        qualification_manifest_sha256_expected="a" * 64,
        qualification_manifest_sha256_observed="a" * 64,
        output_adjudication=adjudication,
        output_adjudication_sha256_pre_run=None,
        output_adjudication_sha256_post_run="b" * 64,
        pending_run_sha256="d" * 64,
        positive_throughput_required=True,
        minimum_good_ticket_count=1,
        minimum_good_to_bad_ratio=minimum_ratio,
        require_zero_unknown_authoritative_tickets=True,
    )


def test_two_to_one_noncritical_ticket_ratio_fails_configured_three_to_one_policy() -> None:
    report = _ticket_policy_report(
        [("good", None), ("good", None), ("bad", "noncritical")],
        minimum_ratio=3.0,
    )

    assert report["failures"] == [
        "independent_qualification_good_to_bad_ratio_below_minimum:observed=2:required=3"
    ]
    assert report["status"] == "failed"
    assert report["end_to_end"]["counts"]["noncritical_bad"] == 1


def test_three_to_one_noncritical_ticket_ratio_passes_configured_policy() -> None:
    report = _ticket_policy_report(
        [
            ("good", None),
            ("good", None),
            ("good", None),
            ("bad", "noncritical"),
        ],
        minimum_ratio=3.0,
    )

    assert report["failures"] == []
    assert report["status"] == "verified"
    assert report["counts"]["positive_qualifying_corpus"] == 1
    assert report["end_to_end"]["good_to_bad_ratio"]["value"] == 3.0


def test_critical_bad_ticket_fails_regardless_of_good_to_bad_ratio() -> None:
    report = _ticket_policy_report(
        [
            ("good", None),
            ("good", None),
            ("good", None),
            ("good", None),
            ("bad", "critical"),
        ],
        minimum_ratio=3.0,
    )

    assert report["failures"] == ["independent_qualification_critical_bad_ticket_accepted:count=1"]
    assert report["end_to_end"]["counts"]["critical_bad"] == 1


def test_unknown_authoritative_ticket_prevents_positive_qualification() -> None:
    report = _ticket_policy_report(
        [("good", None), ("good", None), ("good", None), ("unknown", None)],
        minimum_ratio=3.0,
    )

    assert report["failures"] == [
        "independent_qualification_unknown_authoritative_ticket_accepted:count=1"
    ]
    assert report["end_to_end"]["counts"]["unknown"] == 1


def test_bad_ticket_does_not_count_as_recovery_or_hide_end_to_end_false_rejection() -> None:
    atoms = [_atom("atom:one")]
    ticket = _output("case:bad")
    manifest = _manifest(atoms, _labels(("label:one", "actionable", ["atom:one"])))
    adjudication = build_qualification_output_adjudication(
        manifest=manifest,
        accepted_outputs_by_kind={"ticket": [ticket]},
        output_adjudications=[
            {
                "output_kind": "ticket",
                "output_sha256": _canonical_hash(ticket),
                "quality": "bad",
                "bad_severity": "noncritical",
                "bad_categories": ["limited_coverage"],
                "actionable_label_ids": ["label:one"],
                "repair_status": "not_repaired",
                "rationale": "The accepted ticket does not solve the held-out case.",
            }
        ],
        false_rejections=[
            {
                "label_id": "label:one",
                "rationale": "No good authoritative ticket recovered this case.",
            }
        ],
        pending_run_sha256="d" * 64,
        adjudicator="post-run-reviewer",
        method="authoritative ticket recovery review",
    )

    report = evaluate_independent_qualification(
        atoms=atoms,
        accepted_outputs_by_kind={"ticket": [ticket]},
        manifest=manifest,
        qualification_manifest_sha256_expected="a" * 64,
        qualification_manifest_sha256_observed="a" * 64,
        output_adjudication=adjudication,
        output_adjudication_sha256_pre_run=None,
        output_adjudication_sha256_post_run="b" * 64,
        pending_run_sha256="d" * 64,
        positive_throughput_required=True,
    )

    assert report["counts"]["accepted_ticket_referenced_actionable_cases"] == 1
    assert report["counts"]["recovered_actionable_cases"] == 0
    assert report["counts"]["false_rejected_good"] == 1


def test_bad_output_routes_feedback_to_exact_author_without_discarding_provenance() -> None:
    atoms = [_atom("atom:one")]
    ticket = _output("case:bad")
    manifest = _manifest(atoms, _labels(("label:one", "actionable", ["atom:one"])))
    output_sha256 = _canonical_hash(ticket)
    adjudication = build_qualification_output_adjudication(
        manifest=manifest,
        accepted_outputs_by_kind={"ticket": [ticket]},
        output_adjudications=[
            {
                "output_kind": "ticket",
                "output_sha256": output_sha256,
                "quality": "bad",
                "bad_severity": "noncritical",
                "bad_categories": ["limited_root_cause_coverage"],
                "actionable_label_ids": ["label:one"],
                "repair_status": "not_repaired",
                "correctability": "correctable",
                "rationale": (
                    "The planned intervention handles one symptom but leaves the "
                    "verified root-cause path unchanged."
                ),
            }
        ],
        false_rejections=[
            {
                "label_id": "label:one",
                "rationale": "No good ticket has recovered the actionable case yet.",
            }
        ],
        pending_run_sha256="d" * 64,
        adjudicator="post-run-reviewer",
        method="independent correction-routing benchmark",
    )
    provenance = {
        f"ticket:{output_sha256}": {
            "provenance_source": "runner_stage_role_history",
            "authoring_stage": "implementation_planning",
            "author_role": "planner",
            "case_id": "case:bad",
            "problem_id": "problem:bad",
            "agent_session_id": "session:planner",
            "exact_session_continuation": True,
            "workspace_dir": "C:/retained/planner-workspace",
            "workspace_continuity_verified": True,
            "author_attempt_identity": {
                "attempt_number": 2,
                "response_sha256": "e" * 64,
            },
        }
    }

    report = _evaluate(
        atoms=atoms,
        outputs=[ticket],
        manifest=manifest,
        adjudication=adjudication,
        output_author_provenance=provenance,
    )

    route = report["correction_routes"][0]
    assert route["route_status"] == "same_author_resume"
    assert route["agent_session_id"] == "session:planner"
    assert route["workspace_dir"] == "C:/retained/planner-workspace"
    assert route["author_attempt_identity"] == {
        "attempt_number": 2,
        "response_sha256": "e" * 64,
    }
    assert route["restart_from_stage"] == "implementation_planning"
    assert route["rerun_downstream_stages"] == [
        "implementation_planning",
        "ticket_assembly",
    ]
    assert route["consumption_status"] == "pending_orchestration"
    assert route["consumption_receipt"] is None
    assert report["correction_routing_status"] == "pending_orchestration"
    assert report["counts"]["same_author_correction_routes"] == 1


def test_invalid_component_keeps_output_causal_target_without_actionable_labels() -> None:
    output_sha = "a" * 64
    provenance = {
        f"problem:{output_sha}": {
            "provenance_source": "composite_output_component_frontiers",
            "authoring_stage": "problem_mining",
            "problem_id": "problem:old-id",
            "case_id": "case:canonical",
            "causal_target": {
                "problem_ids": ["problem:old-id", "problem:canonical"],
                "case_ids": ["case:canonical"],
                "evidence_atom_ids": ["atom:one"],
                "expected_item_keys": ["problem:old-id"],
            },
            "default_author_component_target": "coverage_review:miner:one",
            "author_component_frontiers": [
                {
                    "component_id": "coverage_review:miner:one",
                    "author_provenance": {
                        "agent_session_id": "session:reviewer",
                        "workspace_dir": "C:/retained/reviewer",
                        "exact_session_continuation": True,
                        "workspace_continuity_verified": True,
                    },
                }
            ],
        }
    }
    routes = qualification_mod._qualification_correction_routes(
        [
            {
                "output_kind": "problem",
                "output_sha256": output_sha,
                "quality": "bad",
                "bad_severity": "noncritical",
                "bad_categories": ["shallow_mechanism"],
                "rationale": "The case describes a symptom instead of its mechanism.",
                "actionable_label_ids": [],
                "correctability": "correctable",
                "author_component_target": "unknown-component",
            }
        ],
        output_author_provenance=provenance,
    )

    assert len(routes) == 1
    route = routes[0]
    assert route["route_status"] == "author_provenance_unavailable"
    assert route["author_provenance"] is None
    assert route["actionable_label_ids"] == []
    assert route["causal_target"] == {
        "problem_ids": ["problem:old-id", "problem:canonical"],
        "case_ids": ["case:canonical"],
        "evidence_atom_ids": ["atom:one"],
        "actionable_label_ids": [],
        "expected_item_keys": ["problem:old-id"],
    }

    no_author_routes = qualification_mod._qualification_correction_routes(
        [
            {
                "output_kind": "problem",
                "output_sha256": output_sha,
                "quality": "bad",
                "bad_severity": "noncritical",
                "bad_categories": ["shallow_mechanism"],
                "rationale": "The case describes a symptom instead of its mechanism.",
                "actionable_label_ids": [],
                "correctability": "correctable",
            }
        ],
        output_author_provenance=None,
        output_causal_targets={
            f"problem:{output_sha}": {
                "problem_ids": ["problem:from-output"],
                "case_ids": ["case:from-output"],
                "evidence_atom_ids": ["atom:from-output"],
                "expected_item_keys": ["problem:from-output"],
            }
        },
    )
    assert no_author_routes[0]["author_provenance"] is None
    assert no_author_routes[0]["causal_target"]["problem_ids"] == [
        "problem:from-output"
    ]


def test_undispositioned_actionable_group_still_routes_to_exact_stage1_author() -> None:
    atoms = [_atom("atom:missed")]
    manifest = _manifest(
        atoms,
        _labels(("label:missed", "actionable", ["atom:missed"])),
    )
    adjudication = _adjudication(
        manifest=manifest,
        outputs=[],
        qualities=[],
        false_rejections=[],
    )
    provenance = {
        "label:missed": {
            "provenance_source": "runner_stage_attempt_history",
            "authoring_stage": "problem_mining",
            "author_role": "coverage_depth_reviewer",
            "agent_session_id": "11111111-1111-4111-8111-111111111111",
            "workspace_dir": "C:/retained/stage1-review",
            "exact_session_continuation": True,
            "workspace_continuity_verified": True,
            "author_attempt_identity": {"attempt_number": 1},
            "rerun_downstream_stages": [
                "problem_mining",
                "problem_prioritization",
                "repro_research",
                "solution_optioning",
                "solution_selection",
                "implementation_planning",
                "ticket_assembly",
            ],
        }
    }

    report = _evaluate(
        atoms=atoms,
        outputs=[],
        manifest=manifest,
        adjudication=adjudication,
        false_rejection_author_provenance=provenance,
    )

    assert report["counts"]["undispositioned_actionable_cases"] == 1
    route = report["correction_routes"][0]
    assert route["feedback_kind"] == "false_rejection"
    assert route["actionable_label_ids"] == ["label:missed"]
    assert route["route_status"] == "same_author_resume"
    assert route["restart_from_stage"] == "problem_mining"


def test_false_rejection_fans_out_to_every_assignment_with_disjoint_atom_targets() -> None:
    def provenance(session: str, atom_id: str) -> dict[str, object]:
        return {
            "provenance_source": "runner_stage_attempt_history",
            "authoring_stage": "problem_mining",
            "author_role": "coverage_depth_reviewer",
            "agent_session_id": session,
            "workspace_dir": f"C:/retained/{session}",
            "exact_session_continuation": True,
            "workspace_continuity_verified": True,
            "author_attempt_identity": {"attempt_number": 1},
            "stage1_correction_adapter": "coverage_review",
            "author_component_id": f"coverage_review:{session}",
            "evidence_atom_ids": [atom_id],
            "causal_target": {
                "problem_ids": [],
                "case_ids": [],
                "evidence_atom_ids": [atom_id],
                "actionable_label_ids": ["label:spanning"],
                "expected_item_keys": [f"atom:{atom_id}"],
            },
        }

    routes = qualification_mod._qualification_correction_routes(
        [],
        output_author_provenance={},
        false_rejections=[
            {
                "label_id": "label:spanning",
                "rationale": "Both independently assigned atoms were omitted.",
                "bad_categories": ["false_rejection"],
            }
        ],
        false_rejection_author_provenance={
            "label:spanning": [
                provenance("session-one", "one"),
                provenance("session-two", "two"),
            ]
        },
    )

    assert len(routes) == 2
    assert {route["agent_session_id"] for route in routes} == {
        "session-one",
        "session-two",
    }
    assert {tuple(route["causal_target"]["evidence_atom_ids"]) for route in routes} == {
        ("one",),
        ("two",),
    }
    assert {tuple(route["causal_target"]["expected_item_keys"]) for route in routes} == {
        ("atom:one",),
        ("atom:two",),
    }


def test_good_ticket_and_false_rejection_for_same_case_is_contradictory() -> None:
    atoms = [_atom("atom:one")]
    ticket = _output("case:good")
    manifest = _manifest(atoms, _labels(("label:one", "actionable", ["atom:one"])))

    with pytest.raises(
        ValueError,
        match="qualification_false_rejection_also_recovered_by_good_ticket:label:one",
    ):
        build_qualification_output_adjudication(
            manifest=manifest,
            accepted_outputs_by_kind={"ticket": [ticket]},
            output_adjudications=[
                {
                    "output_kind": "ticket",
                    "output_sha256": _canonical_hash(ticket),
                    "quality": "good",
                    "actionable_label_ids": ["label:one"],
                    "repair_status": "not_repaired",
                    "rationale": "The good ticket recovers the held-out case.",
                }
            ],
            false_rejections=[
                {
                    "label_id": "label:one",
                    "rationale": "Contradicts the good ticket above.",
                }
            ],
            pending_run_sha256="d" * 64,
            adjudicator="post-run-reviewer",
            method="authoritative ticket recovery review",
        )


def test_disabled_positive_gate_preserves_missing_label_compatibility() -> None:
    report = evaluate_independent_qualification(
        atoms=[_atom("atom:one")],
        accepted_outputs_by_kind={},
        manifest=None,
        output_adjudication=None,
        positive_throughput_required=False,
    )

    assert report["status"] == "missing"
    assert report["failures"] == []
    assert report["counts"]["actionable_cases"] is None
    assert report["unknowns"] == ["independent_qualification_unavailable"]


def test_positive_gate_fails_clearly_without_independent_labels() -> None:
    report = evaluate_independent_qualification(
        atoms=[_atom("atom:one")],
        accepted_outputs_by_kind={},
        manifest=None,
        output_adjudication=None,
        positive_throughput_required=True,
    )

    assert report["failures"] == ["independent_qualification_manifest_missing"]
