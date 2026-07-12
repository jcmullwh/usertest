from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from backlog_core.case_lineage import (
    apply_atom_disposition_decision,
    apply_atom_dispositions,
    assign_problem_case_ids,
    atom_disposition_receipt_errors,
    atom_disposition_summary,
    atom_is_independent_problem_evidence,
    attach_supporting_atoms_to_problem_cases,
    build_case_registry,
    eligible_problem_mining_atoms,
    load_case_registry,
    normalize_atom_lineage,
    problem_case_records_from_registry,
    propagate_case_lineage,
    record_lineage_context,
    update_case_registry_stage_lineage,
    verified_causal_identities_from_case_registry,
    verified_mechanism_identities_from_case_registry,
    write_case_registry,
)
from backlog_core.stage_contracts import (
    evidence_assignment_sha256,
    evidence_verification_sha256,
)
from backlog_core.ticket_readiness import assign_plan_revision_id, plan_revision_id_for


def _atom(atom_id: str, **overrides: object) -> dict[str, object]:
    atom: dict[str, object] = {
        "atom_id": atom_id,
        "run_id": atom_id.split(":", 1)[0],
        "source": "confusion_point",
        "severity_hint": "high",
        "mission_id": "ordinary_mission",
    }
    atom.update(overrides)
    return atom


def _evidence_assignment(*, case_id: str = "case:parent") -> dict[str, object]:
    snapshot = {"atom_id": "atom:origin", "text": "Original observed failure"}
    assignment: dict[str, object] = {
        "status": "complete",
        "errors": [],
        "case_id": case_id,
        "problem_id": "problem:original",
        "expected_atom_ids": ["atom:origin"],
        "atom_receipts": [
            {
                "atom_id": "atom:origin",
                "atom_sha256": evidence_assignment_sha256(snapshot),
                "atom_snapshot": snapshot,
                "source_projection_version": 1,
                "artifact_receipts": [
                    {
                        "path": "runs/origin/report.json",
                        "sha256": "a" * 64,
                        "size_bytes": 123,
                    }
                ],
                "origin_evidence_mode": "snapshot_and_artifacts",
            }
        ],
    }
    # The canonical algorithm is the same for atom snapshots and assignments: hash
    # the JSON object after excluding the assignment's self-hash field.
    assignment["assignment_sha256"] = evidence_assignment_sha256(assignment)
    return assignment


def test_record_lineage_context_retains_legacy_research_alias_without_trusting_it() -> None:
    context = record_lineage_context(
        {
            "target_ref": {"mission_id": "backlog_repro_research"},
            "report": {
                "extensions": {"backlog_repro_research": {"problem_id": "problem:original"}}
            },
        },
        run_id="target/run/agent/1",
    )

    assert context["origin_stage"] == "repro_research"
    assert context["evidence_role"] == "research"
    assert context["parent_problem_id"] is None
    assert context["parent_case_id"] is None
    assert context["disposition"] == "unresolved"
    assert context["legacy_parent_problem_id"] == "problem:original"
    assert "lineage_mining_blocker" not in context


def test_trusted_observation_lineage_claim_cannot_suppress_mining() -> None:
    context = record_lineage_context(
        {
            "target_ref": {},
            "report": {
                "extensions": {
                    "backlog_repro_research": {"problem_id": "problem:legacy-model-claim"}
                }
            },
        },
        run_id="target/run/agent/1",
    )
    atoms = normalize_atom_lineage(
        [_atom("target/run/agent/1:confusion_point:1", **context)],
        strict_new_output=True,
    )

    assert atoms[0]["evidence_role"] == "observation"
    assert atoms[0]["disposition"] == "unresolved"
    assert atoms[0]["disposition_status"] == "pending"
    assert [atom["atom_id"] for atom in eligible_problem_mining_atoms(atoms)] == [
        "target/run/agent/1:confusion_point:1"
    ]


def test_observation_mission_containing_verification_is_not_derived() -> None:
    context = record_lineage_context(
        {
            "target_ref": {
                "mission_id": "investigate_verification_path_failure",
            }
        },
        run_id="target/run/agent/1",
    )
    atom = normalize_atom_lineage(
        [_atom("target/run/agent/1:confusion_point:1", **context)],
        strict_new_output=True,
    )[0]

    assert atom["evidence_role"] == "observation"
    assert [item["atom_id"] for item in eligible_problem_mining_atoms([atom])] == [atom["atom_id"]]


@pytest.mark.parametrize(
    "origin_marker",
    [
        {"source": "idea"},
        {"category": "IDEA"},
        {"origin_stage": "external_idea"},
        {"idea_originated": True},
    ],
)
def test_external_idea_atoms_never_enter_automated_problem_mining(
    origin_marker: dict[str, object],
) -> None:
    atom = _atom(
        "target/run/agent/1:idea:1",
        evidence_role="observation",
        disposition="unresolved",
        **origin_marker,
    )

    assert eligible_problem_mining_atoms([atom]) == []


@pytest.mark.parametrize("disposition", ["duplicate", "expected_noise"])
def test_unproved_permanent_disposition_is_reopened_for_mining(disposition: str) -> None:
    atom = apply_atom_disposition_decision(
        _atom(
            "target/run/agent/1:confusion_point:1",
            evidence_role="observation",
            disposition="unresolved",
        ),
        disposition=disposition,
        source="problem_mining_evidence_partition",
        rationale="A model classified the atom without typed proof.",
    )

    assert "permanent_disposition_proof_missing" in atom_disposition_receipt_errors(
        atom,
        require_decided=True,
    )
    assert [item["atom_id"] for item in eligible_problem_mining_atoms([atom])] == [atom["atom_id"]]


def test_record_lineage_context_rejects_model_authored_novel_case_promotion() -> None:
    context = record_lineage_context(
        {
            "target_ref": {"mission_id": "backlog_repro_research"},
            "report": {
                "extensions": {
                    "backlog_lineage": {
                        "parent_case_id": "case:parent",
                        "evidence_role": "research",
                        "disposition": "novel_case",
                        "novel_case_rationale": "The research harness failed independently.",
                    }
                }
            },
        },
        run_id="target/run/agent/1",
    )

    assert context["origin_stage"] == "repro_research"
    assert context["evidence_role"] == "research"
    assert context["parent_case_id"] is None
    assert context["case_id"] is None
    assert context["disposition"] == "unresolved"
    assert "novel_case_rationale" not in context
    assert context["legacy_report_lineage_claims"]["backlog_lineage"] == {
        "parent_case_id": "case:parent",
        "evidence_role": "research",
        "disposition": "novel_case",
        "novel_case_rationale": "The research harness failed independently.",
    }
    assert "lineage_mining_blocker" not in context


def test_record_lineage_context_does_not_allow_model_to_relabel_runner_research() -> None:
    context = record_lineage_context(
        {
            "target_ref": {
                "requested_mission_id": "backlog_repro_research",
            },
            "report": {
                "extensions": {
                    "backlog_lineage": {
                        "origin_stage": "observation",
                        "evidence_role": "observation",
                        "disposition": "unresolved",
                    }
                }
            },
        },
        run_id="target/run/agent/1",
    )

    assert context["origin_stage"] == "repro_research"
    assert context["evidence_role"] == "research"
    assert context["disposition"] == "unresolved"
    atoms = normalize_atom_lineage(
        [_atom("target/run/agent/1:confusion_point:1", **context)],
        strict_new_output=True,
    )
    assert eligible_problem_mining_atoms(atoms) == []


