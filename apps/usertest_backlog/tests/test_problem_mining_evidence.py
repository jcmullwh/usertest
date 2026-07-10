from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from backlog_core.case_lineage import (
    apply_atom_dispositions,
    atom_disposition_receipt_errors,
    eligible_problem_mining_atoms,
)
from backlog_miner.origin_evidence import origin_attachment_requirements
from backlog_repo import write_case_relation_receipt

from usertest_backlog.workflows.problem_mining import (
    _atoms_for_problem_mining_prompt,
    _preserve_primary_after_coverage_review_failure,
    _problem_mining_job_batches,
    _reconcile_problem_mining_reviews,
    _relation_review_payload,
    _run_problem_mining_stage,
    _run_relation_review_batches,
    _verified_relation_edges_from_case_registry,
    _write_chunked_problem_mining_atoms_workspace,
)
from usertest_backlog.workflows.problem_mining_evidence import (
    apply_problem_mining_decision_partition,
    build_dry_run_miner_receipt,
    build_failed_miner_receipt,
    build_live_miner_receipt,
    build_problem_mining_evidence_draft,
    finalize_problem_mining_evidence_receipt,
    normalize_problem_mining_events,
    problem_mining_evidence_receipt_ref,
    verify_problem_mining_evidence_receipt,
)


def _atom(atom_id: str = "atom:one", *, role: str = "observation") -> dict[str, object]:
    return {
        "atom_id": atom_id,
        "run_id": "run:one",
        "run_rel": "run/one",
        "origin_run_id": "run:one",
        "origin_stage": "observation" if role == "observation" else "repro_research",
        "source": "command_failure",
        "severity_hint": "low",
        "text": "The command failed before the requested workflow could complete.",
        "derived_from_atom_ids": [],
        "evidence_role": role,
        "parent_case_id": None,
        "case_id": None,
        "supporting_case_ids": [],
        "disposition": "unresolved",
        "disposition_status": "pending",
        "disposition_receipt": None,
    }


def _problem(atom_id: str = "atom:one") -> dict[str, object]:
    return {
        "problem_id": "problem:one",
        "case_id": "case:one",
        "title": "Command aborts the workflow",
        "problem": "The observed command failure aborts the requested workflow.",
        "user_impact": "The workflow cannot complete.",
        "severity": "high",
        "confidence": 0.9,
        "evidence_atom_ids": [atom_id],
        "evidence_summary": "The full atom records the failed command.",
        "problem_status": "identified",
    }


def _write_full_read_event(
    path: Path,
    *,
    relative_path: str,
    file_path: Path,
    append: bool = False,
) -> None:
    content = file_path.read_text(encoding="utf-8")
    file_bytes = file_path.read_bytes()
    event = {
        "ts": "2026-07-10T00:00:00Z",
        "type": "read_file",
        "data": {
            "path": relative_path,
            "bytes": len(file_bytes),
            "read_source": "tool",
            "source_exit_code": 0,
            "content_observed": True,
            "whole_file_observed": True,
            "observed_content": content,
            "observed_content_sha256": sha256(content.encode("utf-8")).hexdigest(),
            "observed_bytes": len(content.encode("utf-8")),
            "observed_start_line": 1,
            "observed_end_line": max(1, content.count("\n")),
            "file_sha256": sha256(file_bytes).hexdigest(),
            "file_size_bytes": len(file_bytes),
        },
    }
    with path.open("a" if append else "w", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\n")


def _verified_stage1(tmp_path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    atom = _atom()
    draft = build_problem_mining_evidence_draft(
        atoms=[atom], eligible_atoms=[atom], mode="live"
    )
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    atom_file = manifest["atom_files"][0]
    normalized = tmp_path / "normalized_events.jsonl"
    _write_full_read_event(
        normalized,
        relative_path=str(atom_file["file"]),
        file_path=workspace / str(atom_file["file"]),
    )
    problem = _problem()
    response = json.dumps(
        {
            "problem_records": [problem],
            "atom_decisions": [
                {
                    "atom_id": "atom:one",
                    "disposition": "supports_case",
                    "problem_ids": ["problem:one"],
                    "rationale": "The full atom directly records the command failure.",
                }
            ],
        }
    )
    miner = build_live_miner_receipt(
        tag="problem_mining_001",
        template_name="problem_miner_default.md",
        assigned_atom_ids=["atom:one"],
        eligible_atom_ids=["atom:one"],
        records=[problem],
        decisions=json.loads(response)["atom_decisions"],
        response_text=response,
        normalized_events_path=normalized,
        workspace_dir=workspace,
        workspace_manifest=manifest,
    )
    draft["miners"] = [miner]
    partitioned = apply_problem_mining_decision_partition(
        atoms=[atom], canonical_records=[problem], draft=draft
    )
    final_atoms = apply_atom_dispositions(partitioned, [problem])
    receipt_path = tmp_path / "problem_mining_evidence.json"
    receipt = finalize_problem_mining_evidence_receipt(
        draft=draft,
        atoms=final_atoms,
        receipt_path=receipt_path,
    )
    stage1 = {
        "items": [problem],
        "input_meta": {
            "problem_mining_evidence_receipt": problem_mining_evidence_receipt_ref(
                receipt=receipt,
                receipt_path=receipt_path,
            )
        },
        "artifacts": {"problem_mining_evidence_receipt": str(receipt_path)},
    }
    return stage1, final_atoms


def test_live_receipt_binds_full_read_and_exact_final_partition(tmp_path: Path) -> None:
    stage1, atoms = _verified_stage1(tmp_path)

    assert verify_problem_mining_evidence_receipt(
        stage1=stage1,
        atoms=atoms,
        require_live=True,
    ) == []
    receipt = json.loads(
        Path(stage1["artifacts"]["problem_mining_evidence_receipt"]).read_text(
            encoding="utf-8"
        )
    )
    assert receipt["eligible_source_atom_ids"] == ["atom:one"]
    assert receipt["eligible_derived_atom_ids"] == []
    assert receipt["decision_partition"][0]["case_ids"] == ["case:one"]


def test_preview_only_citation_is_rejected(tmp_path: Path) -> None:
    atom = _atom()
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    normalized = tmp_path / "normalized_events.jsonl"
    normalized.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="preview_only_citation"):
        build_live_miner_receipt(
            tag="problem_mining_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:one"],
            eligible_atom_ids=["atom:one"],
            records=[_problem()],
            decisions=[
                {
                    "atom_id": "atom:one",
                    "disposition": "supports_case",
                    "problem_ids": ["problem:one"],
                    "rationale": "The preview looked relevant.",
                }
            ],
            response_text="{}",
            normalized_events_path=normalized,
            workspace_dir=workspace,
            workspace_manifest=manifest,
        )


