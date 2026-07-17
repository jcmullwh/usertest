from __future__ import annotations

import json
import zipfile
from hashlib import sha256
from pathlib import Path

from backlog_core.case_lineage import source_evidence_atom_projection
from backlog_core.stage_contracts import evidence_assignment_sha256

import backlog_miner.research_runner as research_runner
from backlog_miner.origin_evidence import (
    materialize_origin_attachments,
    origin_attachment_read_scope,
    origin_attachment_requirements,
    verify_materialized_origin_attachments,
)


def test_stage3_read_scope_requires_indexes_and_only_declared_source_chunks() -> None:
    manifest = {
        "atom_refs": [
            {"atom_id": "atom:one", "artifact_sha256": "a" * 64},
        ],
        "artifacts": [
            {
                "artifact_sha256": "a" * 64,
                "representation": "utf-8",
                "chunks": [
                    {
                        "file": ".usertest_research/origin_evidence/artifacts/one/chunk_0001.txt",
                        "sha256": "b" * 64,
                        "size_bytes": 10,
                        "content_role": "full_utf8_text",
                    },
                    {
                        "file": ".usertest_research/origin_evidence/artifacts/one/chunk_0002.txt",
                        "sha256": "c" * 64,
                        "size_bytes": 10,
                        "content_role": "full_utf8_text",
                    },
                ],
            }
        ],
        "run_context": {
            "index_file": ".usertest_research/origin_evidence/run_context/index.json",
            "index_file_sha256": "d" * 64,
            "index_file_size_bytes": 20,
            "runs": [{"atom_ids": ["atom:one"]}],
        },
        "assigned_evidence": {
            "index_file": ".usertest_research/origin_evidence/assigned/index.json",
            "index_file_sha256": "e" * 64,
            "index_file_size_bytes": 30,
        },
    }
    chunk = manifest["artifacts"][0]["chunks"][0]["file"]
    context = manifest["run_context"]["index_file"]
    assigned = manifest["assigned_evidence"]["index_file"]

    index_only = origin_attachment_read_scope(
        manifest,
        dossier={"artifact_refs": []},
        observed_files=[context, assigned],
    )
    assert index_only["required_files"] == sorted([context, assigned])
    assert index_only["coverage_status"] == (
        "required_reads_complete_with_unread_optional_evidence"
    )
    assert index_only["unread_optional_file_count"] == 2
    assert index_only["selection_errors"] == []

    claim_bound = origin_attachment_read_scope(
        manifest,
        dossier={
            "artifact_refs": [
                {"artifact_id": "origin:one", "kind": "origin", "path": chunk}
            ]
        },
        observed_files=[context, assigned],
    )
    assert chunk in claim_bound["claim_bound_files"]
    assert chunk in claim_bound["missing_required_files"]


def test_stage3_read_scope_does_not_expand_snapshot_binding_to_all_artifact_chunks() -> None:
    chunk = ".usertest_research/origin_evidence/artifacts/one/chunk_0001.txt"
    manifest = {
        "atom_refs": [{"atom_id": "atom:one", "artifact_sha256": "a" * 64}],
        "artifacts": [
            {
                "artifact_sha256": "a" * 64,
                "chunks": [
                    {
                        "file": chunk,
                        "sha256": "b" * 64,
                        "size_bytes": 10,
                        "content_role": "full_utf8_text",
                    }
                ],
            }
        ],
    }
    snapshot_scope = origin_attachment_read_scope(
        manifest,
        dossier={"artifact_refs": []},
        verification={
            "atom_bindings": [
                {
                    "atom_id": "atom:one",
                    "experiment_id": "experiment:one",
                    "match_kind": "command_and_atom_evidence_symptom",
                    "origin_atom_field_path": "/symptom/error_code",
                    "origin_artifact_sha256": "a" * 64,
                }
            ]
        },
    )
    assert snapshot_scope["required_files"] == []
    assert snapshot_scope["selection_errors"] == []

    artifact_only_scope = origin_attachment_read_scope(
        manifest,
        dossier={"artifact_refs": []},
        verification={
            "atom_bindings": [
                {
                    "atom_id": "atom:one",
                    "experiment_id": "experiment:one",
                    "match_kind": "command_and_artifact_symptom_text",
                    "origin_artifact_sha256": "a" * 64,
                }
            ]
        },
    )
    assert artifact_only_scope["selection_errors"] == [
        "origin_attachment_artifact_only_binding_missing_declared_chunk:"
        "atom:one:experiment:one"
    ]


