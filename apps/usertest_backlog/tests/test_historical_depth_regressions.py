from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from backlog_core.case_lineage import (
    atom_is_idea_originated,
    eligible_problem_mining_atoms,
    normalize_atom_lineage,
)
from backlog_core.relation_review import canonicalize_problem_cases
from backlog_core.stage_contracts import (
    assess_research_readiness,
    parse_change_plan_list,
    parse_problem_record_list,
    parse_research_dossier_list,
)
from backlog_miner.pipeline import load_pipeline_prompt_manifest
from backlog_repo import (
    outcome_suppresses_new_case_discovery,
    ticket_export_fingerprint,
    validate_outcome_record,
    write_case_relation_receipt,
)
from runner_core import RunnerConfig
from runner_core.runner import _run_verification_commands
from runner_core.shell_capability import _resolve_shell_capability

from usertest_backlog.workflows.depth_contracts import change_plan_quality_errors
from usertest_backlog.workflows.prioritization import (
    _enforce_full_drain_research_policy,
    _server_normalize_priority_decisions,
)
from usertest_backlog.workflows.problem_mining import (
    _run_problem_mining_stage,
    _verified_relation_edges_from_case_registry,
)

_BENCHMARK_PATH = Path(__file__).parent / "fixtures" / "historical_depth_benchmark.json"
_BENCHMARK = json.loads(_BENCHMARK_PATH.read_text(encoding="utf-8"))
_CASES = {str(item["benchmark_id"]): item for item in _BENCHMARK["cases"] if isinstance(item, dict)}
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _benchmark_case(benchmark_id: str) -> dict[str, Any]:
    return dict(_CASES[benchmark_id])