def test_complete_chunk_read_attests_every_contained_atom(tmp_path: Path) -> None:
    atoms = [_atom(f"atom:{index:02d}") for index in range(20)]
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt(atoms),
        max_records_per_miner=20,
        assigned_atom_ids=[str(atom["atom_id"]) for atom in atoms],
    )
    assert manifest["chunk_count"] == 1
    chunk = manifest["chunks"][0]
    normalized = tmp_path / "normalized_events.jsonl"
    _write_full_read_event(
        normalized,
        relative_path=str(chunk["text_file"]),
        file_path=workspace / str(chunk["text_file"]),
    )
    problem = _problem("atom:00")
    decisions = [
        {
            "atom_id": str(atom["atom_id"]),
            "disposition": "supports_case" if index == 0 else "expected_noise",
            "problem_ids": ["problem:one"] if index == 0 else [],
            "rationale": (
                "The command failure is direct evidence."
                if index == 0
                else "This atom repeats non-actionable progress output."
            ),
        }
        for index, atom in enumerate(atoms)
    ]

    receipt = build_live_miner_receipt(
        tag="problem_mining_001",
        template_name="problem_miner_default.md",
        assigned_atom_ids=[str(atom["atom_id"]) for atom in atoms],
        eligible_atom_ids=[str(atom["atom_id"]) for atom in atoms],
        records=[problem],
        decisions=decisions,
        response_text="{}",
        normalized_events_path=normalized,
        workspace_dir=workspace,
        workspace_manifest=manifest,
    )

    assert len(receipt["read_attestations"]) == 20
    assert {row["evidence_file_kind"] for row in receipt["read_attestations"]} == {
        "chunk_markdown"
    }


@pytest.mark.parametrize("model_disposition", ["duplicate", "expected_noise"])
def test_model_only_permanent_disposition_is_coerced_to_reconsiderable(
    tmp_path: Path,
    model_disposition: str,
) -> None:
    atom = _atom()
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    atom_file = manifest["atom_files"][0]
    normalized = tmp_path / "normalized_events.jsonl"
    _write_full_read_event(
        normalized,
        relative_path=str(atom_file["file"]),
        file_path=workspace / str(atom_file["file"]),
    )

    receipt = build_live_miner_receipt(
        tag="problem_mining_001",
        template_name="problem_miner_default.md",
        assigned_atom_ids=["atom:one"],
        eligible_atom_ids=["atom:one"],
        records=[],
        decisions=[
            {
                "atom_id": "atom:one",
                "disposition": model_disposition,
                "problem_ids": [],
                "rationale": "The model considered this non-actionable.",
                "revisit_when": None,
            }
        ],
        response_text="{}",
        normalized_events_path=normalized,
        workspace_dir=workspace,
        workspace_manifest=manifest,
    )

    decision = receipt["atom_decisions"][0]
    assert decision["disposition"] == "deferred"
    assert decision["revisit_when"]
    assert "disposition_proof" not in decision


