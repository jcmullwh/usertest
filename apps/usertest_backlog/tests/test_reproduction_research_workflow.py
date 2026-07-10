from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from backlog_miner.research_evidence import (
    BlockedReplayExecutor,
    DockerReplayExecutor,
    PlatformRoutingReplayExecutor,
    TrustedHostReplayExecutor,
)
from runner_core import RunnerConfig

import usertest_backlog.workflows.reproduction_research as mod


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
    assert selected[0]["missing_evidence_atom_ids"] == ["missing:atom"]
    assert captured["replay_executor"] is replay_executor
    assert captured["replay_executor_metadata"] == replay_metadata


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
        selected_priority_decisions=[
            {"problem_id": "problem:one", "selected_for_research": True}
        ],
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
    (run_dir / "agent_stderr.txt").write_text(
        "full retained stderr\n", encoding="utf-8"
    )
    (run_dir / "agent_last_message.txt").write_text(
        "full retained last message\n", encoding="utf-8"
    )
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
    assert any(
        route["executor"] == "trusted_host"
        for route in route_receipt["routes"].values()
    )


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