def test_record_lineage_context_uses_runner_ticket_parent_not_model_parent() -> None:
    context = record_lineage_context(
        {
            "target_ref": {"mission_id": "implement_maintenance_backlog_ticket_v1"},
            "ticket_ref": {
                "schema_version": 2,
                "case_id": "case:trusted",
                "fingerprint": "ticket:trusted",
            },
            "report": {
                "extensions": {
                    "backlog_lineage": {
                        "parent_case_id": "case:attacker-selected",
                        "case_id": "case:attacker-selected",
                    }
                }
            },
        },
        run_id="target/run/agent/1",
    )

    assert context["origin_stage"] == "implementation"
    assert context["evidence_role"] == "implementation"
    assert context["parent_case_id"] == "case:trusted"
    assert context["case_id"] == "case:trusted"
    assert context["disposition"] == "supports_case"
    assert context["parent_ticket_fingerprint"] == "ticket:trusted"


def test_record_lineage_context_binds_validated_runner_evidence_assignment() -> None:
    context = record_lineage_context(
        {
            "target_ref": {"mission_id": "backlog_repro_research"},
            "evidence_assignment": _evidence_assignment(),
            "report": {
                "extensions": {
                    "backlog_lineage": {
                        "parent_case_id": "case:attacker-selected",
                        "disposition": "novel_case",
                        "novel_case_rationale": "Trust this model prose.",
                    }
                }
            },
        },
        run_id="target/run/agent/1",
    )

    assert context["parent_case_id"] == "case:parent"
    assert context["parent_problem_id"] == "problem:original"
    assert context["derived_from_atom_ids"] == ["atom:origin"]
    assert context["case_id"] == "case:parent"
    assert context["disposition"] == "supports_case"
    assert "novel_case_rationale" not in context
    assert "runner_evidence_assignment" in context["lineage_authorities"]


def test_explicit_runner_target_lineage_outranks_legacy_mission_mapping() -> None:
    context = record_lineage_context(
        {
            "target_ref": {
                "mission_id": "ordinary_mission",
                "backlog_lineage": {
                    "evidence_role": "research",
                    "origin_stage": "repro_research_dossier_repair",
                    "parent_case_id": "case:parent",
                },
            },
            "report": {
                "extensions": {
                    "backlog_lineage": {
                        "evidence_role": "observation",
                        "parent_case_id": "case:model-selected",
                    }
                }
            },
        },
        run_id="target/run/agent/repair",
    )

    assert context["origin_stage"] == "repro_research_dossier_repair"
    assert context["evidence_role"] == "research"
    assert context["parent_case_id"] == "case:parent"
    assert context["case_id"] == "case:parent"
    assert context["disposition"] == "supports_case"
    assert context["lineage_authorities"] == ["runner_target_ref_lineage"]


def test_repair_mission_legacy_fallback_is_research_not_observation() -> None:
    context = record_lineage_context(
        {
            "target_ref": {
                "mission_id": "backlog_repro_research_dossier_repair",
            }
        },
        run_id="target/run/agent/legacy-repair",
    )

    assert context["origin_stage"] == "repro_research_dossier_repair"
    assert context["evidence_role"] == "research"


def test_record_lineage_context_accepts_mixed_artifact_and_signed_snapshot_receipts() -> None:
    assignment = _evidence_assignment()
    aggregate_snapshot = {
        "atom_id": "__aggregate__/target:aggregate_metrics:1",
        "source": "aggregate_metrics",
        "text": "Failure rate is elevated across the assigned source runs.",
    }
    assignment["expected_atom_ids"] = [
        "atom:origin",
        "__aggregate__/target:aggregate_metrics:1",
    ]
    assignment["atom_receipts"].append(
        {
            "atom_id": "__aggregate__/target:aggregate_metrics:1",
            "atom_sha256": evidence_assignment_sha256(aggregate_snapshot),
            "atom_snapshot": aggregate_snapshot,
            "source_projection_version": 1,
            "artifact_receipts": [],
            "origin_evidence_mode": "signed_snapshot",
        }
    )
    assignment["assignment_sha256"] = evidence_assignment_sha256(assignment)

    context = record_lineage_context(
        {
            "target_ref": {"mission_id": "backlog_repro_research"},
            "evidence_assignment": assignment,
        },
        run_id="target/run/agent/1",
    )
    atoms = normalize_atom_lineage(
        [
            _atom("target/run/agent/1:command_failure:1", **context),
            _atom("target/run/agent/1:report_outcome:1", **context),
            _atom("target/run/agent/1:agent_stderr_artifact:1", **context),
        ],
        strict_new_output=True,
    )

    assert context.get("lineage_validation_errors") is None
    assert context["derived_from_atom_ids"] == assignment["expected_atom_ids"]
    assert {atom["parent_case_id"] for atom in atoms} == {"case:parent"}
    assert {atom["case_id"] for atom in atoms} == {"case:parent"}
    assert {atom["disposition"] for atom in atoms} == {"supports_case"}
    assert {atom["disposition_status"] for atom in atoms} == {"decided"}


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (
            {"source_projection_version": 2},
            "runner_evidence_assignment_source_projection_version_invalid:0",
        ),
        (
            {
                "origin_evidence_mode": "signed_snapshot",
            },
            "runner_evidence_assignment_signed_snapshot_has_artifacts:0:atom:origin",
        ),
    ],
)
def test_record_lineage_context_rejects_invalid_evidence_receipt_mode_or_projection(
    mutation: dict[str, object],
    expected_error: str,
) -> None:
    assignment = _evidence_assignment()
    assignment["atom_receipts"][0].update(mutation)
    assignment["assignment_sha256"] = evidence_assignment_sha256(assignment)

    context = record_lineage_context(
        {
            "target_ref": {"mission_id": "backlog_repro_research"},
            "evidence_assignment": assignment,
        },
        run_id="target/run/agent/1",
    )

    assert context["parent_case_id"] is None
    assert expected_error in context["lineage_validation_errors"]


def test_record_lineage_context_rejects_tampered_runner_evidence_assignment() -> None:
    assignment = _evidence_assignment()
    assignment["case_id"] = "case:tampered-without-receipt-update"
    context = record_lineage_context(
        {
            "target_ref": {"mission_id": "backlog_repro_research"},
            "evidence_assignment": assignment,
        },
        run_id="target/run/agent/1",
    )

    assert context["parent_case_id"] is None
    assert context["disposition"] == "unresolved"
    assert context["lineage_mining_blocker"] == "invalid_runner_lineage_assignment"
    assert "runner_evidence_assignment_hash_mismatch" in context["lineage_validation_errors"]


def test_disposition_receipt_distinguishes_pending_from_explicit_unresolved() -> None:
    pending = normalize_atom_lineage(
        [_atom("target/run/agent/1:confusion_point:1")],
        strict_new_output=True,
    )[0]
    assert pending["disposition"] == "unresolved"
    assert pending["disposition_status"] == "pending"
    assert atom_disposition_receipt_errors(pending, require_decided=True) == [
        "disposition_decision_pending"
    ]

    decided = apply_atom_disposition_decision(
        pending,
        disposition="unresolved",
        source="atom_action_ledger",
        rationale="The reviewer explicitly deferred classification pending a missing artifact.",
    )
    assert atom_disposition_receipt_errors(decided, require_decided=True) == []

    tampered = dict(decided)
    tampered["case_id"] = "case:receipt-does-not-bind-this"
    assert atom_disposition_receipt_errors(tampered, require_decided=True) == [
        "decided_disposition_receipt_mismatch"
    ]