def test_runner_rule_can_prove_proposal_evidence_is_expected_noise(tmp_path: Path) -> None:
    atom = _atom()
    atom["source"] = "suggested_change"
    atom["evidence_class"] = "proposal"
    draft = build_problem_mining_evidence_draft(
        atoms=[atom], eligible_atoms=[atom], mode="live"
    )
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    atom_file = manifest["atom_files"][0]
    normalized = tmp_path / "normalized_events.jsonl"
    _write_full_read_event(
        normalized,
        relative_path=str(atom_file["file"]),
        file_path=workspace / str(atom_file["file"]),
    )
    miner = build_live_miner_receipt(
        tag="problem_mining_001",
        template_name="problem_miner_default.md",
        assigned_atom_ids=["atom:one"],
        eligible_atom_ids=["atom:one"],
        records=[],
        decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "expected_noise",
                "problem_ids": [],
                "rationale": "This is proposal evidence, not an observed failure.",
                "revisit_when": None,
            }
        ],
        response_text="{}",
        normalized_events_path=normalized,
        workspace_dir=workspace,
        workspace_manifest=manifest,
    )
    draft["miners"] = [miner]
    partitioned = apply_problem_mining_decision_partition(
        atoms=[atom], canonical_records=[], draft=draft
    )
    final_atom = apply_atom_dispositions(partitioned, [])[0]

    assert final_atom["disposition"] == "expected_noise"
    assert final_atom["disposition_proof"]["rule_id"] == "proposal_evidence_class_v1"
    assert atom_disposition_receipt_errors(final_atom, require_decided=True) == []
    assert eligible_problem_mining_atoms([final_atom]) == []


@pytest.mark.parametrize("tampered_field", ["whole_file_observed", "file_sha256"])
def test_partial_or_hash_mismatched_chunk_read_attests_nothing(
    tmp_path: Path,
    tampered_field: str,
) -> None:
    atom = _atom()
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    chunk = manifest["chunks"][0]
    normalized = tmp_path / "normalized_events.jsonl"
    _write_full_read_event(
        normalized,
        relative_path=str(chunk["text_file"]),
        file_path=workspace / str(chunk["text_file"]),
    )
    event = json.loads(normalized.read_text(encoding="utf-8"))
    event["data"][tampered_field] = (
        False if tampered_field == "whole_file_observed" else "0" * 64
    )
    normalized.write_text(json.dumps(event) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="assigned_atom_not_read_in_full"):
        build_live_miner_receipt(
            tag="problem_mining_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:one"],
            eligible_atom_ids=["atom:one"],
            records=[],
            decisions=[
                {
                    "atom_id": "atom:one",
                    "disposition": "unresolved",
                    "problem_ids": [],
                    "rationale": "The available text does not establish a problem.",
                }
            ],
            response_text="{}",
            normalized_events_path=normalized,
            workspace_dir=workspace,
            workspace_manifest=manifest,
        )


