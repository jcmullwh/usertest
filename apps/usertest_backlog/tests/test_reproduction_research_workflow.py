from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from backlog_core import apply_atom_disposition_decision, build_operational_failure_candidates
from backlog_core.stage_contracts import research_evidence_role_partition
from backlog_miner.research_evidence import (
    BlockedReplayExecutor,
    DockerReplayExecutor,
    PlatformRoutingReplayExecutor,
    TrustedHostReplayExecutor,
)
from runner_core import RunnerConfig

import usertest_backlog.workflows.reproduction_research as mod


def test_provisional_same_cause_research_receives_every_member_source_atom(
    tmp_path: Path,
) -> None:
    canonical_atom = {
        "atom_id": "atom:canonical",
        "source": "command_failure",
        "text": "The historical Windows command failed before execution.",
    }
    member_atom = {
        "atom_id": "atom:member",
        "source": "command_failure",
        "text": "A newer Windows command failed before execution.",
    }
    record = {
        "case_id": "case:canonical",
        "problem_id": "problem:canonical",
        "case_identity_status": "provisional_same_cause",
        "case_identity_candidate_ids": ["case:member", "case:canonical"],
        "case_member_problem_ids": ["problem:member", "problem:canonical"],
        "evidence_atom_ids": ["atom:canonical"],
        "source_evidence_atom_ids": ["atom:canonical"],
        "provisional_same_cause_group": {
            "schema_version": 1,
            "status": "research_hypothesis",
            "group_id": "provisional:windows-command-failure",
            "member_case_ids": ["case:member", "case:canonical"],
            "member_problem_ids": ["problem:member", "problem:canonical"],
            "member_facets": [
                {
                    "case_id": "case:member",
                    "problem_id": "problem:member",
                    "evidence_atom_ids": ["atom:member"],
                    "source_evidence_atom_ids": ["atom:member"],
                },
                {
                    "case_id": "case:canonical",
                    "problem_id": "problem:canonical",
                    "evidence_atom_ids": ["atom:canonical"],
                    "source_evidence_atom_ids": ["atom:canonical"],
                },
            ],
        },
    }

    [selected] = mod._build_selected_research_payloads(
        repo_root=tmp_path,
        selected_priority_decisions=[{"problem_id": "problem:canonical"}],
        problem_records=[record],
        atoms=[canonical_atom, member_atom],
    )

    assert selected["expected_evidence_atom_ids"] == [
        "atom:canonical",
        "atom:member",
    ]
    assert selected["provisional_same_cause_member_evidence_atom_ids"] == [
        "atom:member",
        "atom:canonical",
    ]
    assert [atom["atom_id"] for atom in selected["evidence_atoms"]] == [
        "atom:canonical",
        "atom:member",
    ]
    assignment = selected["evidence_assignment"]
    assert assignment["status"] == "complete"
    assert assignment["errors"] == []
    assert assignment["provisional_same_cause_member_evidence_atom_ids"] == [
        "atom:member",
        "atom:canonical",
    ]
    assert [receipt["atom_id"] for receipt in assignment["atom_receipts"]] == [
        "atom:canonical",
        "atom:member",
    ]