def test_legacy_research_atom_resolves_parent_and_is_not_remined() -> None:
    registry = {
        "problem_id_to_case_id": {"problem:original": "case:abc"},
        "atom_id_to_case_id": {},
        "ticket_fingerprint_to_case_id": {},
    }
    atoms = normalize_atom_lineage(
        [
            _atom(
                "target/run/agent/1:confusion_point:1",
                mission_id="backlog_repro_research",
                parent_problem_id="problem:original",
            )
        ],
        case_registry=registry,
        strict_new_output=True,
    )

    assert atoms[0]["parent_case_id"] == "case:abc"
    assert atoms[0]["case_id"] == "case:abc"
    assert atoms[0]["disposition"] == "supports_case"
    assert eligible_problem_mining_atoms(atoms) == []


def test_persisted_case_membership_overrides_extractor_unresolved_default() -> None:
    atoms = normalize_atom_lineage(
        [
            _atom(
                "target/run/agent/1:confusion_point:1",
                disposition="unresolved",
            )
        ],
        case_registry={
            "problem_id_to_case_id": {},
            "atom_id_to_case_id": {"target/run/agent/1:confusion_point:1": "case:persisted"},
            "ticket_fingerprint_to_case_id": {},
        },
        strict_new_output=True,
    )

    assert atoms[0]["case_id"] == "case:persisted"
    assert atoms[0]["disposition"] == "supports_case"
    assert eligible_problem_mining_atoms(atoms) == []


def test_explicit_novel_derived_failure_is_eligible() -> None:
    atoms = normalize_atom_lineage(
        [
            _atom(
                "target/run/agent/1:run_failure_event:1",
                mission_id="backlog_repro_research",
                disposition="novel_case",
                parent_case_id="case:researched-problem",
                novel_case_rationale="The research runner itself crashed before investigation.",
            )
        ],
        strict_new_output=True,
    )

    assert [item["atom_id"] for item in eligible_problem_mining_atoms(atoms)] == [
        "target/run/agent/1:run_failure_event:1"
    ]
    assert atoms[0]["parent_case_id"] == "case:researched-problem"
    assert atoms[0]["case_id"] is None
    assert atom_is_independent_problem_evidence(atoms[0]) is True

    ledger_persisted = apply_atom_disposition_decision(
        atoms[0],
        disposition="novel_case",
        source="atom_action_ledger",
        rationale="The runner-owned ledger persisted the distinct failure decision.",
    )
    assert atom_is_independent_problem_evidence(ledger_persisted) is True

    records = assign_problem_case_ids(
        [
            {
                "problem_id": "problem:research-runner-crash",
                "evidence_atom_ids": [atoms[0]["atom_id"]],
            }
        ],
        atoms,
    )
    assert records[0]["case_id"] != "case:researched-problem"
    assert records[0]["related_case_ids"] == ["case:researched-problem"]

    dispositioned = apply_atom_dispositions(atoms, records)
    assert dispositioned[0]["disposition"] == "novel_case"
    assert dispositioned[0]["case_id"] == records[0]["case_id"]

    registry = build_case_registry(records)
    repeated = normalize_atom_lineage(
        [
            _atom(
                "target/run/agent/1:run_failure_event:1",
                mission_id="backlog_repro_research",
                parent_case_id="case:researched-problem",
            )
        ],
        case_registry=registry,
        strict_new_output=True,
    )
    assert repeated[0]["disposition"] == "novel_case"
    assert repeated[0]["case_id"] == records[0]["case_id"]
    assert atom_is_independent_problem_evidence(repeated[0]) is True
    assert eligible_problem_mining_atoms(repeated) == []


def test_ordinary_derived_parent_evidence_is_not_an_independent_work_unit() -> None:
    atom = normalize_atom_lineage(
        [
            _atom(
                "target/run/agent/1:research_note:1",
                mission_id="backlog_repro_research",
                parent_case_id="case:researched-problem",
            )
        ],
        strict_new_output=True,
    )[0]

    assert atom["disposition"] == "supports_case"
    assert atom_is_independent_problem_evidence(atom) is False


def test_deferred_source_observation_is_revisited_next_cycle() -> None:
    atom = normalize_atom_lineage(
        [_atom("target/run/agent/1:confusion_point:deferred")],
        strict_new_output=True,
    )[0]
    atom = apply_atom_disposition_decision(
        atom,
        disposition="deferred",
        source="problem_mining_evidence_partition",
        rationale="The first pass could not yet distinguish a real failure from context.",
    )
    atom["disposition_revisit_when"] = "The next full evidence cycle."

    eligible = eligible_problem_mining_atoms([atom])

    assert [item["atom_id"] for item in eligible] == [atom["atom_id"]]