def test_codex_raw_chunk_read_normalizes_into_live_receipt(tmp_path: Path) -> None:
    atoms = [_atom("atom:one"), _atom("atom:two")]
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt(atoms),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one", "atom:two"],
    )
    chunk = manifest["chunks"][0]
    chunk_path = workspace / str(chunk["text_file"])
    raw_events = tmp_path / "raw_events.jsonl"
    raw_events.write_text(
        json.dumps(
            {
                "id": "chunk-read",
                "msg": {
                    "type": "exec_command_end",
                    "command": [
                        "Get-Content",
                        "-Raw",
                        "-LiteralPath",
                        str(chunk["text_file"]),
                    ],
                    "exit_code": 0,
                    "cwd": str(workspace),
                    "stdout": chunk_path.read_text(encoding="utf-8"),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    normalized = tmp_path / "normalized_events.jsonl"

    normalize_problem_mining_events(
        agent="codex",
        raw_events_path=raw_events,
        normalized_events_path=normalized,
        workspace_dir=workspace,
    )
    receipt = build_live_miner_receipt(
        tag="problem_mining_001",
        template_name="problem_miner_default.md",
        assigned_atom_ids=["atom:one", "atom:two"],
        eligible_atom_ids=["atom:one", "atom:two"],
        records=[],
        decisions=[
            {
                "atom_id": atom_id,
                "disposition": "expected_noise",
                "problem_ids": [],
                "rationale": "The complete atom is routine non-error progress output.",
                "revisit_when": None,
            }
            for atom_id in ("atom:one", "atom:two")
        ],
        response_text="{}",
        normalized_events_path=normalized,
        workspace_dir=workspace,
        workspace_manifest=manifest,
    )

    assert len(receipt["read_attestations"]) == 2
    assert {row["evidence_file_kind"] for row in receipt["read_attestations"]} == {
        "chunk_markdown"
    }


def test_large_corpus_jobs_are_bounded_and_partition_exactly(tmp_path: Path) -> None:
    atoms = [_atom(f"atom:{index:04d}") for index in range(1305)]
    for index, atom in enumerate(atoms):
        atom["text"] = f"Evidence {index}: " + ("detailed observed output " * 32)
    prompt_atoms = _atoms_for_problem_mining_prompt(atoms)

    batches = _problem_mining_job_batches(prompt_atoms)

    assigned_ids = [str(atom["atom_id"]) for batch in batches for atom in batch]
    assert sorted(assigned_ids) == sorted(str(atom["atom_id"]) for atom in atoms)
    assert len(assigned_ids) == len(set(assigned_ids))
    assert len(batches) < 50
    for index, batch in enumerate(batches):
        workspace = tmp_path / f"workspace-{index:02d}"
        manifest = _write_chunked_problem_mining_atoms_workspace(
            workspace_dir=workspace,
            prompt_atoms=batch,
            max_records_per_miner=20,
            assigned_atom_ids=[str(atom["atom_id"]) for atom in batch],
        )
        assert manifest["chunk_count"] <= 3
        assert manifest["total_atom_count"] <= 100
        assert manifest["total_chunk_bytes"] <= 150_000


def test_tiny_atom_corpus_respects_max_atoms_per_job() -> None:
    atoms = [_atom(f"atom:{index:04d}") for index in range(150)]
    for atom in atoms:
        atom["text"] = "x"

    batches = _problem_mining_job_batches(_atoms_for_problem_mining_prompt(atoms))

    assert [len(batch) for batch in batches] == [100, 50]
    assigned_ids = [str(atom["atom_id"]) for batch in batches for atom in batch]
    assert assigned_ids == [str(atom["atom_id"]) for atom in atoms]


def test_single_workspace_chunk_is_split_to_job_byte_limit(tmp_path: Path) -> None:
    atoms = [_atom(f"atom:{index:04d}") for index in range(24)]
    for index, atom in enumerate(atoms):
        atom["text"] = f"evidence-{index}-" + ("detail " * 24)
    prompt_atoms = _atoms_for_problem_mining_prompt(atoms)
    max_bytes = 8_000

    batches = _problem_mining_job_batches(
        prompt_atoms,
        chunk_max_bytes=55_000,
        max_chunks=3,
        max_atoms=100,
        max_bytes=max_bytes,
    )

    assert len(batches) > 1
    assigned_ids = [str(atom["atom_id"]) for batch in batches for atom in batch]
    assert assigned_ids == [str(atom["atom_id"]) for atom in prompt_atoms]
    for index, batch in enumerate(batches):
        manifest = _write_chunked_problem_mining_atoms_workspace(
            workspace_dir=tmp_path / f"byte-workspace-{index}",
            prompt_atoms=batch,
            max_records_per_miner=20,
            assigned_atom_ids=[str(atom["atom_id"]) for atom in batch],
        )
        assert manifest["total_chunk_bytes"] <= max_bytes
        assert manifest["total_text_chunk_bytes"] <= max_bytes


def test_problem_mining_projection_retains_unique_evidence_context(tmp_path: Path) -> None:
    atom = _atom()
    atom.update(
        {
            "run_dir": "runs/target/run",
            "impact": "The workflow cannot complete for Windows users.",
            "evidence_text": "The captured stderr names the missing executable.",
            "command": "python -m usertest",
            "exit_code": 1,
            "output_excerpt": "FileNotFoundError: executable was not found",
            "artifact_ref": {"path": "stderr.txt", "sha256": "a" * 64},
            "excerpt_head": str(atom["text"]),
            "attachments": [
                {
                    "path": "stderr.txt",
                    "excerpt_head": "duplicated large stderr",
                    "artifact_ref": {"path": "stderr.txt", "sha256": "a" * 64},
                }
            ],
        }
    )

    projection = _atoms_for_problem_mining_prompt([atom])[0]

    assert projection["impact"] == atom["impact"]
    assert projection["evidence_text"] == atom["evidence_text"]
    assert projection["output_excerpt"] == atom["output_excerpt"]
    assert projection["artifact_ref"] == atom["artifact_ref"]
    assert "excerpt_head" not in projection
    assert "excerpt_head" not in projection["attachments"][0]
    assert projection["attachments"][0]["artifact_ref"] == atom["artifact_ref"]

    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=[projection],
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    required_markdown = (
        workspace / str(manifest["chunks"][0]["text_file"])
    ).read_text(encoding="utf-8")
    assert "FileNotFoundError: executable was not found" in required_markdown
    assert "The workflow cannot complete for Windows users." in required_markdown
    assert '"artifact_ref"' in required_markdown
    assert manifest["chunks"][0]["text_bytes"] <= manifest["chunk_max_bytes"]


def test_problem_mining_reads_middle_of_large_materialized_attachment(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "target" / "one"
    run_dir.mkdir(parents=True)
    signature = "REAL_UNACTIONED_FAILURE_SIGNATURE_IN_MIDDLE"
    artifact = run_dir / "agent_stderr.txt"
    artifact.write_text(("ordinary-prefix\n" * 1_400) + signature + ("\nordinary-suffix" * 1_400))
    assert artifact.stat().st_size > 24 * 1024
    atom = _atom()
    atom.update(
        {
            "run_dir": str(run_dir),
            "text": "The retained diagnostic is larger than the atom excerpt.",
            "attachments": [
                {
                    "kind": "agent_stderr",
                    "artifact_ref": {
                        "path": artifact.name,
                        "sha256": sha256(artifact.read_bytes()).hexdigest(),
                        "size_bytes": artifact.stat().st_size,
                    },
                }
            ],
        }
    )
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
        source_root=tmp_path,
    )
    origin_manifest = manifest["origin_attachment_evidence"]
    requirements = origin_attachment_requirements(origin_manifest, atom_ids=["atom:one"])
    assert len(requirements) >= 2
    matching = [
        item
        for item in requirements
        if signature in (workspace / str(item["file"])).read_text(encoding="utf-8")
    ]
    assert matching
    assert all(int(item["size_bytes"]) < 24 * 1024 for item in requirements)

    normalized = tmp_path / "normalized_events.jsonl"
    atom_file = manifest["atom_files"][0]
    _write_full_read_event(
        normalized,
        relative_path=str(atom_file["file"]),
        file_path=workspace / str(atom_file["file"]),
    )
    for requirement in requirements:
        _write_full_read_event(
            normalized,
            relative_path=str(requirement["file"]),
            file_path=workspace / str(requirement["file"]),
            append=True,
        )
    problem = _problem()
    problem["problem"] = f"The retained diagnostic reports {signature}."
    response = json.dumps(
        {
            "problem_records": [problem],
            "atom_decisions": [
                {
                    "atom_id": "atom:one",
                    "disposition": "supports_case",
                    "problem_ids": ["problem:one"],
                    "rationale": f"The materialized attachment contains {signature}.",
                }
            ],
        }
    )

    receipt = build_live_miner_receipt(
        tag="problem_mining_001",
        template_name="problem_miner_default.md",
        assigned_atom_ids=["atom:one"],
        eligible_atom_ids=["atom:one"],
        records=[problem],
        decisions=json.loads(response)["atom_decisions"],
        response_text=response,
        normalized_events_path=normalized,
        workspace_dir=workspace,
        workspace_manifest=manifest,
    )

    assert len(receipt["origin_attachment_read_attestations"]) == len(requirements)
    assert any(
        item["file"] == matching[0]["file"]
        for item in receipt["origin_attachment_read_attestations"]
    )


def test_linked_atoms_remain_in_the_same_bounded_job() -> None:
    atoms = [_atom(f"atom:{index:03d}") for index in range(180)]
    for atom in atoms:
        atom["text"] = "observed output " * 80
    atoms[0]["linked_atom_ids"] = ["atom:179"]
    atoms[179]["linked_atom_ids"] = ["atom:000"]

    batches = _problem_mining_job_batches(_atoms_for_problem_mining_prompt(atoms))
    batch_by_atom = {
        str(atom["atom_id"]): batch_index
        for batch_index, batch in enumerate(batches)
        for atom in batch
    }

    assert batch_by_atom["atom:000"] == batch_by_atom["atom:179"]


def test_assignment_requires_one_decision_for_every_atom(tmp_path: Path) -> None:
    atom = _atom()
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    atom_file = manifest["atom_files"][0]
    normalized = tmp_path / "normalized_events.jsonl"
    _write_full_read_event(
        normalized,
        relative_path=str(atom_file["file"]),
        file_path=workspace / str(atom_file["file"]),
    )

    with pytest.raises(ValueError, match="decision_partition_mismatch"):
        build_live_miner_receipt(
            tag="problem_mining_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:one"],
            eligible_atom_ids=["atom:one"],
            records=[],
            decisions=[],
            response_text="{}",
            normalized_events_path=normalized,
            workspace_dir=workspace,
            workspace_manifest=manifest,
        )


def test_cited_atom_requires_matching_support_decision(tmp_path: Path) -> None:
    atom = _atom()
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    atom_file = manifest["atom_files"][0]
    normalized = tmp_path / "normalized_events.jsonl"
    _write_full_read_event(
        normalized,
        relative_path=str(atom_file["file"]),
        file_path=workspace / str(atom_file["file"]),
    )

    with pytest.raises(ValueError, match="citation_without_support_decision"):
        build_live_miner_receipt(
            tag="problem_mining_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:one"],
            eligible_atom_ids=["atom:one"],
            records=[_problem()],
            decisions=[
                {
                    "atom_id": "atom:one",
                    "disposition": "expected_noise",
                    "problem_ids": [],
                    "rationale": "The atom was incorrectly dismissed.",
                    "revisit_when": None,
                }
            ],
            response_text="{}",
            normalized_events_path=normalized,
            workspace_dir=workspace,
            workspace_manifest=manifest,
        )


def test_live_deferred_decision_requires_concrete_revisit_trigger(tmp_path: Path) -> None:
    atom = _atom()
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    atom_file = manifest["atom_files"][0]
    normalized = tmp_path / "normalized_events.jsonl"
    _write_full_read_event(
        normalized,
        relative_path=str(atom_file["file"]),
        file_path=workspace / str(atom_file["file"]),
    )

    with pytest.raises(ValueError, match="deferred_revisit_missing"):
        build_live_miner_receipt(
            tag="problem_mining_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:one"],
            eligible_atom_ids=["atom:one"],
            records=[],
            decisions=[
                {
                    "atom_id": "atom:one",
                    "disposition": "deferred",
                    "problem_ids": [],
                    "rationale": "A referenced runtime artifact is not available yet.",
                    "revisit_when": None,
                }
            ],
            response_text="{}",
            normalized_events_path=normalized,
            workspace_dir=workspace,
            workspace_manifest=manifest,
        )


def test_independent_review_can_recover_primary_missed_problem() -> None:
    records, decisions = _reconcile_problem_mining_reviews(
        primary_records=[],
        primary_decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "expected_noise",
                "problem_ids": [],
                "rationale": "The primary pass treated the output as routine.",
                "revisit_when": None,
            }
        ],
        review_records=[_problem()],
        review_decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "supports_case",
                "problem_ids": ["problem:one"],
                "rationale": "The second pass recognized the workflow-blocking failure.",
                "revisit_when": None,
            }
        ],
    )

    assert [record["problem_id"] for record in records] == ["problem:one"]
    assert decisions[0]["disposition"] == "supports_case"