def test_inconsistent_provisional_same_cause_evidence_blocks_assignment(
    tmp_path: Path,
) -> None:
    canonical_atom = {
        "atom_id": "atom:canonical",
        "source": "command_failure",
        "text": "The historical Windows command failed before execution.",
    }
    injected_atom = {
        "atom_id": "atom:injected",
        "source": "command_failure",
        "text": "This atom must not enter through an inconsistent facet packet.",
    }
    record = {
        "case_id": "case:canonical",
        "problem_id": "problem:canonical",
        "case_identity_status": "provisional_same_cause",
        "case_identity_candidate_ids": ["case:member", "case:canonical"],
        "case_member_problem_ids": ["problem:member", "problem:canonical"],
        "evidence_atom_ids": ["atom:canonical"],
        "source_evidence_atom_ids": ["atom:canonical"],
        "provisional_same_cause_group": {
            "schema_version": 1,
            "status": "research_hypothesis",
            "group_id": "provisional:windows-command-failure",
            "member_case_ids": ["case:member", "case:canonical"],
            # This contradicts the canonical record and the facet below.
            "member_problem_ids": ["problem:other", "problem:canonical"],
            "member_facets": [
                {
                    "case_id": "case:member",
                    "problem_id": "problem:member",
                    "evidence_atom_ids": ["atom:injected"],
                    "source_evidence_atom_ids": ["atom:injected"],
                },
                {
                    "case_id": "case:canonical",
                    "problem_id": "problem:canonical",
                    "evidence_atom_ids": ["atom:canonical"],
                    "source_evidence_atom_ids": ["atom:canonical"],
                },
            ],
        },
    }

    [selected] = mod._build_selected_research_payloads(
        repo_root=tmp_path,
        selected_priority_decisions=[{"problem_id": "problem:canonical"}],
        problem_records=[record],
        atoms=[canonical_atom, injected_atom],
    )

    assert selected["expected_evidence_atom_ids"] == ["atom:canonical"]
    assert selected["provisional_same_cause_member_evidence_atom_ids"] == []
    assert selected["evidence_assignment"]["status"] == "incomplete"
    assert "atom:injected" not in {
        receipt["atom_id"]
        for receipt in selected["evidence_assignment"]["atom_receipts"]
    }
    assert any(
        error.startswith("provisional_same_cause_group_invalid:")
        for error in selected["evidence_assignment"]["errors"]
    )