def test_case_identity_uses_evidence_not_title_and_persists_aliases(tmp_path: Path) -> None:
    atoms = normalize_atom_lineage(
        [_atom("target/run/agent/1:confusion_point:1")],
        strict_new_output=True,
    )
    first = assign_problem_case_ids(
        [
            {
                "problem_id": "problem:first-wording",
                "title": "First generated title",
                "evidence_atom_ids": [atoms[0]["atom_id"]],
            }
        ],
        atoms,
    )
    registry = build_case_registry(first)
    registry_path = tmp_path / "cases.json"
    write_case_registry(registry_path, registry)

    loaded = load_case_registry(registry_path)
    second = assign_problem_case_ids(
        [
            {
                "problem_id": "problem:first-wording",
                "title": "Completely different generated title",
                "evidence_atom_ids": [atoms[0]["atom_id"]],
            }
        ],
        atoms,
        case_registry=loaded,
    )

    assert first[0]["case_id"] == second[0]["case_id"]
    assert json.loads(registry_path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_registry_preserves_case_context_for_future_relation_review() -> None:
    first = build_case_registry(
        [
            {
                "problem_id": "problem:shell",
                "canonical_problem_id": "problem:shell",
                "case_id": "case:shell",
                "case_member_problem_ids": ["problem:shell"],
                "evidence_atom_ids": ["run/one/agent/1:failure:1"],
                "title": "Shell probe reports the wrong cause",
                "problem": "A failed executable probe is attributed to policy.",
                "user_impact": "The mission still cannot execute commands.",
                "severity": "high",
                "confidence": 0.82,
                "problem_status": "identified",
                "root_cause_status": "unestablished",
            }
        ]
    )
    updated = build_case_registry(
        [
            {
                "problem_id": "problem:shell",
                "canonical_problem_id": "problem:shell",
                "case_id": "case:shell",
                "case_member_problem_ids": ["problem:shell", "problem:shell-recurrence"],
                "evidence_atom_ids": [
                    "run/two/agent/1:failure:1",
                ],
            }
        ],
        previous=first,
    )

    records = problem_case_records_from_registry(updated)
    assert len(records) == 1
    assert records[0]["title"] == "Shell probe reports the wrong cause"
    assert records[0]["canonical_symptoms"] == [
        "A failed executable probe is attributed to policy."
    ]
    assert records[0]["case_revision"] == 2
    assert records[0]["severity"] == "high"
    assert records[0]["confidence"] == 0.82
    assert records[0]["problem_status"] == "identified"
    assert records[0]["evidence_atom_ids"] == [
        "run/one/agent/1:failure:1",
        "run/two/agent/1:failure:1",
    ]
    assert records[0]["_historical_case_context"] is True


def test_registry_persists_same_class_recurrence_against_resolving_plan() -> None:
    previous = build_case_registry(
        [
            {
                "problem_id": "problem:shell",
                "canonical_problem_id": "problem:shell",
                "case_id": "case:shell",
                "case_member_problem_ids": ["problem:shell"],
                "evidence_atom_ids": ["run/one/agent/1:failure:1"],
                "case_state": "resolved",
            }
        ]
    )
    previous["cases"]["case:shell"]["current_lifecycle"] = {
        "state": "resolved",
        "outcome_reference": {
            "source": "provenance_verified_plan_outcome",
            "plan_revision_id": "plan:shell:v1",
        },
    }

    updated = build_case_registry(
        [
            {
                "problem_id": "problem:shell-recurrence",
                "canonical_problem_id": "problem:shell",
                "case_id": "case:shell",
                "case_member_problem_ids": [
                    "problem:shell",
                    "problem:shell-recurrence",
                ],
                "evidence_atom_ids": [
                    "run/one/agent/1:failure:1",
                    "run/two/agent/1:failure:1",
                ],
                "case_state": "active",
                "reopened_from_state": "resolved",
            }
        ],
        previous=previous,
    )

    case = updated["cases"]["case:shell"]
    assert case["state"] == "active"
    assert case["recurrence_reopen"] == {
        "from_state": "resolved",
        "against_plan_revision_id": "plan:shell:v1",
        "case_revision": 2,
        "new_evidence_atom_ids": ["run/two/agent/1:failure:1"],
    }


def test_derived_supporting_atom_updates_parent_record_and_registry() -> None:
    initial = build_case_registry(
        [
            {
                "problem_id": "problem:parent",
                "case_id": "case:parent",
                "evidence_atom_ids": ["run/original/agent/1:failure:1"],
            }
        ]
    )
    derived = normalize_atom_lineage(
        [
            _atom(
                "run/research/agent/1:confusion_point:1",
                mission_id="backlog_repro_research",
                parent_problem_id="problem:parent",
            )
        ],
        case_registry=initial,
        strict_new_output=True,
    )
    active = problem_case_records_from_registry(initial)
    attached = attach_supporting_atoms_to_problem_cases(active, derived)
    updated = build_case_registry(
        attached,
        previous=initial,
        supporting_atoms=derived,
    )

    assert attached[0]["evidence_atom_ids"] == [
        "run/original/agent/1:failure:1",
        "run/research/agent/1:confusion_point:1",
    ]
    assert attached[0]["derived_evidence_atom_ids"] == ["run/research/agent/1:confusion_point:1"]
    assert attached[0]["source_evidence_atom_ids"] == ["run/original/agent/1:failure:1"]
    assert updated["cases"]["case:parent"]["evidence_atom_ids"] == attached[0]["evidence_atom_ids"]
    assert updated["cases"]["case:parent"]["source_evidence_atom_ids"] == [
        "run/original/agent/1:failure:1"
    ]
    assert updated["cases"]["case:parent"]["derived_evidence_atom_ids"] == [
        "run/research/agent/1:confusion_point:1"
    ]
    assert updated["atom_id_to_case_id"][derived[0]["atom_id"]] == "case:parent"


def test_split_parent_is_durable_nonactive_graph_node() -> None:
    children = [
        {
            "problem_id": "problem:parent:split:1",
            "case_id": "case:child-1",
            "evidence_atom_ids": ["run/one/agent/1:failure:1"],
            "split_from_case_id": "case:parent",
            "split_parent_problem_id": "problem:parent",
            "split_parent_problem_ids": ["problem:parent", "problem:parent-alias"],
        },
        {
            "problem_id": "problem:parent:split:2",
            "case_id": "case:child-2",
            "evidence_atom_ids": ["run/two/agent/1:failure:1"],
            "split_from_case_id": "case:parent",
            "split_parent_problem_id": "problem:parent",
            "split_parent_problem_ids": ["problem:parent", "problem:parent-alias"],
        },
    ]
    registry = build_case_registry(children)

    assert registry["cases"]["case:parent"]["state"] == "split"
    assert registry["cases"]["case:parent"]["child_case_ids"] == [
        "case:child-1",
        "case:child-2",
    ]
    assert registry["problem_id_to_case_id"]["problem:parent"] == "case:parent"
    assert registry["problem_id_to_case_id"]["problem:parent-alias"] == "case:parent"
    assert {record["case_id"] for record in problem_case_records_from_registry(registry)} == {
        "case:child-1",
        "case:child-2",
    }


def test_exact_identity_records_coalesce_before_research() -> None:
    atoms = normalize_atom_lineage(
        [_atom("target/run/agent/1:confusion_point:1")],
        strict_new_output=True,
    )
    records = assign_problem_case_ids(
        [
            {
                "problem_id": "problem:miner-a",
                "title": "A",
                "evidence_atom_ids": [atoms[0]["atom_id"]],
            },
            {
                "problem_id": "problem:miner-b",
                "title": "B",
                "evidence_atom_ids": [atoms[0]["atom_id"]],
            },
        ],
        atoms,
    )

    assert len(records) == 1
    assert records[0]["case_member_problem_ids"] == ["problem:miner-a", "problem:miner-b"]


def test_new_problem_record_rejects_unknown_atom_reference() -> None:
    with pytest.raises(ValueError, match="unknown evidence atom IDs"):
        assign_problem_case_ids(
            [
                {
                    "problem_id": "problem:bad",
                    "evidence_atom_ids": ["missing:atom"],
                }
            ],
            [],
        )


def test_problem_case_cannot_be_originated_or_supported_by_proposals_alone() -> None:
    proposal, observation = normalize_atom_lineage(
        [
            _atom(
                "target/run/agent/1:suggested_change:1",
                source="suggested_change",
            ),
            _atom(
                "target/run/agent/1:report_outcome:1",
                source="report_outcome",
            ),
        ],
        strict_new_output=True,
    )

    assert proposal["evidence_class"] == "proposal"
    assert observation["evidence_class"] == "observed"
    with pytest.raises(ValueError, match="proposal-only evidence"):
        assign_problem_case_ids(
            [
                {
                    "problem_id": "problem:proposal-only",
                    "evidence_atom_ids": [proposal["atom_id"]],
                }
            ],
            [proposal, observation],
        )

    assigned = assign_problem_case_ids(
        [
            {
                "problem_id": "problem:observed-with-proposal-context",
                "evidence_atom_ids": [observation["atom_id"], proposal["atom_id"]],
            }
        ],
        [proposal, observation],
    )
    assert assigned[0]["evidence_atom_ids"] == [
        observation["atom_id"],
        proposal["atom_id"],
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("case_id", "case:model-forged"),
        ("canonical_problem_id", "problem:model-forged"),
        ("case_member_problem_ids", ["problem:model-forged"]),
        ("case_revision", 99),
        ("source_evidence_atom_ids", ["atom:model-forged"]),
        ("derived_evidence_atom_ids", ["atom:model-forged"]),
        ("case_identity_status", "resolved"),
        ("case_identity_candidate_ids", ["case:model-forged"]),
    ],
)
def test_new_problem_record_rejects_model_supplied_server_case_identity(
    field: str, value: object
) -> None:
    atom = normalize_atom_lineage(
        [_atom("target/run/agent/1:confusion_point:1")],
        strict_new_output=True,
    )[0]
    with pytest.raises(ValueError, match="model supplied server-owned case fields"):
        assign_problem_case_ids(
            [
                {
                    "problem_id": "problem:model-output",
                    "evidence_atom_ids": [atom["atom_id"]],
                    field: value,
                }
            ],
            [atom],
        )