def test_independent_review_must_confirm_primary_support_claim_verbatim() -> None:
    primary_problem = _problem()
    records, decisions = _reconcile_problem_mining_reviews(
        primary_records=[primary_problem],
        primary_decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "supports_case",
                "problem_ids": ["problem:one"],
                "rationale": "The primary pass found a workflow-blocking command failure.",
                "revisit_when": None,
            }
        ],
        review_records=[dict(primary_problem)],
        review_decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "supports_case",
                "problem_ids": ["problem:one"],
                "rationale": "The complete atom independently confirms the exact claim.",
                "revisit_when": None,
            }
        ],
    )

    assert [record["problem_id"] for record in records] == ["problem:one"]
    assert decisions[0]["disposition"] == "supports_case"
    assert decisions[0]["problem_ids"] == ["problem:one"]
    assert "independently confirmed" in decisions[0]["rationale"]


def test_unconfirmed_primary_support_claim_becomes_unresolved() -> None:
    records, decisions = _reconcile_problem_mining_reviews(
        primary_records=[_problem()],
        primary_decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "supports_case",
                "problem_ids": ["problem:one"],
                "rationale": "The primary pass inferred a problem from the atom.",
                "revisit_when": None,
            }
        ],
        review_records=[],
        review_decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "unresolved",
                "problem_ids": [],
                "rationale": "The evidence does not directly establish that claim.",
                "revisit_when": None,
            }
        ],
    )

    assert records == []
    assert len(decisions) == 1
    assert decisions[0]["atom_id"] == "atom:one"
    assert decisions[0]["disposition"] == "unresolved"
    assert decisions[0]["problem_ids"] == []
    assert decisions[0]["revisit_when"] is None
    assert "did not confirm" in decisions[0]["rationale"]