def test_research_orchestration_passes_original_evidence_atoms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 3 receives the complete atoms cited by its assigned problem."""
    captured: dict[str, Any] = {}

    def fake_run_repro_research_stage(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"stage": "repro_research", "items": [], "artifacts": {}}

    monkeypatch.setattr(mod, "run_repro_research_stage", fake_run_repro_research_stage)
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "agent_stderr.txt").write_text("failure\n", encoding="utf-8")
    atom = {
        "atom_id": "run-1:command_failure:1",
        "run_dir": "runs/run-1",
        "source": "command_failure",
        "text": "The command failed",
        "artifact_ref": {"path": "agent_stderr.txt"},
    }
    out_json = tmp_path / "compiled" / "target.research.json"
    replay_executor = BlockedReplayExecutor(reason="dry_run_test")
    replay_metadata = {"executor": "blocked", "reason": "dry_run_test"}

    mod._run_repro_research_stage(
        repo_root=tmp_path,
        repo_input="pip:example",
        repo_ref="HEAD",
        target_slug="target",
        selected_priority_decisions=[{"problem_id": "problem:one", "selected_for_research": True}],
        problem_records=[
            {
                "case_id": "case:one",
                "problem_id": "problem:one",
                "title": "Failure",
                "evidence_atom_ids": [atom["atom_id"], "missing:atom"],
            }
        ],
        atoms=[atom],
        artifacts_dir=tmp_path / "compiled" / "artifacts",
        out_json=out_json,
        out_md=out_json.with_suffix(".md"),
        agent="codex",
        model=None,
        cfg=RunnerConfig(repo_root=tmp_path, runs_dir=tmp_path / "runs", agents={}, policies={}),
        dry_run=True,
        replay_timeout_seconds=30.0,
        replay_executor=replay_executor,
        replay_executor_metadata=replay_metadata,
    )

    selected = captured["selected_problems"]
    assert len(selected) == 1
    assert selected[0]["evidence_atoms"] == [atom]
    assert selected[0]["expected_evidence_atom_ids"] == [
        atom["atom_id"],
        "missing:atom",
    ]
    assert selected[0]["case_evidence_atom_ids"] == []
    assert selected[0]["occurrence_evidence_atom_ids"] == [
        atom["atom_id"],
        "missing:atom",
    ]
    assert selected[0]["missing_evidence_atom_ids"] == ["missing:atom"]
    assert captured["replay_executor"] is replay_executor
    assert captured["replay_executor_metadata"] == replay_metadata


def test_operational_candidate_research_receives_underlying_occurrence_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_repro_research_stage(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"stage": "repro_research", "items": [], "artifacts": {}}

    monkeypatch.setattr(mod, "run_repro_research_stage", fake_run_repro_research_stage)
    run_id = "implementation/run/disk-full"
    raw_atom_id = f"{run_id}:run_failure_event:1"
    run_dir = tmp_path / "runs" / "source-disk-full"
    run_dir.mkdir(parents=True)
    (run_dir / "agent_stderr.txt").write_text("ENOSPC on source volume\n", encoding="utf-8")
    (run_dir / "agent_last_message.txt").write_text("run stopped\n", encoding="utf-8")
    raw_atom: dict[str, Any] = {
        "atom_id": raw_atom_id,
        "run_id": run_id,
        "run_rel": run_id,
        "run_dir": str(run_dir),
        "origin_run_id": run_id,
        "source": "run_failure_event",
        "text": "No space left on device while writing the implementation workspace.",
        "status": "error",
        "evidence_class": "observed",
        "evidence_role": "implementation",
        "origin_stage": "implementation",
        "parent_case_id": None,
        "case_id": None,
        "supporting_case_ids": [],
        "disposition": "unresolved",
        "disposition_status": "pending",
    }
    [candidate] = build_operational_failure_candidates(
        [
            {
                "run_rel": run_id,
                "status": "error",
                "agent_exit_code": 1,
                "target_ref": {
                    "mission_id": "implement_maintenance_backlog_ticket_v1",
                    "report_schema_path": "configs/report_schemas/troubleshoot_v1.schema.json",
                },
                "error": {
                    "type": "AgentExecFailed",
                    "subtype": "disk_full",
                    "exit_code": 1,
                },
                "metrics": {},
                "report_validation_errors": [],
                "terminal_artifact_reads": {},
            }
        ],
        [raw_atom],
    )
    candidate_id = str(candidate["atom_id"])
    out_json = tmp_path / "compiled" / "target.research.json"

    mod._run_repro_research_stage(
        repo_root=tmp_path,
        repo_input="pip:example",
        repo_ref="HEAD",
        target_slug="target",
        selected_priority_decisions=[
            {"problem_id": "problem:disk-full", "selected_for_research": True}
        ],
        problem_records=[
            {
                "case_id": "case:disk-full",
                "problem_id": "problem:disk-full",
                "title": "Disk full",
                "evidence_atom_ids": [candidate_id],
                "source_evidence_atom_ids": [candidate_id],
            }
        ],
        atoms=[candidate, raw_atom],
        artifacts_dir=tmp_path / "compiled" / "artifacts",
        out_json=out_json,
        out_md=out_json.with_suffix(".md"),
        agent="codex",
        model=None,
        cfg=RunnerConfig(repo_root=tmp_path, runs_dir=tmp_path / "runs", agents={}, policies={}),
        dry_run=True,
        replay_timeout_seconds=30.0,
        replay_executor=BlockedReplayExecutor(reason="dry_run_test"),
        replay_executor_metadata={"executor": "blocked"},
    )

    selected = captured["selected_problems"][0]
    assert selected["case_evidence_atom_ids"] == [candidate_id]
    assert selected["occurrence_evidence_atom_ids"] == [raw_atom_id]
    assert selected["expected_evidence_atom_ids"] == [candidate_id, raw_atom_id]
    assert [atom["atom_id"] for atom in selected["evidence_atoms"]] == [
        candidate_id,
        raw_atom_id,
    ]
    assignment = selected["evidence_assignment"]
    assert assignment["status"] == "complete"
    assert assignment["errors"] == []
    assert assignment["case_evidence_atom_ids"] == [candidate_id]
    assert assignment["occurrence_evidence_atom_ids"] == [raw_atom_id]
    receipts = {receipt["atom_id"]: receipt for receipt in assignment["atom_receipts"]}
    assert receipts[candidate_id]["origin_evidence_mode"] == "signed_snapshot"
    assert receipts[raw_atom_id]["origin_evidence_mode"] == "snapshot_and_artifacts"
    assert {Path(item["path"]).name for item in receipts[raw_atom_id]["artifact_receipts"]} >= {
        "agent_stderr.txt",
        "agent_last_message.txt",
    }
    retained_assignment = dict(assignment)
    retained_assignment.pop("case_evidence_atom_ids")
    retained_assignment.pop("occurrence_evidence_atom_ids")
    assert research_evidence_role_partition(retained_assignment) == (
        [candidate_id],
        [raw_atom_id],
        "recovered_operational_aggregate_v1",
    )


def test_split_child_research_receives_authenticated_original_occurrence_not_facet_prose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_repro_research_stage(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"stage": "repro_research", "items": [], "artifacts": {}}

    monkeypatch.setattr(mod, "run_repro_research_stage", fake_run_repro_research_stage)
    parent_case_id = "case:broad"
    parent_problem_id = "problem:broad"
    child_case_id = "case:checkout"
    child_problem_id = "problem:broad:facet:checkout"
    context_atom_id = "atom:facet-context:checkout"
    occurrence_id = "atom:checkout"
    other_occurrence_id = "atom:writer"
    occurrence = {
        "atom_id": occurrence_id,
        "run_id": "run:checkout",
        "run_rel": "run/checkout",
        "origin_run_id": "run:checkout",
        "origin_stage": "implementation",
        "evidence_role": "implementation",
        "evidence_class": "observed",
        "source": "run_failure_event",
        "status": "error",
        "action": "create implementation checkout",
        "text": "checkout failed before implementation could begin",
        "derived_from_atom_ids": [],
        "parent_case_id": None,
        "case_id": None,
        "supporting_case_ids": [],
        "disposition": "unresolved",
        "disposition_status": "pending",
        "disposition_receipt": None,
    }
    other_occurrence = {
        **occurrence,
        "atom_id": other_occurrence_id,
        "run_id": "run:writer",
        "run_rel": "run/writer",
        "origin_run_id": "run:writer",
        "action": "append provider state",
        "text": "provider state append failed",
    }
    context_atom = apply_atom_disposition_decision(
        {
            "atom_id": context_atom_id,
            "run_id": "post_research_relation:checkout",
            "run_rel": "post_research_relation/checkout",
            "origin_run_id": "post_research_relation:checkout",
            "origin_stage": "post_research_relation",
            "evidence_role": "research",
            "evidence_class": "proposal",
            "source": "post_research_facet_context",
            "status": "identified",
            "severity_hint": "high",
            "text": "Research separated checkout creation from provider state writes.",
            "derived_from_atom_ids": [occurrence_id],
            "parent_case_id": parent_case_id,
            "case_id": child_case_id,
            "supporting_case_ids": [child_case_id],
            "disposition": "novel_case",
            "disposition_status": "pending",
            "disposition_receipt": None,
            "novel_case_rationale": "Signed research established a distinct action boundary.",
            "post_research_split_facet_id": "facet:checkout",
            "occurrence_evidence_atom_ids": [occurrence_id],
            "authenticated_boundary": {"kind": "action"},
        },
        disposition="novel_case",
        source="post_research_split",
        rationale="Runner authenticated the exact split-child occurrence membership.",
    )
    receipt_without_hash = {
        "schema_version": 1,
        "producer": "usertest_backlog",
        "receipt_kind": "post_research_case_split",
        "stage": "repro_research",
        "parent_case_id": parent_case_id,
        "parent_problem_id": parent_problem_id,
        "assessment": {"disposition": "split"},
        "occurrence_evidence_atom_ids": [occurrence_id, other_occurrence_id],
        "facets": [
            {
                "facet_id": "facet:checkout",
                "child_case_id": child_case_id,
                "child_problem_id": child_problem_id,
                "facet_context_atom_id": context_atom_id,
                "occurrence_evidence_atom_ids": [occurrence_id],
            },
            {
                "facet_id": "facet:writer",
                "child_case_id": "case:writer",
                "child_problem_id": "problem:broad:facet:writer",
                "facet_context_atom_id": "atom:facet-context:writer",
                "occurrence_evidence_atom_ids": [other_occurrence_id],
            },
        ],
    }
    canonical = json.dumps(
        receipt_without_hash,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    content_sha = sha256(canonical).hexdigest()
    receipt = {**receipt_without_hash, "content_sha256": content_sha}
    receipt_path = tmp_path / "split-receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    receipt_ref = {
        "schema_version": 1,
        "receipt_kind": "post_research_case_split",
        "receipt_path": str(receipt_path),
        "receipt_sha256": sha256(receipt_path.read_bytes()).hexdigest(),
        "content_sha256": content_sha,
    }
    child_record = {
        "case_id": child_case_id,
        "problem_id": child_problem_id,
        "title": "Implementation checkout cannot be created",
        "problem": "The implementation runner cannot create its checkout.",
        "evidence_atom_ids": [context_atom_id],
        "source_evidence_atom_ids": [],
        "derived_evidence_atom_ids": [context_atom_id],
        "occurrence_evidence_atom_ids": [occurrence_id],
        "split_from_case_id": parent_case_id,
        "split_parent_problem_id": parent_problem_id,
        "post_research_split_receipt": receipt_ref,
    }
    out_json = tmp_path / "compiled" / "target.research.json"

    mod._run_repro_research_stage(
        repo_root=tmp_path,
        repo_input="pip:example",
        repo_ref="HEAD",
        target_slug="target",
        selected_priority_decisions=[
            {"problem_id": child_problem_id, "selected_for_research": True}
        ],
        problem_records=[child_record],
        atoms=[context_atom, occurrence, other_occurrence],
        artifacts_dir=tmp_path / "compiled" / "artifacts",
        out_json=out_json,
        out_md=out_json.with_suffix(".md"),
        agent="codex",
        model=None,
        cfg=RunnerConfig(repo_root=tmp_path, runs_dir=tmp_path / "runs", agents={}, policies={}),
        dry_run=True,
        replay_timeout_seconds=30.0,
        replay_executor=BlockedReplayExecutor(reason="dry_run_test"),
        replay_executor_metadata={"executor": "blocked"},
    )

    selected = captured["selected_problems"][0]
    assert selected["case_evidence_atom_ids"] == []
    assert selected["occurrence_evidence_atom_ids"] == [occurrence_id]
    assert selected["expected_evidence_atom_ids"] == [occurrence_id]
    assert selected["evidence_atoms"] == [occurrence]
    assert selected["derived_evidence_atom_ids"] == [context_atom_id]
    assert selected["derived_evidence_atoms"] == [context_atom]
    assert selected["evidence_lineage_errors"] == []
    assignment = selected["evidence_assignment"]
    assert assignment["status"] == "complete"
    assert assignment["case_evidence_atom_ids"] == []
    assert assignment["occurrence_evidence_atom_ids"] == [occurrence_id]


def test_open_case_reresearch_requires_source_evidence_not_derived_commentary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_repro_research_stage(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"stage": "repro_research", "items": [], "artifacts": {}}

    monkeypatch.setattr(mod, "run_repro_research_stage", fake_run_repro_research_stage)
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text('{"symptom":"real failure"}\n', encoding="utf-8")
    source_atom = {
        "atom_id": "atom:source",
        "run_dir": "runs/run-1",
        "source": "command_failure",
        "text": "real failure",
        "artifact_ref": {"path": "report.json"},
    }
    derived_atom = {
        "atom_id": "atom:prior-research",
        "evidence_role": "research",
        "parent_case_id": "case:one",
        "text": "prior researcher commentary",
    }
    out_json = tmp_path / "compiled" / "target.research.json"

    mod._run_repro_research_stage(
        repo_root=tmp_path,
        repo_input="pip:example",
        repo_ref="HEAD",
        target_slug="target",
        selected_priority_decisions=[{"problem_id": "problem:one", "selected_for_research": True}],
        problem_records=[
            {
                "case_id": "case:one",
                "problem_id": "problem:one",
                "evidence_atom_ids": ["atom:source", "atom:prior-research"],
                "source_evidence_atom_ids": ["atom:source"],
                "derived_evidence_atom_ids": ["atom:prior-research"],
            }
        ],
        atoms=[source_atom, derived_atom],
        artifacts_dir=tmp_path / "compiled" / "artifacts",
        out_json=out_json,
        out_md=out_json.with_suffix(".md"),
        agent="codex",
        model=None,
        cfg=RunnerConfig(
            repo_root=tmp_path,
            runs_dir=tmp_path / "runs",
            agents={},
            policies={},
        ),
        dry_run=True,
        replay_timeout_seconds=30.0,
        replay_executor=BlockedReplayExecutor(reason="dry_run_test"),
        replay_executor_metadata={"executor": "blocked"},
    )

    selected = captured["selected_problems"][0]
    assert selected["expected_evidence_atom_ids"] == ["atom:source"]
    assert selected["evidence_atoms"] == [source_atom]
    assert selected["derived_evidence_atom_ids"] == ["atom:prior-research"]
    assert selected["derived_evidence_atoms"] == [derived_atom]
    assert selected["missing_evidence_atom_ids"] == []


def test_evidence_assignment_snapshot_excludes_mining_decision_text(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text('{"error":"real failure"}\n', encoding="utf-8")
    atom = {
        "atom_id": "atom:source",
        "run_dir": "runs/run-1",
        "source": "command_failure",
        "text": "real failure",
        "artifact_ref": {"path": "report.json"},
        "case_id": "case:one",
        "disposition": "supports_case",
        "disposition_receipt": {
            "rationale": "invented symptom from prior mining",
        },
        "lineage_mining_blocker": "model-authored text",
    }

    assignment, missing = mod._evidence_assignment(
        case_id="case:one",
        problem_id="problem:one",
        evidence_atom_ids=["atom:source"],
        evidence_atoms=[atom],
        repo_root=tmp_path,
    )

    assert missing == []
    snapshot = assignment["atom_receipts"][0]["atom_snapshot"]
    assert snapshot["text"] == "real failure"
    assert "case_id" not in snapshot
    assert "disposition_receipt" not in snapshot
    assert "lineage_mining_blocker" not in snapshot


def test_signed_source_atom_snapshot_does_not_require_ancillary_artifact(
    tmp_path: Path,
) -> None:
    atom = {
        "atom_id": "atom:snapshot-only",
        "source": "command_failure",
        "command": "python -m tool verify",
        "exit_code": 3,
        "output_excerpt": "classifier selected the wrong recovery path",
        "text": "The verification command failed.",
    }

    assignment, missing = mod._evidence_assignment(
        case_id="case:one",
        problem_id="problem:one",
        evidence_atom_ids=["atom:snapshot-only"],
        evidence_atoms=[atom],
        repo_root=tmp_path,
    )

    assert missing == []
    receipt = assignment["atom_receipts"][0]
    assert receipt["origin_evidence_mode"] == "signed_snapshot"
    assert receipt["artifact_receipts"] == []
    assert receipt["atom_snapshot"]["output_excerpt"] == (
        "classifier selected the wrong recovery path"
    )


def test_origin_artifact_receipts_include_nested_and_retained_failure_streams(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "run-1"
    nested = run_dir / "command_failures" / "cmd-1" / "stderr.txt"
    nested.parent.mkdir(parents=True)
    nested.write_text("exact command failure\n", encoding="utf-8")
    (run_dir / "agent_stderr.txt").write_text("full retained stderr\n", encoding="utf-8")
    (run_dir / "agent_last_message.txt").write_text(
        "full retained last message\n", encoding="utf-8"
    )
    (run_dir / "preflight.json").write_text(
        '{"command_diagnostics":{"bash":{"usable":false}}}\n', encoding="utf-8"
    )
    (run_dir / "settings_ref.json").write_text(
        '{"settings":{"applied":{"exec_backend":"local"}}}\n', encoding="utf-8"
    )
    (run_dir / "prompt.txt").write_text("must not be retained\n", encoding="utf-8")
    atom = {
        "atom_id": "atom:failure",
        "run_dir": "runs/run-1",
        "source": "command_failure",
        "attachments": [
            {
                "path": "command_failures/cmd-1/stderr.txt",
                "artifact_ref": {
                    "path": "command_failures/cmd-1/stderr.txt",
                },
            }
        ],
    }

    receipts = mod._origin_artifact_receipts(atom, repo_root=tmp_path)

    paths = {Path(str(receipt["path"])).relative_to(run_dir).as_posix() for receipt in receipts}
    assert "command_failures/cmd-1/stderr.txt" in paths
    assert "agent_stderr.txt" in paths
    assert "agent_last_message.txt" in paths
    by_name = {Path(str(receipt["path"])).name: receipt for receipt in receipts}
    assert by_name["preflight.json"]["research_context_role"] == "preflight"
    assert by_name["settings_ref.json"]["research_context_role"] == "settings"
    assert "prompt.txt" not in by_name
    assert all(receipt["sha256"] for receipt in receipts)


def test_replay_executor_config_defaults_to_fail_closed(tmp_path: Path) -> None:
    executor, metadata = mod._configured_replay_executor(
        research_config={},
        repo_root=tmp_path,
        repo_input=None,
    )

    assert isinstance(executor, BlockedReplayExecutor)
    assert metadata == {
        "executor": "blocked",
        "reason": "backlog_research.replay_executor_missing",
    }


def test_replay_executor_config_builds_networkless_docker_executor(
    tmp_path: Path,
) -> None:
    executor, metadata = mod._configured_replay_executor(
        research_config={
            "replay_executor": "docker",
            "replay_docker_image": "example.invalid/replay@sha256:" + "a" * 64,
        },
        repo_root=tmp_path,
        repo_input="https://example.invalid/untrusted.git",
    )

    assert isinstance(executor, DockerReplayExecutor)
    assert executor.image_ref.endswith("a" * 64)
    assert metadata["executor"] == "docker"
    assert metadata["network"] == "none"
    assert metadata["host_environment"] == "not_forwarded"


def test_platform_router_keeps_docker_default_and_routes_host_platform(
    tmp_path: Path,
) -> None:
    approved = tmp_path / "approved"
    source = approved / "repo"
    source.mkdir(parents=True)
    executor, metadata = mod._configured_replay_executor(
        research_config={
            "replay_executor": "platform_router",
            "replay_docker_image": "example.invalid/replay@sha256:" + "a" * 64,
            "replay_trusted_host_roots": [str(approved)],
        },
        repo_root=tmp_path,
        repo_input=str(source),
    )

    assert isinstance(executor, PlatformRoutingReplayExecutor)
    assert isinstance(executor.default_executor, DockerReplayExecutor)
    assert metadata["executor"] == "platform_router"
    route_receipt = executor.isolation_receipt(source_workspace=source)
    assert route_receipt["trust_decision"] == "explicit_routes"
    assert any(route["executor"] == "trusted_host" for route in route_receipt["routes"].values())


@pytest.mark.parametrize(
    ("research_config", "message"),
    [
        ({"replay_executor": "docker"}, "replay_docker_image is required"),
        (
            {
                "replay_executor": "docker",
                "replay_docker_image": "image:tag\nforged",
            },
            "must be one Docker image reference",
        ),
        (
            {
                "replay_executor": "docker",
                "replay_docker_image": "image:tag",
                "replay_trusted_host_roots": ["."],
            },
            "only valid for replay_executor=trusted_host",
        ),
        ({"replay_executor": "anything"}, "must be one of"),
    ],
)
def test_replay_executor_config_rejects_invalid_docker_configuration(
    tmp_path: Path,
    research_config: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        mod._configured_replay_executor(
            research_config=research_config,
            repo_root=tmp_path,
            repo_input=None,
        )


def test_trusted_host_replay_binds_approval_to_local_source_identity(
    tmp_path: Path,
) -> None:
    approved = tmp_path / "approved"
    source = approved / "repo"
    source.mkdir(parents=True)

    executor, metadata = mod._configured_replay_executor(
        research_config={
            "replay_executor": "trusted_host",
            "replay_trusted_host_roots": [str(approved)],
        },
        repo_root=tmp_path,
        repo_input=str(source),
    )

    assert isinstance(executor, TrustedHostReplayExecutor)
    assert executor.source_identity == source.resolve()
    assert metadata["source_identity"] == str(source.resolve())


def test_trusted_host_replay_rejects_remote_or_unapproved_source(
    tmp_path: Path,
) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    config = {
        "replay_executor": "trusted_host",
        "replay_trusted_host_roots": [str(approved)],
    }

    with pytest.raises(ValueError, match="existing local repository"):
        mod._configured_replay_executor(
            research_config=config,
            repo_root=tmp_path,
            repo_input="https://example.invalid/untrusted.git",
        )
    with pytest.raises(ValueError, match="outside replay_trusted_host_roots"):
        mod._configured_replay_executor(
            research_config=config,
            repo_root=tmp_path,
            repo_input=str(outside),
        )