def test_legacy_problem_case_identity_is_stripped_and_server_registry_wins() -> None:
    atom = normalize_atom_lineage(
        [_atom("target/run/agent/1:confusion_point:1")],
        strict_new_output=True,
    )[0]
    assigned = assign_problem_case_ids(
        [
            {
                "problem_id": "problem:known",
                "case_id": "case:model-forged",
                "canonical_problem_id": "problem:model-forged",
                "evidence_atom_ids": [atom["atom_id"]],
            }
        ],
        [atom],
        case_registry={
            "problem_id_to_case_id": {"problem:known": "case:server"},
            "atom_id_to_case_id": {},
            "ticket_fingerprint_to_case_id": {},
        },
        strict_new_output=False,
    )

    assert assigned[0]["case_id"] == "case:server"
    assert assigned[0]["canonical_problem_id"] == "problem:known"


def test_multi_case_evidence_requires_relation_without_minting_third_case() -> None:
    atom_a = "target/run-a/agent/1:confusion_point:1"
    atom_b = "target/run-b/agent/1:confusion_point:1"
    registry = {
        "schema_version": 1,
        "cases": {
            "case:a": {"case_id": "case:a", "state": "active"},
            "case:b": {"case_id": "case:b", "state": "active"},
        },
        "problem_id_to_case_id": {},
        "atom_id_to_case_id": {atom_a: "case:a", atom_b: "case:b"},
        "atom_id_to_case_ids": {atom_a: ["case:a"], atom_b: ["case:b"]},
        "ticket_fingerprint_to_case_id": {},
        "operational_signature_to_case_id": {},
    }
    atoms = normalize_atom_lineage(
        [_atom(atom_a), _atom(atom_b)],
        case_registry=registry,
        strict_new_output=True,
    )

    assigned = assign_problem_case_ids(
        [
            {
                "problem_id": "problem:ambiguous-linkage",
                "title": "The observations may be related",
                "evidence_atom_ids": [atom_a, atom_b],
            }
        ],
        atoms,
        case_registry=registry,
    )

    assert len(assigned) == 1
    assert assigned[0]["case_id"] in {"case:a", "case:b"}
    assert assigned[0]["case_identity_status"] == "pending_relation"
    assert assigned[0]["case_identity_candidate_ids"] == ["case:a", "case:b"]


@pytest.mark.parametrize("reverse", [False, True])
def test_coalesced_identity_state_join_is_order_independent(reverse: bool) -> None:
    atom_a = "target/run-a/agent/1:confusion_point:1"
    atom_b = "target/run-b/agent/1:confusion_point:1"
    registry = {
        "schema_version": 1,
        "cases": {
            "case:a": {"case_id": "case:a", "state": "active"},
            "case:b": {"case_id": "case:b", "state": "active"},
        },
        "problem_id_to_case_id": {},
        "atom_id_to_case_id": {atom_a: "case:a", atom_b: "case:b"},
        "atom_id_to_case_ids": {atom_a: ["case:a"], atom_b: ["case:b"]},
        "ticket_fingerprint_to_case_id": {},
        "operational_signature_to_case_id": {},
    }
    atoms = normalize_atom_lineage(
        [_atom(atom_a), _atom(atom_b)],
        case_registry=registry,
        strict_new_output=True,
    )
    records = [
        {
            "problem_id": "problem:resolved",
            "evidence_atom_ids": [atom_a],
        },
        {
            "problem_id": "problem:pending",
            "evidence_atom_ids": [atom_a, atom_b],
        },
    ]
    if reverse:
        records.reverse()

    assigned = assign_problem_case_ids(records, atoms, case_registry=registry)

    assert len(assigned) == 1
    assert assigned[0]["case_id"] == "case:a"
    assert assigned[0]["case_identity_status"] == "pending_relation"
    assert assigned[0]["case_identity_candidate_ids"] == ["case:a", "case:b"]
    assert set(assigned[0]["evidence_atom_ids"]) == {atom_a, atom_b}


def _complete_provisional_group() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "research_hypothesis",
        "group_id": "cause:provisional",
        "research_unit_case_id": "case:a",
        "member_case_ids": ["case:a", "case:b"],
        "member_problem_ids": ["problem:a", "problem:b"],
        "member_facets": [
            {
                "case_id": "case:a",
                "problem_id": "problem:a",
                "evidence_atom_ids": ["atom:a"],
                "source_evidence_atom_ids": ["atom:a"],
            },
            {
                "case_id": "case:b",
                "problem_id": "problem:b",
                "evidence_atom_ids": ["atom:b"],
                "source_evidence_atom_ids": ["atom:b"],
            },
        ],
    }


def test_registry_cycle_preserves_complete_provisional_group_and_facets() -> None:
    group = _complete_provisional_group()
    previous = build_case_registry(
        [
            {
                "case_id": "case:a",
                "problem_id": "problem:a",
                "evidence_atom_ids": ["atom:a"],
                "source_evidence_atom_ids": ["atom:a"],
                "case_identity_status": "provisional_same_cause",
                "case_identity_candidate_ids": ["case:a", "case:b"],
                "provisional_same_cause_group": group,
            },
            {
                "case_id": "case:b",
                "problem_id": "problem:b",
                "evidence_atom_ids": ["atom:b"],
                "source_evidence_atom_ids": ["atom:b"],
                "case_identity_status": "provisional_same_cause",
                "case_identity_candidate_ids": ["case:a", "case:b"],
                "provisional_same_cause_group": group,
            },
        ]
    )

    current = build_case_registry(
        [
            {
                "case_id": "case:a",
                "problem_id": "problem:a:new-observation",
                "evidence_atom_ids": ["atom:a"],
                "source_evidence_atom_ids": ["atom:a"],
                "case_identity_status": "resolved",
            }
        ],
        previous=previous,
    )

    entry = current["cases"]["case:a"]
    assert entry["case_identity_status"] == "provisional_same_cause"
    assert entry["case_identity_candidate_ids"] == ["case:a", "case:b"]
    assert entry["provisional_same_cause_group"] == group
    assert len(entry["provisional_same_cause_group"]["member_facets"]) == 2