def test_coverage_review_failure_preserves_verified_primary_work() -> None:
    primary = {
        "tag": "problem_mining_001",
        "status": "verified",
        "assigned_atom_ids": ["atom:one"],
        "atom_decisions": [
            {
                "atom_id": "atom:one",
                "disposition": "supports_case",
                "problem_ids": ["problem:one"],
            }
        ],
    }
    failed_review = {
        "tag": "problem_mining_001_coverage_depth_review",
        "status": "failed_unresolved",
        "assigned_atom_ids": ["atom:one"],
    }

    preserved = _preserve_primary_after_coverage_review_failure(
        primary_receipt=primary,
        review_receipt=failed_review,
        review_failure="RuntimeError: reviewer unavailable",
    )

    assert preserved["status"] == "review_failed_primary_preserved"
    assert preserved["atom_decisions"] == primary["atom_decisions"]
    assert preserved["primary_pass"]["status"] == "verified"
    assert preserved["non_support_review"] == failed_review
    assert primary["status"] == "verified"


def test_all_support_job_still_runs_exactly_one_independent_full_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = tmp_path / "problem_miner_default.md"
    template.write_text(
        "{{STAGE_GUIDANCE}}\nEvidence: {{ATOMS_JSON}}\n",
        encoding="utf-8",
    )
    problem = _problem()
    problem.pop("case_id", None)
    decision = {
        "atom_id": "atom:one",
        "disposition": "supports_case",
        "problem_ids": ["problem:one"],
        "rationale": "The complete atom records a workflow-blocking command failure.",
        "revisit_when": None,
    }
    response = json.dumps(
        {
            "problem_records": [problem],
            "atom_decisions": [decision],
        }
    )
    prompts: list[str] = []

    def _fake_run_stage_prompt_json(**kwargs: object) -> str:
        prompts.append(str(kwargs["prompt"]))
        return response

    def _fake_build_live_miner_receipt(**kwargs: object) -> dict[str, object]:
        return {
            "tag": kwargs["tag"],
            "status": "verified",
            "assigned_atom_ids": list(kwargs["assigned_atom_ids"]),
            "atom_decisions": [dict(item) for item in kwargs["decisions"]],
            "read_attestations": [],
        }

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )
    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.normalize_problem_mining_events",
        lambda **_: [],
    )
    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.build_live_miner_receipt",
        _fake_build_live_miner_receipt,
    )

    stage_doc = _run_problem_mining_stage(
        repo_root=tmp_path,
        atoms=[_atom()],
        pipeline_manifest=type(
            "Manifest",
            (),
            {"problem_miner_templates": (template,)},
        )(),
        artifacts_dir=tmp_path / "artifacts",
        out_json=tmp_path / "problem_records.json",
        out_md=tmp_path / "problem_records.md",
        agent="codex",
        model=None,
        cfg=object(),
        dry_run=False,
        stage_guidance_text="Mine observed problems without proposing fixes.",
        case_registry={"cases": {}, "aliases": {}},
    )

    assert len(prompts) == 2
    assert "INDEPENDENT FULL COVERAGE AND DEPTH REVIEW" not in prompts[0]
    assert "INDEPENDENT FULL COVERAGE AND DEPTH REVIEW" in prompts[1]
    assert "including atoms that the primary pass attached to a problem" in prompts[1]
    miner_result = stage_doc["input_meta"]["miner_results"][0]
    assert miner_result["positive_review_atom_count"] == 1
    assert miner_result["non_support_review_atom_count"] == 0
    assert miner_result["coverage_depth_review_atom_count"] == 1
    receipt = stage_doc["input_meta"]["problem_mining_evidence_draft"]["miners"][0]
    assert receipt["review_scope"] == "all_assigned_atoms_positive_and_non_support"
    assert receipt["non_support_review"]["status"] == "verified"