def _canonical_sha256(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _source_atoms(benchmark_id: str) -> list[dict[str, Any]]:
    case = _benchmark_case(benchmark_id)
    return [
        dict(item["snapshot"])
        for item in case["source_evidence"]
        if isinstance(item, dict)
        and isinstance(item.get("snapshot"), dict)
        and isinstance(item["snapshot"].get("atom_id"), str)
    ]


def _run_dry_problem_mining(
    tmp_path: Path,
    *,
    atoms: list[dict[str, Any]],
    case_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = load_pipeline_prompt_manifest(_REPO_ROOT / "configs" / "backlog_prompts")
    return _run_problem_mining_stage(
        repo_root=_REPO_ROOT,
        atoms=atoms,
        pipeline_manifest=manifest,
        artifacts_dir=tmp_path / "artifacts",
        out_json=tmp_path / "compiled" / "problem_records.json",
        out_md=tmp_path / "compiled" / "problem_records.md",
        agent="codex",
        model=None,
        cfg=RunnerConfig(
            repo_root=_REPO_ROOT,
            runs_dir=tmp_path / "runs",
            agents={},
            policies={},
        ),
        dry_run=True,
        stage_guidance_text="Mine concrete observed failures without inferring root cause.",
        case_registry=case_registry,
    )


def _outcome(case_id: str, state: str, *, requires_live: bool) -> dict[str, object]:
    return {
        "schema_version": 1,
        "case_id": case_id,
        "plan_revision_id": f"plan:{case_id}:v1",
        "state": state,
        "recorded_at": "2026-07-09T00:00:00Z",
        "requires_live_verification": requires_live,
        "target_branch": None,
        "merged_commit": None,
        "test_evidence": [],
        "original_scenario_evidence": [],
        "live_evidence": [],
        "remaining_risks": ["Historical outcome proof is incomplete."],
        "recurrence_check": {"status": "not_run"},
    }


def test_benchmark_is_hash_bound_non_idea_evidence_with_honest_integrity_labels() -> None:
    assert _BENCHMARK["schema_version"] == 2
    assert len(_CASES) == 7
    assert _BENCHMARK["captured_from"]["problem_records"].endswith("usertest.problem_records.json")
    manifest = _BENCHMARK["source_manifest"]
    assert manifest["raw_corpus_status"] == "partially_unavailable_integrity_unknown"
    assert manifest["excluded_origins"] == ["IDEA", "external_idea"]
    assert len(manifest["captured_artifact_bindings"]) == 3
    assert all(
        len(binding["sha256"]) == 64
        and binding["size_bytes"] > 0
        and binding["retention"] == "observed_in_external_source_checkout_not_vendored"
        for binding in manifest["captured_artifact_bindings"]
    )
    assert manifest["cases_sha256"] == _canonical_sha256(_BENCHMARK["cases"])
    assert manifest["retained_replay_sha256"] == _canonical_sha256(_BENCHMARK["retained_replay"])
    assert {item["kind"] for item in _CASES.values()} == {
        "canonical_grouping",
        "derived_lineage",
        "research_gate",
        "outcome_gate",
        "plan_revision",
    }
    for case in _CASES.values():
        expected_case_hash = case["case_sha256"]
        case_without_hash = {key: value for key, value in case.items() if key != "case_sha256"}
        assert expected_case_hash == _canonical_sha256(case_without_hash)
        assert case["source_evidence"]
        for evidence in case["source_evidence"]:
            assert evidence["origin_category"] != "IDEA"
            assert evidence["snapshot_sha256"] == _canonical_sha256(evidence["snapshot"])
            assert atom_is_idea_originated(evidence["snapshot"]) is False


def test_priority_bucket_cannot_permanently_suppress_a_canonical_problem() -> None:
    decisions = [
        {
            "problem_id": "problem:single-run-real-failure",
            "priority_bucket": "watch",
            "selected_for_research": False,
            "priority_status": "prioritized",
        },
        {
            "problem_id": "problem:malformed-output",
            "priority_bucket": "watch",
            "selected_for_research": False,
            "priority_status": "invalid_output",
        },
    ]

    _enforce_full_drain_research_policy(decisions)

    assert decisions[0]["selected_for_research"] is True
    assert decisions[1]["selected_for_research"] is True
    assert {decision["priority_status"] for decision in decisions} == {"prioritized"}


def test_priority_model_cannot_block_or_omit_a_canonical_case() -> None:
    records = [
        {
            "problem_id": "problem:blocked",
            "evidence_atom_ids": ["atom:blocked"],
        },
        {
            "problem_id": "problem:omitted",
            "evidence_atom_ids": ["atom:omitted"],
        },
    ]
    normalized, warnings = _server_normalize_priority_decisions(
        decisions=[
            {
                "problem_id": "problem:blocked",
                "priority_bucket": "watch",
                "selected_for_research": False,
                "priority_rationale": "The model attempted to defer it.",
                "priority_status": "blocked",
            }
        ],
        problem_records=records,
        signals_by_problem_id={
            "problem:blocked": {"bucket_candidate": "p2"},
            "problem:omitted": {"bucket_candidate": "p1"},
        },
    )

    assert [item["problem_id"] for item in normalized] == [
        "problem:blocked",
        "problem:omitted",
    ]
    assert all(item["selected_for_research"] is True for item in normalized)
    assert all(item["priority_status"] == "prioritized" for item in normalized)
    assert normalized[0]["model_priority_accepted"] is True
    assert normalized[1]["model_priority_accepted"] is False
    assert "prioritizer_missing_problem_id:problem:omitted" in warnings


def test_retained_artifacts_replay_through_current_depth_gates() -> None:
    """Replay immutable historical excerpts through real current parsers and gates."""
    replay = _BENCHMARK["retained_replay"]
    problems, problem_warnings = parse_problem_record_list(json.dumps([replay["problem_record"]]))
    research, research_warnings = parse_research_dossier_list(
        json.dumps([replay["research_dossier"]]),
        legacy=True,
    )
    plans, plan_warnings = parse_change_plan_list(json.dumps([replay["change_plan"]]))

    assert len(problems) == len(research) == len(plans) == 1
    assert problem_warnings == []
    assert research_warnings == []
    assert any("missing_required_field" in warning for warning in plan_warnings)

    research_ready, research_reasons = assess_research_readiness(research[0])
    plan_errors = change_plan_quality_errors(
        plans[0],
        expected_revision="retained-revision-unavailable",
        problem_record=problems[0],
        research_dossier=research[0],
    )

    assert research_ready is replay["expected_current_disposition"]["research_ready"]
    assert "research_proof_invalid" in research_reasons
    assert bool(plan_errors) is not replay["expected_current_disposition"]["plan_ready"]
    assert any("discovery_first_step" in error for error in plan_errors)
    assert any("missing_change_targets" in error for error in plan_errors)
    assert any("repo_revision_mismatch" in error for error in plan_errors)


def test_verification_path_research_crosses_production_mining_without_remining(
    tmp_path: Path,
) -> None:
    case = _benchmark_case("verification-artifact-paths")
    atoms = normalize_atom_lineage(
        _source_atoms("verification-artifact-paths"),
        case_registry={
            "problem_id_to_case_id": {case["problem_id"]: case["case_id"]},
            "atom_id_to_case_id": {case["source_atom_id"]: case["case_id"]},
            "ticket_fingerprint_to_case_id": {},
        },
        strict_new_output=True,
    )

    by_id = {atom["atom_id"]: atom for atom in atoms}
    derived = by_id[case["derived_atom_id"]]
    assert derived["case_id"] == case["case_id"]
    assert derived["disposition"] == case["expected_disposition"]
    assert len(eligible_problem_mining_atoms(atoms)) == case["expected_new_cases"]
    stage = _run_dry_problem_mining(
        tmp_path,
        atoms=atoms,
        case_registry={
            "problem_id_to_case_id": {case["problem_id"]: case["case_id"]},
            "atom_id_to_case_id": {
                case["source_atom_id"]: case["case_id"],
                case["derived_atom_id"]: case["case_id"],
            },
            "ticket_fingerprint_to_case_id": {},
        },
    )
    assert stage["items"] == []
    assert stage["input_meta"]["eligible_problem_origin_atom_count"] == 0

    # Execute the exact agent-readable path through the production verification runner.
    workspace = tmp_path / "verification-path-workspace"
    artifact = workspace / "agent-readable" / "verification.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text('{"status":"passed"}\n', encoding="utf-8")
    probe = workspace / "verify_artifact.py"
    probe.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "payload = json.loads(Path('agent-readable/verification.json').read_text())\n"
        "assert payload['status'] == 'passed'\n"
        "print('agent_readable_path=ok')\n",
        encoding="utf-8",
    )
    summary = _run_verification_commands(
        run_dir=tmp_path / "verification-path-run",
        attempt_number=1,
        commands=["python verify_artifact.py"],
        command_prefix=[],
        cwd=workspace,
        timeout_seconds=None,
        python_executable=sys.executable,
        artifacts_dir_rel=Path("."),
    )
    assert summary["passed"] is True
    assert summary["commands"][0]["timed_out"] is False
    assert "agent_readable_path=ok" in summary["commands"][0]["stdout_tail"]


@pytest.mark.parametrize("benchmark_id", ["lifecycle-classification", "storage-exhaustion"])
def test_historical_similarity_does_not_collapse_cases_before_objective_identity(
    benchmark_id: str,
) -> None:
    benchmark = _benchmark_case(benchmark_id)
    records = []
    for raw in benchmark["records"]:
        record = dict(raw)
        record["canonical_problem_id"] = record["problem_id"]
        record["case_member_problem_ids"] = [record["problem_id"]]
        records.append(record)
    first, second = records
    evidence_ids = [*first["evidence_atom_ids"], *second["evidence_atom_ids"]]
    decisions = [
        {
            "focus_id": item["problem_id"],
            "action": benchmark["relation"],
            "group_id": f"cause:{benchmark_id}",
            "member_ids": [peer["problem_id"]],
            "rationale": "A reviewer proposed one cause; the runner still requires identity proof.",
            "review_confidence": 0.95,
            "evidence_atom_ids": evidence_ids,
        }
        for item, peer in ((first, second), (second, first))
    ]

    canonical = canonicalize_problem_cases(records, decisions, strict_review=True)

    assert len(canonical) == benchmark["expected_without_verified_relation_receipt"]
    assert all(
        any(
            error.startswith("collapse_objective_identity_missing:")
            for error in item["case_relation_actions"][0]["relation_validation_errors"]
        )
        for item in canonical
    )


def test_lifecycle_relation_applies_only_from_hash_verified_runner_receipt(
    tmp_path: Path,
) -> None:
    benchmark = _benchmark_case("lifecycle-classification")
    records = [
        {
            **raw,
            "canonical_problem_id": raw["problem_id"],
            "case_member_problem_ids": [raw["problem_id"]],
        }
        for raw in benchmark["records"]
    ]
    first, second = records
    response = tmp_path / "relation-review.response.json"
    response.write_text(
        json.dumps(
            {
                "decision": "same_cause_group",
                "source": second["case_id"],
                "target": first["case_id"],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _receipt, refs = write_case_relation_receipt(
        tmp_path / "relations.json",
        stage="repro_research",
        relation_review_response_path=response,
        relations=[
            {
                "source_case_id": second["case_id"],
                "target_case_id": first["case_id"],
                "direction": "source_to_canonical",
                "relation_kind": "canonical_absorption",
                "decision_actions": ["same_cause_group"],
            }
        ],
    )
    registry = {
        "cases": {
            first["case_id"]: {
                "case_id": first["case_id"],
                "incoming_relation_receipts": [refs[second["case_id"]]],
            },
            second["case_id"]: {
                "case_id": second["case_id"],
                "relation_receipt": refs[second["case_id"]],
            },
        }
    }
    edges = _verified_relation_edges_from_case_registry(registry)
    assert edges == {tuple(sorted((first["case_id"], second["case_id"])))}
    evidence_ids = [*first["evidence_atom_ids"], *second["evidence_atom_ids"]]
    decisions = [
        {
            "focus_id": item["problem_id"],
            "action": "same_cause_group",
            "group_id": "cause:lifecycle-classification",
            "member_ids": [peer["problem_id"]],
            "rationale": "The runner persisted the reviewed canonical absorption.",
            "review_confidence": 1.0,
            "evidence_atom_ids": evidence_ids,
        }
        for item, peer in ((first, second), (second, first))
    ]

    canonical = canonicalize_problem_cases(
        records,
        decisions,
        strict_review=True,
        verified_relation_edges=edges,
    )

    assert len(canonical) == benchmark["expected_with_verified_relation_receipt"]
    assert set(canonical[0]["case_member_problem_ids"]) == {
        first["problem_id"],
        second["problem_id"],
    }


@pytest.mark.parametrize("benchmark_id", ["lifecycle-classification", "windows-shell"])
def test_representative_observed_cases_cross_the_production_mining_boundary(
    tmp_path: Path,
    benchmark_id: str,
) -> None:
    atoms = _source_atoms(benchmark_id)

    stage = _run_dry_problem_mining(tmp_path, atoms=atoms)

    assert stage["input_meta"]["eligible_problem_origin_atom_count"] == len(atoms)
    cited = {atom_id for record in stage["items"] for atom_id in record["evidence_atom_ids"]}
    assert cited == {atom["atom_id"] for atom in atoms}
    assert all(record["_dry_run_synthesized"] is True for record in stage["items"])
    assert all(atom_is_idea_originated(atom) is False for atom in atoms)


def test_historical_apply_patch_excerpt_is_not_upgraded_into_research_proof() -> None:
    benchmark = _benchmark_case("apply-patch-context")
    ready, reasons = assess_research_readiness(
        {
            "problem_id": benchmark["problem_id"],
            "research_status": "insufficient_evidence",
            "reproduction_status": benchmark["historical_reproduction_status"],
            "material_unknowns": [
                {
                    "unknown": benchmark["historical_material_unknown"],
                    "affects": ["root_cause", "change_surface"],
                }
            ],
        }
    )

    assert ready is benchmark["expected_ready"]
    assert benchmark["historical_evidence_status"] == "retained_compiled_excerpt_only"
    assert "research_proof_invalid" in reasons


def test_shell_probe_failure_is_not_misreported_as_policy_block_or_resolution() -> None:
    benchmark = _benchmark_case("windows-shell")
    capability = _resolve_shell_capability(
        agent="claude",
        operating_system="Windows",
        backend="local",
        sandbox_mode="workspace-write",
        policy_status="allowed",
        policy_reason="The effective policy permits shell execution.",
        allowed_tools=["Bash"],
        probe_result={
            "kind": "agent_shell_payload",
            "ok": False,
            "exit_code": 1,
            "stderr_excerpt": "PowerShell process launch failed before payload execution.",
        },
    ).to_dict()

    assert capability["state"] == "blocked"
    assert capability["policy_status"] == "allowed"
    assert capability["reason_code"] == "shell_probe_failed"

    shell_outcome = _outcome(
        "case:windows-shell",
        benchmark["expected_current_outcome_state_without_provenance"],
        requires_live=benchmark["requires_live_verification"],
    )

    validated = validate_outcome_record(shell_outcome)
    assert validated["state"] == "unverified"
    assert (
        outcome_suppresses_new_case_discovery(validated)
        is benchmark["expected_suppresses_discovery"]
    )

    shell_outcome["state"] = "resolved"
    with pytest.raises(ValueError, match="outcome_record_evidence_required"):
        validate_outcome_record(shell_outcome)


def test_unverified_claude_stderr_recurrence_remains_open() -> None:
    benchmark = _benchmark_case("claude-empty-stderr")
    outcome = validate_outcome_record(
        _outcome(
            "case:claude-stderr",
            benchmark["outcome_state"],
            requires_live=benchmark["requires_live_verification"],
        )
    )

    assert (
        outcome_suppresses_new_case_discovery(outcome) is benchmark["expected_suppresses_discovery"]
    )


def test_python_toolchain_replanning_requires_explicit_revision() -> None:
    benchmark = _benchmark_case("python-toolchain")
    identities = {
        ticket_export_fingerprint(
            {
                "case_id": benchmark["case_id"],
                "plan_revision_id": revision,
            }
        )
        for revision in benchmark["plan_revisions"]
    }

    assert len(identities) == benchmark["expected_distinct_export_identities"]