def test_incomplete_historical_provisional_group_is_locally_pending() -> None:
    incomplete_group = _complete_provisional_group()
    incomplete_group["member_facets"] = incomplete_group["member_facets"][:1]
    previous = build_case_registry(
        [
            {
                "case_id": "case:a",
                "problem_id": "problem:a",
                "evidence_atom_ids": ["atom:a"],
                "source_evidence_atom_ids": ["atom:a"],
                "case_identity_status": "provisional_same_cause",
                "case_identity_candidate_ids": ["case:a", "case:b"],
                "provisional_same_cause_group": incomplete_group,
            }
        ]
    )

    entry = previous["cases"]["case:a"]
    assert entry["case_identity_status"] == "pending_relation"
    assert "provisional_same_cause_facets_incomplete" in entry[
        "provisional_same_cause_integrity_errors"
    ]
    assert problem_case_records_from_registry(previous)[0]["case_id"] == "case:a"


def test_atom_dispositions_and_group_lineage_propagate() -> None:
    atoms = normalize_atom_lineage(
        [
            _atom("target/run/agent/1:confusion_point:1"),
            _atom("target/run/agent/2:confusion_point:1"),
        ],
        strict_new_output=True,
    )
    problem_case = {
        "problem_id": "problem:a",
        "canonical_problem_id": "problem:a",
        "case_id": "case:a",
        "case_member_problem_ids": ["problem:a", "problem:alias"],
        "same_cause_group_id": "cause:one",
        "evidence_atom_ids": [atoms[0]["atom_id"]],
    }
    updated = apply_atom_dispositions(atoms, [problem_case])
    downstream = propagate_case_lineage(
        [{"problem_id": "problem:a", "selected_for_research": True}],
        [problem_case],
    )

    assert updated[0]["disposition"] == "supports_case"
    assert updated[1]["disposition"] == "unresolved"
    summary = atom_disposition_summary(updated)
    assert summary["high_severity_unresolved_count"] == 1
    assert summary["high_severity_pending_count"] == 1
    assert summary["decision_status_counts"] == {"decided": 1, "pending": 1}
    assert downstream[0]["case_id"] == "case:a"
    assert downstream[0]["same_cause_group_id"] == "cause:one"


def test_source_atom_can_support_distinct_canonical_facets_without_losing_evidence() -> None:
    atom_id = "target/run/agent/1:confusion_point:1"
    atoms = normalize_atom_lineage([_atom(atom_id)], strict_new_output=True)
    shell_case = {
        "problem_id": "problem:shell",
        "canonical_problem_id": "problem:shell",
        "case_id": "case:shell",
        "case_member_problem_ids": ["problem:shell"],
        "evidence_atom_ids": [atom_id],
    }
    smoke_case = {
        "problem_id": "problem:smoke",
        "canonical_problem_id": "problem:smoke",
        "case_id": "case:smoke",
        "case_member_problem_ids": ["problem:smoke"],
        "evidence_atom_ids": [atom_id],
    }

    dispositioned = apply_atom_dispositions(atoms, [smoke_case, shell_case])

    assert dispositioned[0]["disposition"] == "supports_case"
    assert dispositioned[0]["case_id"] == "case:shell"
    assert dispositioned[0]["supporting_case_ids"] == ["case:shell", "case:smoke"]
    assert eligible_problem_mining_atoms(dispositioned) == []

    attached = attach_supporting_atoms_to_problem_cases([shell_case, smoke_case], dispositioned)
    assert all(case["evidence_atom_ids"] == [atom_id] for case in attached)

    registry = build_case_registry(attached, supporting_atoms=dispositioned)
    assert registry["atom_id_to_case_id"][atom_id] == "case:shell"
    assert registry["atom_id_to_case_ids"][atom_id] == ["case:shell", "case:smoke"]
    assert atom_id in registry["cases"]["case:shell"]["evidence_atom_ids"]
    assert atom_id in registry["cases"]["case:smoke"]["evidence_atom_ids"]


def test_persisted_primary_for_multi_case_source_atom_survives_order_and_normalization() -> None:
    atom_id = "target/run/agent/1:confusion_point:1"
    previous = {
        "schema_version": 1,
        "cases": {
            "case:a": {"case_id": "case:a", "state": "active"},
            "case:z": {"case_id": "case:z", "state": "active"},
        },
        "problem_id_to_case_id": {},
        "atom_id_to_case_id": {atom_id: "case:z"},
        "atom_id_to_case_ids": {atom_id: ["case:a", "case:z"]},
        "ticket_fingerprint_to_case_id": {},
    }
    normalized = normalize_atom_lineage(
        [_atom(atom_id, disposition="unresolved")],
        case_registry=previous,
        strict_new_output=True,
    )
    assert normalized[0]["case_id"] == "case:z"
    assert normalized[0]["supporting_case_ids"] == ["case:a", "case:z"]
    assert normalized[0]["disposition"] == "supports_case"

    cases = [
        {
            "problem_id": "problem:a",
            "case_id": "case:a",
            "evidence_atom_ids": [atom_id],
        },
        {
            "problem_id": "problem:z",
            "case_id": "case:z",
            "evidence_atom_ids": [atom_id],
        },
    ]
    updated = build_case_registry(
        list(reversed(cases)), previous=previous, supporting_atoms=normalized
    )
    assert updated["atom_id_to_case_id"][atom_id] == "case:z"
    assert updated["atom_id_to_case_ids"][atom_id] == ["case:a", "case:z"]


def test_derived_evidence_cannot_be_assigned_to_multiple_canonical_cases() -> None:
    atom_id = "target/research/agent/1:confusion_point:1"
    atoms = normalize_atom_lineage(
        [
            _atom(
                atom_id,
                mission_id="backlog_repro_research",
                parent_case_id="case:parent",
            )
        ],
        strict_new_output=True,
    )
    cases = [
        {
            "problem_id": "problem:parent",
            "case_id": "case:parent",
            "evidence_atom_ids": [atom_id],
        },
        {
            "problem_id": "problem:other",
            "case_id": "case:other",
            "evidence_atom_ids": [atom_id],
        },
    ]

    with pytest.raises(ValueError, match="derived atom .* supports multiple canonical cases"):
        apply_atom_dispositions(atoms, cases)


def _stage_doc(
    stage: str,
    items: list[dict[str, object]],
    *,
    suffix: str,
    input_meta: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "stage": stage,
        "generated_at": f"2026-07-09T00:00:0{suffix}Z",
        "items": items,
        "input_meta": input_meta or {},
        "artifacts": {
            f"{stage}_json": f"compiled/case.{stage}.{suffix}.json",
            f"{stage}_md": f"compiled/case.{stage}.{suffix}.md",
        },
    }