def test_stage3_index_only_read_attests_coverage_without_reading_optional_chunk(
    tmp_path: Path,
) -> None:
    origin_run = tmp_path / "origin-run"
    origin_run.mkdir()
    preflight = origin_run / "preflight.json"
    preflight.write_text('{"status":"failed"}\n', encoding="utf-8")
    stderr = origin_run / "agent_stderr.txt"
    stderr.write_text("retained optional diagnostic\n", encoding="utf-8")
    atom = {
        "atom_id": "atom:one",
        "run_dir": str(origin_run),
        "attachments": [
            {
                "kind": "agent_stderr",
                "artifact_ref": {
                    "path": stderr.name,
                    "sha256": sha256(stderr.read_bytes()).hexdigest(),
                    "size_bytes": stderr.stat().st_size,
                },
            }
        ],
    }
    assignment = {
        "atom_receipts": [
            {
                "atom_id": "atom:one",
                "atom_sha256": sha256(
                    json.dumps(atom, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "atom_snapshot": atom,
                "artifact_receipts": [
                    {
                        "path": str(preflight),
                        "sha256": sha256(preflight.read_bytes()).hexdigest(),
                        "size_bytes": preflight.stat().st_size,
                        "source_relpath": preflight.name,
                        "research_context_role": "preflight",
                    }
                ],
            }
        ]
    }
    workspace = tmp_path / "workspace"
    manifest = materialize_origin_attachments(
        atoms=[atom],
        workspace_dir=workspace,
        source_root=tmp_path,
        evidence_assignment=assignment,
    )
    assert manifest["errors"] == []
    requirements = origin_attachment_requirements(manifest)
    mandatory = [
        requirement
        for requirement in requirements
        if requirement["content_role"]
        in {"assigned_evidence_index", "source_run_context_index"}
    ]
    optional = [requirement for requirement in requirements if requirement not in mandatory]
    assert len(mandatory) == 2
    assert len(optional) == 1

    events: list[dict[str, object]] = []
    for requirement in mandatory:
        path = workspace / str(requirement["file"])
        events.append(
            {
                "type": "read_file",
                "data": {
                    "path": requirement["file"],
                    "content_observed": True,
                    "whole_file_observed": True,
                    "source_exit_code": 0,
                    "file_sha256": sha256(path.read_bytes()).hexdigest(),
                    "file_size_bytes": path.stat().st_size,
                    "observed_content_sha256": sha256(path.read_bytes()).hexdigest(),
                    "observed_bytes": path.stat().st_size,
                },
            }
        )
    research_run = tmp_path / "research-run"
    research_run.mkdir()
    (research_run / "normalized_events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    reads, scope, errors = research_runner._origin_attachment_read_evidence(
        run_dir=research_run,
        workspace_dir=workspace,
        manifest=manifest,
        dossier={"artifact_refs": []},
        verification={"atom_bindings": []},
    )

    assert errors == []
    assert {read["file"] for read in reads} == {
        requirement["file"] for requirement in mandatory
    }
    assert scope["coverage_status"] == (
        "required_reads_complete_with_unread_optional_evidence"
    )
    assert scope["unread_optional_file_count"] == 1

    declared_chunk = str(optional[0]["file"])
    declared_dossier = {
        "artifact_refs": [
            {
                "artifact_id": "origin:diagnostic",
                "kind": "origin_evidence",
                "path": declared_chunk,
            }
        ]
    }
    _, missing_scope, missing_errors = research_runner._origin_attachment_read_evidence(
        run_dir=research_run,
        workspace_dir=workspace,
        manifest=manifest,
        dossier=declared_dossier,
        verification={"atom_bindings": []},
    )
    assert declared_chunk in missing_scope["missing_required_files"]
    assert missing_errors == [
        f"origin_attachment_chunk_not_read_in_full:{declared_chunk}"
    ]

    chunk_path = workspace / declared_chunk
    events.append(
        {
            "type": "read_file",
            "data": {
                "path": declared_chunk,
                "content_observed": True,
                "whole_file_observed": True,
                "source_exit_code": 0,
                "file_sha256": sha256(chunk_path.read_bytes()).hexdigest(),
                "file_size_bytes": chunk_path.stat().st_size,
                "observed_content_sha256": sha256(chunk_path.read_bytes()).hexdigest(),
                "observed_bytes": chunk_path.stat().st_size,
            },
        }
    )
    (research_run / "normalized_events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    _, complete_scope, complete_errors = research_runner._origin_attachment_read_evidence(
        run_dir=research_run,
        workspace_dir=workspace,
        manifest=manifest,
        dossier=declared_dossier,
        verification={"atom_bindings": []},
    )
    assert complete_errors == []
    assert complete_scope["missing_required_files"] == []
    assert declared_chunk in complete_scope["observed_files"]


def _atom(*, run_dir: Path, artifact: Path, digest: str) -> dict[str, object]:
    return {
        "atom_id": "atom:large-attachment",
        "run_dir": str(run_dir),
        "attachments": [
            {
                "kind": "agent_stderr",
                "artifact_ref": {
                    "path": artifact.name,
                    "sha256": digest,
                    "size_bytes": artifact.stat().st_size,
                },
            }
        ],
    }


def test_materializes_middle_signature_from_large_attachment_in_bounded_chunks(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "one"
    run_dir.mkdir(parents=True)
    signature = "ROOT_CAUSE_SIGNATURE_ONLY_IN_ARTIFACT_MIDDLE"
    artifact = run_dir / "agent_stderr.txt"
    artifact.write_text(("prefix-data\n" * 1_600) + signature + ("\nsuffix-data" * 1_600))
    assert artifact.stat().st_size > 24 * 1024
    digest = sha256(artifact.read_bytes()).hexdigest()
    workspace = tmp_path / "workspace"

    manifest = materialize_origin_attachments(
        atoms=[_atom(run_dir=run_dir, artifact=artifact, digest=digest)],
        workspace_dir=workspace,
        source_root=tmp_path,
    )

    assert manifest["errors"] == []
    assert manifest["artifacts"][0]["artifact_sha256"] == digest
    requirements = origin_attachment_requirements(
        manifest,
        atom_ids=["atom:large-attachment"],
    )
    assert len(requirements) >= 2
    assert all(int(item["size_bytes"]) < 24 * 1024 for item in requirements)
    visible = "\n".join(
        (workspace / str(item["file"])).read_text(encoding="utf-8")
        for item in requirements
    )
    assert signature in visible
    assert verify_materialized_origin_attachments(
        workspace_dir=workspace,
        manifest=manifest,
    ) == []


def test_large_multi_atom_prompt_uses_hash_bound_index_without_dropping_atoms(
    tmp_path: Path,
) -> None:
    expected_atoms: list[dict[str, object]] = []
    derived_atoms: list[dict[str, object]] = []
    receipts: list[dict[str, object]] = []
    sentinels: dict[str, str] = {}
    for index in range(12):
        atom_id = f"atom:large:{index:02d}"
        sentinel = f"FULL_BODY_SENTINEL_{index:02d}_ONLY_ON_DISK"
        text = f"Repeated failure {index}\n" + (f"body-{index}-" * 5_000) + sentinel
        atom: dict[str, object] = {
            "atom_id": atom_id,
            "text": text,
            "error": {
                "type": "SyntheticFailure",
                "code": f"failure_{index:02d}",
                "message": f"Repeated failure {index}",
            },
            "evidence_role": "observation",
            "origin_stage": "runtime",
            "parent_case_id": "case:large",
            "derived_from_atom_ids": [f"atom:parent:{index:02d}"],
        }
        atom_snapshot = source_evidence_atom_projection(atom)
        atom_sha = sha256(
            json.dumps(
                atom_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        receipts.append(
            {
                "atom_id": atom_id,
                "atom_sha256": atom_sha,
                "atom_snapshot": atom_snapshot,
                "source_projection_version": 1,
                "artifact_receipts": [],
                "origin_evidence_mode": "signed_snapshot",
            }
        )
        sentinels[atom_id] = sentinel
        (derived_atoms if index >= 10 else expected_atoms).append(atom)

    all_atoms = [*expected_atoms, *derived_atoms]
    assignment: dict[str, object] = {
        "status": "complete",
        "errors": [],
        "case_id": "case:large",
        "problem_id": "problem:large",
        "expected_atom_ids": [str(atom["atom_id"]) for atom in all_atoms],
        "case_evidence_atom_ids": ["atom:large:00"],
        "occurrence_evidence_atom_ids": [
            str(atom["atom_id"]) for atom in all_atoms[1:]
        ],
        "atom_receipts": receipts,
    }
    assignment["assignment_sha256"] = evidence_assignment_sha256(assignment)
    raw_problem = {
        "case_id": "case:large",
        "problem_id": "problem:large",
        "problem_record": {"title": "Large repeated failure"},
        "priority_decision": {"priority_bucket": "high"},
        "expected_evidence_atom_ids": assignment["expected_atom_ids"],
        "evidence_atoms": expected_atoms,
        "derived_evidence_atom_ids": [str(atom["atom_id"]) for atom in derived_atoms],
        "derived_evidence_atoms": derived_atoms,
        "evidence_assignment": assignment,
    }
    raw_payload_bytes = len(
        json.dumps(raw_problem, ensure_ascii=False, indent=2).encode("utf-8")
    )
    workspace = tmp_path / "workspace"
    manifest = materialize_origin_attachments(
        atoms=all_atoms,
        workspace_dir=workspace,
        source_root=tmp_path,
        evidence_assignment=assignment,
    )
    assignment["origin_attachment_evidence"] = manifest
    assignment["assignment_sha256"] = evidence_assignment_sha256(assignment)
    raw_problem["origin_attachment_evidence"] = manifest

    prompt = research_runner._append_prompt_for_problem(
        repo_root=Path(__file__).resolve().parents[3],
        problem_payload=raw_problem,
    )
    marker = "## Assigned problem payload (JSON)\n"
    prompt_payload, _ = json.JSONDecoder().raw_decode(prompt[prompt.index(marker) + len(marker) :])
    prompt_bytes = len(prompt.encode("utf-8"))

    expected_ids = set(assignment["expected_atom_ids"])
    compact_ids = {
        str(atom["atom_id"])
        for atom in [
            *prompt_payload["evidence_atoms"],
            *prompt_payload["derived_evidence_atoms"],
        ]
    }
    assigned = manifest["assigned_evidence"]
    index_ids = {str(atom["atom_id"]) for atom in assigned["atoms"]}
    assert compact_ids == expected_ids
    assert index_ids == expected_ids
    assert prompt_payload["derived_evidence_atom_ids"] == [
        "atom:large:10",
        "atom:large:11",
    ]
    assert prompt_bytes < raw_payload_bytes // 4
    assert "atom_snapshot" not in prompt_payload["evidence_assignment"]
    assert "atom_receipts" not in prompt_payload["evidence_assignment"]
    assert prompt_payload["evidence_assignment"]["atom_receipt_count"] == 12
    compact_assigned = prompt_payload["evidence_assignment"][
        "origin_attachment_evidence"
    ]["assigned_evidence"]
    assert compact_assigned["index_file"] == assigned["index_file"]
    assert compact_assigned["index_file_sha256"] == assigned["index_file_sha256"]
    assert compact_assigned["assignment_file"] == assigned["assignment_file"]
    assert all(sentinel not in prompt for sentinel in sentinels.values())
    for entry in assigned["atoms"]:
        atom_path = workspace / str(entry["atom_file"])
        receipt_path = workspace / str(entry["receipt_file"])
        atom_text = atom_path.read_text(encoding="utf-8")
        assert sentinels[str(entry["atom_id"])] in atom_text
        assert sha256(atom_path.read_bytes()).hexdigest() == entry["atom_file_sha256"]
        assert sha256(receipt_path.read_bytes()).hexdigest() == entry["receipt_file_sha256"]
    assignment_path = workspace / str(assigned["assignment_file"])
    assert sha256(assignment_path.read_bytes()).hexdigest() == assigned[
        "assignment_file_sha256"
    ]
    assert verify_materialized_origin_attachments(
        workspace_dir=workspace,
        manifest=manifest,
    ) == []
    assert any(
        requirement["content_role"] == "assigned_evidence_index"
        for requirement in origin_attachment_requirements(manifest)
    )

    missing_entry_manifest = json.loads(json.dumps(manifest))
    missing_entry_manifest["assigned_evidence"]["atoms"] = missing_entry_manifest[
        "assigned_evidence"
    ]["atoms"][:-1]
    missing_problem = dict(raw_problem)
    missing_assignment = dict(assignment)
    missing_assignment["origin_attachment_evidence"] = missing_entry_manifest
    missing_problem["evidence_assignment"] = missing_assignment
    missing_problem["origin_attachment_evidence"] = missing_entry_manifest
    try:
        research_runner._append_prompt_for_problem(
            repo_root=Path(__file__).resolve().parents[3],
            problem_payload=missing_problem,
        )
    except ValueError as exc:
        assert "assigned_evidence_index_missing_prompt_atoms:atom:large:11" in str(exc)
    else:  # pragma: no cover - fail closed is the contract under test
        raise AssertionError("missing assigned-evidence index entry was silently dropped")


def test_assigned_evidence_rejects_atom_that_differs_from_authoritative_receipt(
    tmp_path: Path,
) -> None:
    atom = {
        "atom_id": "atom:mismatch",
        "text": "observed failure A",
        "evidence_role": "observation",
        "origin_stage": "runtime",
    }
    conflicting_snapshot = {
        **source_evidence_atom_projection(atom),
        "text": "different failure B",
    }
    assignment: dict[str, object] = {
        "status": "complete",
        "errors": [],
        "case_id": "case:mismatch",
        "problem_id": "problem:mismatch",
        "expected_atom_ids": [atom["atom_id"]],
        "atom_receipts": [
            {
                "atom_id": atom["atom_id"],
                "atom_sha256": sha256(
                    json.dumps(
                        conflicting_snapshot,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "atom_snapshot": conflicting_snapshot,
                "source_projection_version": 1,
                "artifact_receipts": [],
                "origin_evidence_mode": "signed_snapshot",
            }
        ],
    }
    assignment["assignment_sha256"] = evidence_assignment_sha256(assignment)

    try:
        materialize_origin_attachments(
            atoms=[atom],
            workspace_dir=tmp_path / "workspace",
            source_root=tmp_path,
            evidence_assignment=assignment,
        )
    except ValueError as exc:
        assert str(exc) == "assigned_evidence_atom_snapshot_mismatch:atom:mismatch"
    else:  # pragma: no cover - conflicting source evidence must fail closed
        raise AssertionError("conflicting evidence atom and assignment receipt were accepted")


def test_hash_mismatch_is_retained_and_never_materialized(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "one"
    run_dir.mkdir(parents=True)
    artifact = run_dir / "agent_stderr.txt"
    artifact.write_text("observed failure")
    workspace = tmp_path / "workspace"

    manifest = materialize_origin_attachments(
        atoms=[_atom(run_dir=run_dir, artifact=artifact, digest="a" * 64)],
        workspace_dir=workspace,
        source_root=tmp_path,
    )

    assert manifest["artifacts"] == []
    assert manifest["atom_refs"] == []
    assert manifest["errors"][0]["error"] == "attachment_artifact_sha256_mismatch"


def test_attachment_cannot_escape_its_run_directory(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "one"
    run_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("host-only secret")
    atom = {
        "atom_id": "atom:escape",
        "run_dir": str(run_dir),
        "attachments": [
            {
                "artifact_ref": {
                    "path": str(outside),
                    "sha256": sha256(outside.read_bytes()).hexdigest(),
                }
            }
        ],
    }

    manifest = materialize_origin_attachments(
        atoms=[atom],
        workspace_dir=tmp_path / "workspace",
        source_root=tmp_path,
    )

    assert manifest["artifacts"] == []
    assert manifest["errors"][0]["error"] == "attachment_artifact_outside_source_boundary"


def test_binary_attachment_retains_exact_raw_bytes_but_requires_only_bounded_summary(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "runs" / "one"
    run_dir.mkdir(parents=True)
    # Thousands of separate printable spans exercise the fixed-size selection heap.
    # The useful diagnostic is deliberately late so a head-only extraction would miss it.
    raw = b"".join(
        f"ordinary-record-{index:05d}".encode("ascii") + b"\x00"
        for index in range(5_000)
    )
    signature = b"FATAL_ROOT_CAUSE_late_binary_diagnostic"
    raw += signature + b"\x00\xff\x80"
    artifact = run_dir / "crash.bin"
    artifact.write_bytes(raw)
    digest = sha256(raw).hexdigest()
    workspace = tmp_path / "workspace"

    manifest = materialize_origin_attachments(
        atoms=[_atom(run_dir=run_dir, artifact=artifact, digest=digest)],
        workspace_dir=workspace,
        source_root=tmp_path,
    )

    assert manifest["errors"] == []
    retained = manifest["artifacts"][0]
    assert retained["representation"] == "bounded_binary_summary"
    assert retained["binary_kind"] == "binary"
    raw_path = workspace / retained["raw_file"]
    assert raw_path.read_bytes() == raw
    assert retained["raw_file_sha256"] == digest
    requirements = origin_attachment_requirements(
        manifest,
        atom_ids=["atom:large-attachment"],
    )
    summary_requirement = next(
        item for item in requirements if item["content_role"] == "bounded_binary_summary"
    )
    assert any(item["content_role"] == "assigned_evidence_index" for item in requirements)
    assert summary_requirement["size_bytes"] <= 12 * 1024
    assert not str(summary_requirement["file"]).endswith(".hex")
    summary = json.loads(
        (workspace / str(summary_requirement["file"])).read_text(encoding="utf-8")
    )
    extraction = summary["printable_extraction"]
    assert extraction["total_sequence_count"] >= 5_001
    assert extraction["selected_sequence_count"] <= 96
    assert any(signature.decode("ascii") in item["text"] for item in extraction["sequences"])
    assert verify_materialized_origin_attachments(
        workspace_dir=workspace,
        manifest=manifest,
    ) == []

    raw_path.write_bytes(raw + b"tampered")
    assert any(
        error.startswith("origin_attachment_raw_changed:")
        for error in verify_materialized_origin_attachments(
            workspace_dir=workspace,
            manifest=manifest,
        )
    )


def test_zip_binary_summary_lists_archive_without_extracting_members(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "one"
    run_dir.mkdir(parents=True)
    artifact = run_dir / "diagnostics.zip"
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("logs/failure.txt", "fatal: worker startup failed")
        archive.writestr("config/runtime.json", '{"mode":"broken"}')
    raw = artifact.read_bytes()
    workspace = tmp_path / "workspace"

    manifest = materialize_origin_attachments(
        atoms=[
            _atom(
                run_dir=run_dir,
                artifact=artifact,
                digest=sha256(raw).hexdigest(),
            )
        ],
        workspace_dir=workspace,
        source_root=tmp_path,
    )

    retained = manifest["artifacts"][0]
    assert retained["binary_kind"] == "zip"
    requirement = origin_attachment_requirements(manifest)[0]
    summary = json.loads(
        (workspace / str(requirement["file"])).read_text(encoding="utf-8")
    )
    listing = summary["archive_listing"]
    assert listing["status"] == "listed_without_extraction"
    assert {entry["name"] for entry in listing["entries"]} == {
        "logs/failure.txt",
        "config/runtime.json",
    }
    assert not (workspace / "logs" / "failure.txt").exists()
    assert (workspace / retained["raw_file"]).read_bytes() == raw


def test_materializes_bounded_hash_bound_source_run_context(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "source"
    run_dir.mkdir(parents=True)
    preflight = run_dir / "preflight.json"
    preflight.write_text(
        json.dumps(
            {
                "meta": {
                    "command_probe_details": {
                        "bash": {
                            "usable": False,
                            "reason": "not enough disk space on C:",
                        }
                    }
                },
                "api_key": "MUST_NOT_LEAK",
            }
        ),
        encoding="utf-8",
    )
    shell_probe_events = run_dir / "agent_shell_probe" / "raw_events.jsonl"
    shell_probe_events.parent.mkdir()
    shell_probe_events.write_text(
        "\n".join(
            (
                '{"type":"thread.started","thread_id":"thread:one"}',
                '{"type":"item.completed","item":{"id":"item_0",'
                '"type":"command_execution","command":"/bin/bash -lc probe",'
                '"aggregated_output":"shell_probe=ok\\n","exit_code":0,'
                '"status":"completed"}}',
                '{"type":"turn.completed"}',
            )
        )
        + "\n",
        encoding="utf-8",
    )
    settings = run_dir / "settings_ref.json"
    settings.write_text(
        json.dumps(
            {
                "settings": {
                    "profile": "research",
                    "applied": {
                        "exec_backend": "local",
                        "exec_use_host_agent_login": True,
                        "token": "MUST_NOT_LEAK",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "prompt.txt").write_text("DO_NOT_COPY_PROMPT_SECRET", encoding="utf-8")
    atom = {"atom_id": "atom:source-context", "run_dir": str(run_dir)}

    def artifact_receipt(path: Path, role: str) -> dict[str, object]:
        raw = path.read_bytes()
        return {
            "path": str(path),
            "sha256": sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "source_relpath": path.relative_to(run_dir).as_posix(),
            "research_context_role": role,
        }

    assignment = {
        "atom_receipts": [
                {
                    "atom_id": atom["atom_id"],
                    "atom_sha256": sha256(
                        json.dumps(atom, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    "atom_snapshot": atom,
                "artifact_receipts": [
                    artifact_receipt(preflight, "preflight"),
                    artifact_receipt(shell_probe_events, "agent_shell_probe_events"),
                    artifact_receipt(settings, "settings"),
                ],
            }
        ]
    }
    workspace = tmp_path / "workspace"
    manifest = materialize_origin_attachments(
        atoms=[atom],
        workspace_dir=workspace,
        source_root=tmp_path,
        evidence_assignment=assignment,
    )
    assignment["origin_attachment_evidence"] = manifest

    assert manifest["errors"] == []
    requirements = origin_attachment_requirements(
        manifest,
        atom_ids=["atom:source-context"],
    )
    assert [item["content_role"] for item in requirements] == [
        "source_run_context_index",
        "assigned_evidence_index",
    ]
    context_requirement = next(
        item for item in requirements if item["content_role"] == "source_run_context_index"
    )
    index_text = (workspace / str(context_requirement["file"])).read_text(encoding="utf-8")
    assert "not enough disk space on C:" in index_text
    assert '"source_name": "agent_shell_probe/raw_events.jsonl"' in index_text
    assert '"aggregated_output": "shell_probe=ok"' in index_text
    assert '"successful_command_count": 1' in index_text
    assert '"turn_completed_count": 1' in index_text
    assert '"exec_backend": "local"' in index_text
    assert "MUST_NOT_LEAK" not in index_text
    assert "DO_NOT_COPY_PROMPT_SECRET" not in index_text
    assert str(run_dir) not in index_text
    prompt = research_runner._append_prompt_for_problem(
        repo_root=Path(__file__).resolve().parents[3],
        problem_payload={"problem_id": "problem:context", "evidence_assignment": assignment},
    )
    assert str(context_requirement["file"]) in prompt
    assert "read the complete run_context index_file" in prompt
    assert str(preflight) not in prompt
    assert verify_materialized_origin_attachments(
        workspace_dir=workspace,
        manifest=manifest,
        evidence_assignment=assignment,
    ) == []

    research_run = tmp_path / "research-run"
    research_run.mkdir()
    (research_run / "normalized_events.jsonl").write_text("", encoding="utf-8")
    receipts, read_errors = research_runner._origin_attachment_read_receipts(
        run_dir=research_run,
        workspace_dir=workspace,
        manifest=manifest,
    )
    assert receipts == []
    assert read_errors == [
        f"origin_attachment_chunk_not_read_in_full:{item['file']}"
        for item in requirements
    ]

    original_preflight = preflight.read_bytes()
    preflight.write_text('{"tampered": true}', encoding="utf-8")
    source_errors = verify_materialized_origin_attachments(
        workspace_dir=workspace,
        manifest=manifest,
        evidence_assignment=assignment,
    )
    assert any(
        "source_projection_failed:run_context_source_hash_mismatch" in item
        for item in source_errors
    )
    preflight.write_bytes(original_preflight)

    index_path = workspace / str(context_requirement["file"])
    index_path.write_text('{"tampered": true}', encoding="utf-8")
    assert any(
        item.startswith("origin_attachment_chunk_changed:")
        for item in verify_materialized_origin_attachments(
            workspace_dir=workspace,
            manifest=manifest,
            evidence_assignment=assignment,
        )
    )