def test_relation_payload_omits_unrelated_global_case_index_entries() -> None:
    relation_items = [
        {
            "problem_id": f"problem:{index}",
            "case_id": f"case:{index}",
            "title": f"Problem {index}",
            "evidence_atom_ids": [f"atom:{index}"],
        }
        for index in range(5)
    ]
    neighborhoods = [
        {
            "focus_id": "problem:0",
            "most_related_by_semantic": [{"index": 4, "score": 0.8}],
            "most_related_by_evidence_overlap": [],
            "most_related_by_metadata": [],
            "most_related_by_path_anchor": [],
        },
        {
            "focus_id": "problem:1",
            "most_related_by_semantic": [{"index": 2, "score": 0.7}],
            "most_related_by_evidence_overlap": [],
            "most_related_by_metadata": [],
            "most_related_by_path_anchor": [],
        },
    ]

    payload = _relation_review_payload(
        relation_items=relation_items,
        neighborhoods=neighborhoods,
        focus_problem_ids={"problem:0"},
    )

    assert payload["focus_count"] == 1
    assert payload["full_case_index_count"] == 5
    assert payload["case_index_count"] == 2
    assert {item["problem_id"] for item in payload["case_index"]} == {
        "problem:0",
        "problem:4",
    }


def test_failed_relation_batch_keeps_only_that_batch_provisionally_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relation_items = [
        {
            "problem_id": f"problem:{index}",
            "case_id": f"case:{index}",
            "title": f"Problem {index}",
            "evidence_atom_ids": [f"atom:{index}"],
        }
        for index in range(3)
    ]
    neighborhoods = [
        {
            "focus_id": f"problem:{index}",
            "most_related_by_semantic": [],
            "most_related_by_evidence_overlap": [],
            "most_related_by_metadata": [],
            "most_related_by_path_anchor": [],
        }
        for index in range(3)
    ]

    def _fake_run_stage_prompt_json(**kwargs: object) -> str:
        if str(kwargs["tag"]).endswith("batch_001"):
            raise RuntimeError("reviewer unavailable")
        return json.dumps(
            [
                {
                    "focus_id": "problem:2",
                    "action": "keep_separate",
                    "rationale": "No objective identity edge exists.",
                    "review_confidence": 0.9,
                }
            ]
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.problem_mining.run_stage_prompt_json",
        _fake_run_stage_prompt_json,
    )
    review_dir = tmp_path / "relation_review"
    review_dir.mkdir()
    decisions, batches = _run_relation_review_batches(
        relation_items=relation_items,
        neighborhoods=neighborhoods,
        focus_problem_ids=["problem:0", "problem:1", "problem:2"],
        template=(
            "{{STAGE_GUIDANCE}}\n{{ALLOWED_ACTIONS}}\n{{NEIGHBORHOODS_JSON}}"
        ),
        allowed_actions=["merge", "alias", "split", "same_cause_group", "keep_separate"],
        stage_guidance_text="Keep uncertain cases independent.",
        review_dir=review_dir,
        tag="problem_mining_relation_review_001",
        agent="codex",
        model=None,
        cfg=object(),
        max_foci=2,
    )

    assert len(decisions) == 3
    assert [batch["status"] for batch in batches] == [
        "failed_provisional_keep_separate",
        "completed",
    ]
    for decision in decisions[:2]:
        assert decision["action"] == "keep_separate"
        assert decision["provisional_relation_suggestion"]["kind"] == (
            "relation_review_batch_failure"
        )
    assert decisions[2]["review_confidence"] == 0.9
    checkpoint = json.loads(
        (review_dir / "problem_mining_relation_review_001.response.txt").read_text(
            encoding="utf-8"
        )
    )
    assert checkpoint == decisions


def test_verified_relation_edges_require_hash_bound_runner_receipt(tmp_path: Path) -> None:
    response_path = tmp_path / "relation.response.txt"
    response_path.write_text("[]\n", encoding="utf-8")
    _receipt, refs = write_case_relation_receipt(
        tmp_path / "relations.json",
        stage="problem_mining",
        relation_review_response_path=response_path,
        relations=[
            {
                "source_case_id": "case:source",
                "target_case_id": "case:target",
                "direction": "source_to_canonical",
                "relation_kind": "canonical_absorption",
                "decision_actions": ["alias"],
            }
        ],
    )
    registry = {
        "cases": {
            "case:source": {
                "case_id": "case:source",
                "relation_receipt": refs["case:source"],
            },
            "case:target": {
                "case_id": "case:target",
                "incoming_relation_receipts": [refs["case:source"]],
            },
        }
    }

    assert _verified_relation_edges_from_case_registry(registry) == {
        ("case:source", "case:target")
    }

    registry["cases"]["case:source"]["relation_receipt"] = {
        **refs["case:source"],
        "receipt_sha256": "0" * 64,
    }
    registry["cases"]["case:target"]["incoming_relation_receipts"] = []
    assert _verified_relation_edges_from_case_registry(registry) == set()