def test_stage_lineage_is_cumulative_and_carried_cases_reuse_proof_context() -> None:
    registry = build_case_registry(
        [
            {
                "problem_id": "problem:one",
                "case_id": "case:one",
                "evidence_atom_ids": ["run/one/agent/1:failure:1"],
                "absorbed_case_ids": ["case:old-alias"],
                "title": "Original failure",
                "problem": "The original scenario fails.",
                "user_impact": "The workflow cannot complete.",
            }
        ]
    )
    original_evidence = list(registry["cases"]["case:one"]["evidence_atom_ids"])

    research = {
        "case_id": "case:one",
        "problem_id": "problem:one",
        "research_schema_version": 2,
        "repo_revision": "abc123",
        "research_method": "reproduction",
        "reproduction_status": "reproduced",
        "research_status": "evidence_sufficient",
        "root_cause_confidence": 0.91,
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "hypothesis:one",
                "statement": "A missing guard causes the failure.",
            }
        ],
        "material_unknowns": [
            {
                "unknown": "Whether a second caller bypasses the guard.",
                "affects": ["scope"],
                "evidence_needed": "Trace the second caller.",
            }
        ],
        "blocking_reasons": [],
        "artifact_refs": [{"kind": "test_output", "path": "repro.txt"}],
    }
    registry = update_case_registry_stage_lineage(
        registry,
        stage_doc=_stage_doc("repro_research", [research], suffix="1"),
    )

    options = [
        {
            "case_id": "case:one",
            "problem_id": "problem:one",
            "option_id": "option:one:direct",
            "family_id": "most_direct",
        },
        {
            "case_id": "case:one",
            "problem_id": "problem:one",
            "option_id": "option:one:bounded",
            "family_id": "bounded_shared",
        },
    ]
    registry = update_case_registry_stage_lineage(
        registry,
        stage_doc=_stage_doc(
            "solution_optioning",
            options,
            suffix="2",
            input_meta={
                "optioning_outcomes": [
                    {
                        "problem_id": "problem:one",
                        "optioning_status": "options_produced",
                    }
                ]
            },
        ),
    )

    selection = {
        "case_id": "case:one",
        "problem_id": "problem:one",
        "selected_option_id": "option:one:direct",
        "selected_family_id": "most_direct",
        "selection_status": "selected",
        "selected_option": {
            "causal_coverage": {
                "unsupported_assumptions": ["The second caller is equivalent."],
                "residual_recurrence_paths": [],
                "compatibility_risks": [],
            }
        },
        "falsification_review": {
            "verdict": "accept",
            "evidence_refs": [
                {
                    "ref": "experiment:one",
                    "finding": "The guard is absent on the failing path.",
                    "effect": "challenges_selection",
                }
            ],
            "material_risk_dispositions": [
                {
                    "risk": "The second caller is equivalent.",
                    "disposition": "accepted",
                    "evidence_refs": ["experiment:one"],
                    "rationale": "The plan preserves the caller boundary.",
                }
            ],
        },
        "_parse_warning": "legacy optional label omitted",
    }
    registry = update_case_registry_stage_lineage(
        registry,
        stage_doc=_stage_doc(
            "solution_selection",
            [selection],
            suffix="3",
            input_meta={
                "selection_outcomes": [
                    {
                        "problem_id": "problem:one",
                        "selection_status": "selected",
                        "selected_option_id": "option:one:direct",
                        "falsification_verdict": "accept",
                    }
                ]
            },
        ),
    )

    plan = assign_plan_revision_id(
        {
            "case_id": "case:one",
            "problem_id": "problem:one",
            "change_plan_id": "plan:one",
            "selected_option_id": "option:one:direct",
            "repo_revision": "abc123",
            "change_plan_status": "planned",
        }
    )
    plan_revision_id = str(plan["plan_revision_id"])
    registry = update_case_registry_stage_lineage(
        registry,
        stage_doc=_stage_doc("implementation_planning", [plan], suffix="4"),
    )
    ticket = {
        "case_id": "case:one",
        "problem_id": "problem:one",
        "plan_revision_id": plan_revision_id,
        "ticket_fingerprint": "0123456789abcdef",
        "ticket_stage": "ready_for_ticket",
    }
    ticket_doc = _stage_doc("ticket_assembly", [ticket], suffix="5")
    registry = update_case_registry_stage_lineage(registry, stage_doc=ticket_doc)
    # Replaying identical content must update the current pointer without duplicating history.
    registry = update_case_registry_stage_lineage(registry, stage_doc=ticket_doc)

    entry = registry["cases"]["case:one"]
    assert entry["evidence_atom_ids"] == original_evidence
    assert entry["root_cause_status"] == "established"
    assert entry["root_cause_confidence"] == 0.91
    assert entry["material_unknown_summary"][0]["affects"] == ["scope"]
    assert entry["current_option_set"]["option_ids"] == [
        "option:one:direct",
        "option:one:bounded",
    ]
    assert entry["current_selection"]["falsification_status"] == "accept"
    assert entry["current_selection"]["falsification_evidence_refs"][0]["ref"] == ("experiment:one")
    assert entry["current_selection"]["material_risks"] == ["The second caller is equivalent."]
    assert entry["current_selection"]["_parse_warning"] == ("legacy optional label omitted")
    assert entry["plan_revision_ids"] == [plan_revision_id]
    assert entry["plan_revisions"][plan_revision_id]["plan_revision_source"] == (
        "server_content_addressed_v1"
    )
    assert entry["plan_revisions"][plan_revision_id]["evidence_atom_ids_at_plan"] == (
        original_evidence
    )
    assert (
        entry["plan_revisions"][plan_revision_id]["source_evidence_atom_ids_at_plan"]
        == original_evidence
    )
    baseline_revision = entry["plan_revisions"][plan_revision_id]["case_revision_at_plan"]
    assert entry["state"] == "planned"
    assert entry["current_lifecycle"]["outcome_reference"]["source"] == (
        "validated_change_plan_artifact"
    )
    assert entry["ticket_fingerprints"] == ["0123456789abcdef"]
    assert registry["ticket_fingerprint_to_case_id"]["0123456789abcdef"] == ("case:one")
    assert len(entry["stage_artifact_refs"]["ticket_assembly"]) == 1
    assert registry["cases"]["case:old-alias"]["alias_of"] == "case:one"

    # A later cycle may see new evidence while still carrying the same historical plan.
    # Replaying that plan must not rewrite the baseline used by recurrence proof.
    entry["evidence_atom_ids"].append("run/two/agent/1:failure:2")
    entry["case_revision"] = int(entry["case_revision"]) + 1
    registry = update_case_registry_stage_lineage(
        registry,
        stage_doc=_stage_doc("implementation_planning", [plan], suffix="4b"),
    )
    retained_revision = registry["cases"]["case:one"]["plan_revisions"][plan_revision_id]
    assert retained_revision["evidence_atom_ids_at_plan"] == original_evidence
    assert retained_revision["source_evidence_atom_ids_at_plan"] == original_evidence
    assert retained_revision["case_revision_at_plan"] == baseline_revision

    blocked_research = dict(research)
    blocked_research.update(
        {
            "repo_revision": "def456",
            "reproduction_status": "blocked",
            "research_status": "blocked",
            "root_cause_confidence": 0.2,
            "blocking_reasons": ["The isolated service was unavailable."],
        }
    )
    registry = update_case_registry_stage_lineage(
        registry,
        stage_doc=_stage_doc("repro_research", [blocked_research], suffix="6"),
    )
    entry = registry["cases"]["case:one"]
    assert len(entry["research_proof_history"]) == 2
    assert entry["current_research_proof"]["research_status"] == "blocked"
    assert entry["best_research_proof"]["research_status"] == "evidence_sufficient"
    assert entry["root_cause_status"] == "established"

    carried = problem_case_records_from_registry(registry)[0]
    prior = carried["prior_stage_context"]
    assert prior["research"]["current"]["repo_revision"] == "def456"
    assert prior["research"]["best_available"]["repo_revision"] == "abc123"
    assert prior["selection"]["selected_option_id"] == "option:one:direct"
    assert prior["planning"]["current_plan_revisions"] == {
        plan_revision_id: entry["plan_revisions"][plan_revision_id]
    }


