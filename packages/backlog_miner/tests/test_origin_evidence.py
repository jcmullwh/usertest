from __future__ import annotations

import json
import zipfile
from hashlib import sha256
from pathlib import Path

from backlog_miner.origin_evidence import (
    materialize_origin_attachments,
    origin_attachment_requirements,
    verify_materialized_origin_attachments,
)


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
    assert len(requirements) == 1
    assert requirements[0]["content_role"] == "bounded_binary_summary"
    assert requirements[0]["size_bytes"] <= 12 * 1024
    assert not str(requirements[0]["file"]).endswith(".hex")
    summary = json.loads(
        (workspace / str(requirements[0]["file"])).read_text(encoding="utf-8")
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