def test_disagreeing_non_support_reviews_remain_unresolved() -> None:
    records, decisions = _reconcile_problem_mining_reviews(
        primary_records=[],
        primary_decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "duplicate",
                "problem_ids": [],
                "rationale": "The primary pass believed another atom covered it.",
                "revisit_when": None,
            }
        ],
        review_records=[],
        review_decisions=[
            {
                "atom_id": "atom:one",
                "disposition": "expected_noise",
                "problem_ids": [],
                "rationale": "The second pass believed it was routine output.",
                "revisit_when": None,
            }
        ],
    )

    assert records == []
    assert decisions[0]["disposition"] == "unresolved"


def test_receipt_revalidation_detects_retained_read_tampering(tmp_path: Path) -> None:
    stage1, atoms = _verified_stage1(tmp_path)
    receipt_path = Path(stage1["artifacts"]["problem_mining_evidence_receipt"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    atom_file = Path(receipt["miners"][0]["workspace_dir"]) / receipt["miners"][0][
        "read_attestations"
    ][0]["atom_file"]
    atom_file.write_text("tampered\n", encoding="utf-8")

    errors = verify_problem_mining_evidence_receipt(
        stage1=stage1,
        atoms=atoms,
        require_live=True,
    )

    assert any(error.startswith("problem_mining_read_attestation_changed") for error in errors)


def test_derived_evidence_is_never_counted_as_source_coverage() -> None:
    source = _atom("atom:source")
    derived = _atom("atom:derived", role="research")

    draft = build_problem_mining_evidence_draft(
        atoms=[source, derived],
        eligible_atoms=[source, derived],
        mode="live",
    )

    assert draft["eligible_source_atom_ids"] == ["atom:source"]
    assert draft["eligible_derived_atom_ids"] == ["atom:derived"]


def test_dry_run_receipt_never_claims_full_reads_or_export_eligibility(tmp_path: Path) -> None:
    atom = _atom()
    draft = build_problem_mining_evidence_draft(
        atoms=[atom], eligible_atoms=[atom], mode="dry_run"
    )
    draft["miners"] = [
        build_dry_run_miner_receipt(
            tag="problem_mining_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:one"],
            records=[],
        )
    ]
    partitioned = apply_problem_mining_decision_partition(
        atoms=[atom], canonical_records=[], draft=draft
    )
    receipt_path = tmp_path / "dry.json"
    receipt = finalize_problem_mining_evidence_receipt(
        draft=draft,
        atoms=partitioned,
        receipt_path=receipt_path,
    )
    stage1 = {
        "items": [],
        "input_meta": {
            "problem_mining_evidence_receipt": problem_mining_evidence_receipt_ref(
                receipt=receipt,
                receipt_path=receipt_path,
            )
        },
        "artifacts": {"problem_mining_evidence_receipt": str(receipt_path)},
    }

    errors = verify_problem_mining_evidence_receipt(
        stage1=stage1,
        atoms=partitioned,
        require_live=True,
    )

    assert receipt["eligible_for_shadow_export"] is False
    assert receipt["miners"][0]["read_attestations"] == []
    assert "problem_mining_evidence_receipt_not_live_verified" in errors


def test_failed_mining_job_preserves_partition_but_keeps_shadow_closed(tmp_path: Path) -> None:
    atom = _atom()
    draft = build_problem_mining_evidence_draft(
        atoms=[atom], eligible_atoms=[atom], mode="live"
    )
    workspace = tmp_path / "failed_workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=_atoms_for_problem_mining_prompt([atom]),
        max_records_per_miner=20,
        assigned_atom_ids=["atom:one"],
    )
    draft["miners"] = [
        build_failed_miner_receipt(
            tag="problem_mining_001",
            template_name="problem_miner_default.md",
            assigned_atom_ids=["atom:one"],
            workspace_dir=workspace,
            workspace_manifest=manifest,
            error="ValueError: malformed model response",
        )
    ]
    partitioned = apply_problem_mining_decision_partition(
        atoms=[atom], canonical_records=[], draft=draft
    )
    receipt_path = tmp_path / "partial.json"
    receipt = finalize_problem_mining_evidence_receipt(
        draft=draft,
        atoms=partitioned,
        receipt_path=receipt_path,
    )
    stage1 = {
        "items": [],
        "input_meta": {
            "problem_mining_evidence_receipt": problem_mining_evidence_receipt_ref(
                receipt=receipt,
                receipt_path=receipt_path,
            )
        },
        "artifacts": {"problem_mining_evidence_receipt": str(receipt_path)},
    }

    errors = verify_problem_mining_evidence_receipt(
        stage1=stage1,
        atoms=partitioned,
        require_live=True,
    )

    assert partitioned[0]["disposition"] == "unresolved"
    assert partitioned[0]["disposition_status"] == "decided"
    assert receipt["status"] == "partial_failed_jobs"
    assert receipt["eligible_for_shadow_export"] is False
    assert any("problem_mining_miner_not_verified" in error for error in errors)