def test_registry_mechanism_identity_comes_only_from_verified_runner_receipt() -> None:
    registry = build_case_registry(
        [
            {
                "problem_id": "problem:one",
                "case_id": "case:one",
                "evidence_atom_ids": ["run/one/agent/1:failure:1"],
            }
        ]
    )
    projection = {
        "schema_version": 1,
        "mechanism_symbols": ["route_request"],
        "code_paths": [
            {"symbol": "route_request", "path": "src/router.py"},
        ],
        "control_points": ["route_request:policy_branch"],
    }
    projection_digest = sha256(
        json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    provenance = {
        "schema_version": 1,
        "primary_hypothesis_id": "hypothesis:one",
        "mechanism_evidence_ids": ["mechanism_evidence:one"],
        "causal_control_ids": ["control:one"],
        "falsification_intervention_ids": ["intervention:one"],
        "deterministic_closure_ids": ["closure:one"],
        "research_probe_control_points": [],
    }
    provenance_digest = sha256(
        json.dumps(
            provenance,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    verification = {
        "status": "verified",
        "verified_mechanism": projection,
        "verified_mechanism_sha256": projection_digest,
        "verified_mechanism_provenance": provenance,
        "verified_mechanism_provenance_sha256": provenance_digest,
        "errors": [],
    }
    verification["receipt_sha256"] = evidence_verification_sha256(verification)
    research = {
        "case_id": "case:one",
        "problem_id": "problem:one",
        "research_schema_version": 2,
        "repo_revision": "abc123",
        "research_method": "reproduction",
        "reproduction_status": "reproduced",
        "research_status": "evidence_sufficient",
        "root_cause_confidence": 0.95,
        "root_cause_hypotheses": [],
        "material_unknowns": [],
        "blocking_reasons": [],
        "artifact_refs": [],
        "evidence_verification": verification,
        # Same-named model prose is deliberately inconsistent and must be ignored.
        "verified_mechanism_sha256": "f" * 64,
    }

    registry = update_case_registry_stage_lineage(
        registry,
        stage_doc=_stage_doc("repro_research", [research], suffix="mechanism"),
    )

    assert verified_mechanism_identities_from_case_registry(registry) == {
        "case:one": projection_digest
    }
    carried = problem_case_records_from_registry(registry)[0]
    assert carried["verified_mechanism_sha256"] == projection_digest
    assert registry["cases"]["case:one"]["verified_mechanism_source"] == (
        "runner_research_evidence_verification_v1"
    )
    assert registry["cases"]["case:one"]["verified_mechanism_provenance"] == provenance

    registry["cases"]["case:one"]["verified_mechanism"] = {
        **projection,
        "mechanism_symbols": ["model_supplied_symbol"],
    }
    assert verified_mechanism_identities_from_case_registry(registry) == {}


def test_registry_exposes_only_runner_persisted_full_causal_identity() -> None:
    registry = build_case_registry(
        [
            {
                "problem_id": "problem:one",
                "case_id": "case:one",
                "evidence_atom_ids": ["atom:one"],
                "root_cause_status": "established",
                "verified_causal_signature_sha256": "a" * 64,
                "verified_causal_signature_source": "runner_verified_causal_signature_v1",
            }
        ]
    )

    assert verified_causal_identities_from_case_registry(registry) == {
        "case:one": "a" * 64
    }
    registry["cases"]["case:one"]["verified_causal_signature_source"] = "model_claim"
    assert verified_causal_identities_from_case_registry(registry) == {}


def test_stage_lineage_rejects_records_without_a_persisted_case() -> None:
    registry = build_case_registry(
        [
            {
                "problem_id": "problem:known",
                "case_id": "case:known",
                "evidence_atom_ids": ["run/one/agent/1:failure:1"],
            }
        ]
    )
    first = _stage_doc(
        "problem_mining",
        [{"case_id": "case:known", "problem_id": "problem:known"}],
        suffix="0",
    )
    registry = update_case_registry_stage_lineage(registry, stage_doc=first)
    carried = _stage_doc(
        "problem_mining",
        [
            {
                "case_id": "case:known",
                "problem_id": "problem:known",
                "prior_stage_context": {
                    "artifact_refs": registry["cases"]["case:known"]["current_stage_artifact_refs"]
                },
                "_historical_case_context": True,
            }
        ],
        suffix="0",
    )
    registry = update_case_registry_stage_lineage(registry, stage_doc=carried)
    assert len(registry["cases"]["case:known"]["stage_artifact_refs"]["problem_mining"]) == 1

    with pytest.raises(ValueError, match="no known case identity"):
        update_case_registry_stage_lineage(
            registry,
            stage_doc=_stage_doc(
                "repro_research",
                [{"problem_id": "problem:unknown"}],
                suffix="1",
            ),
        )

    with pytest.raises(ValueError, match="invalid plan_revision_source"):
        invalid_plan = {
            "case_id": "case:known",
            "problem_id": "problem:known",
            "plan_revision_source": "model_supplied",
        }
        invalid_plan["plan_revision_id"] = plan_revision_id_for(invalid_plan)
        update_case_registry_stage_lineage(
            registry,
            stage_doc=_stage_doc(
                "implementation_planning",
                [invalid_plan],
                suffix="2",
            ),
        )


def test_absorbed_case_stage_history_moves_to_canonical_without_deleting_alias() -> None:
    registry = build_case_registry(
        [
            {
                "problem_id": "problem:canonical",
                "case_id": "case:canonical",
                "evidence_atom_ids": ["atom:canonical"],
            },
            {
                "problem_id": "problem:absorbed",
                "case_id": "case:absorbed",
                "evidence_atom_ids": ["atom:absorbed"],
            },
        ]
    )
    registry = update_case_registry_stage_lineage(
        registry,
        stage_doc=_stage_doc(
            "repro_research",
            [
                {
                    "case_id": "case:absorbed",
                    "problem_id": "problem:absorbed",
                    "research_schema_version": 2,
                    "repo_revision": "abc123",
                    "research_method": "static_trace",
                    "reproduction_status": "reproduction_failed",
                    "research_status": "evidence_sufficient",
                    "root_cause_confidence": 0.88,
                    "root_cause_hypotheses": [{"hypothesis_id": "hypothesis:absorbed"}],
                    "material_unknowns": [],
                    "blocking_reasons": [],
                    "artifact_refs": [{"kind": "trace", "path": "trace.json"}],
                }
            ],
            suffix="1",
        ),
    )

    merged = build_case_registry(
        [
            {
                "problem_id": "problem:canonical",
                "canonical_problem_id": "problem:canonical",
                "case_id": "case:canonical",
                "case_member_problem_ids": [
                    "problem:canonical",
                    "problem:absorbed",
                ],
                "evidence_atom_ids": ["atom:canonical", "atom:absorbed"],
                "absorbed_case_ids": ["case:absorbed"],
            }
        ],
        previous=registry,
    )

    canonical = merged["cases"]["case:canonical"]
    assert canonical["best_research_proof"]["repo_revision"] == "abc123"
    assert len(canonical["research_proof_history"]) == 1
    assert canonical["stage_artifact_refs"]["repro_research"]
    assert merged["cases"]["case:absorbed"]["state"] == "alias"
    assert merged["cases"]["case:absorbed"]["alias_of"] == "case:canonical"
