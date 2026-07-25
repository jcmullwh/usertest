from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path

import pytest

from usertest_backlog.workflows.qualification_run_manifest import (
    build_semantic_run_evidence_manifest,
    collect_atom_artifact_specs,
    collect_outcome_artifact_paths,
    extend_semantic_manifest_atom_closure,
    verify_semantic_run_evidence_manifest,
)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _canonical_run(
    root: Path,
    *,
    timestamp: str = "20260101T000000Z",
) -> Path:
    run = root / "fixture" / timestamp / "codex" / "0"
    _write(run / "target_ref.json", '{"repo_input":"fixture"}\n')
    _write(run / "report.json", '{"status":"failed"}\n')
    return run


def _manifest(
    root: Path,
    *,
    root_role: str = "primary",
    atom_specs: list[dict[str, object]] | None = None,
    outcome_paths: list[str] | None = None,
) -> dict[str, object]:
    return build_semantic_run_evidence_manifest(
        root,
        name="fixture_runs",
        target_slug="fixture",
        root_role=root_role,
        atom_artifact_specs=atom_specs or [],
        outcome_artifact_paths=outcome_paths or [],
    )


def test_semantic_manifest_tracks_reader_inputs_but_ignores_unread_artifacts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    run = _canonical_run(root)
    sealed = _manifest(root)

    _write(root / "_workspaces" / "large" / "blob.bin", "unread workspace\n")
    _write(run / "sandbox" / "unrelated.bin", "unread sandbox\n")
    _write(run / "prompt.txt", "not read by embed=none\n")
    _write(run / "persona.source.md", "not read by embed=none\n")
    _write(run / "cache.pyc", "bytecode\n")
    _write(run / "verification.json", "{}\n")

    assert verify_semantic_run_evidence_manifest(sealed, name="fixture") == []

    _write(run / "report.json", '{"status":"changed"}\n')
    assert verify_semantic_run_evidence_manifest(sealed, name="fixture") == [
        "qualification_input_semantic_tree_changed:fixture"
    ]


def test_semantic_manifest_tracks_cleanup_sidecar_and_run_inventory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    run = _canonical_run(root)
    sealed = _manifest(root)

    _write(run / "sandbox" / "maintenance_image_cleanup.json", "{}\n")
    assert verify_semantic_run_evidence_manifest(sealed, name="fixture") == [
        "qualification_input_semantic_tree_changed:fixture"
    ]

    sealed = _manifest(root)
    _canonical_run(root, timestamp="20260102T000000Z")
    assert verify_semantic_run_evidence_manifest(sealed, name="fixture") == [
        "qualification_input_semantic_tree_changed:fixture"
    ]


def test_semantic_manifest_tracks_nested_shell_probe_origin_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    run = _canonical_run(root)
    shell_probe_events = _write(
        run / "agent_shell_probe" / "raw_events.jsonl",
        '{"type":"turn.completed"}\n',
    )

    sealed = _manifest(root)

    assert (
        "agent_shell_probe/raw_events.jsonl"
        in sealed["artifact_contract"]["origin_evidence_relative_paths"]
    )
    assert verify_semantic_run_evidence_manifest(sealed, name="fixture") == []
    _write(shell_probe_events, '{"type":"turn.failed"}\n')
    assert verify_semantic_run_evidence_manifest(sealed, name="fixture") == [
        "qualification_input_semantic_tree_changed:fixture"
    ]


def test_semantic_manifest_tracks_implementation_orphan_recovery_inputs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "usertest_implement"
    orphan = root / "fixture" / "20260101T000000Z" / "codex" / "0"
    orphan.mkdir(parents=True)
    sealed = _manifest(root, root_role="implementation")

    assert sealed["inventory"] == [
        {
            "run_rel": "fixture/20260101T000000Z/codex/0",
            "kind": "orphan_candidate",
        }
    ]
    _write(orphan / "error.json", '{"error":"late"}\n')
    assert verify_semantic_run_evidence_manifest(sealed, name="fixture") == [
        "qualification_input_semantic_tree_changed:fixture"
    ]


def test_atom_closure_is_external_root_aware_content_bound_and_move_stable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "retained-outside-repo"
    run = _canonical_run(root)
    artifact = _write(run / "command_failures" / "failure.json", '{"exit":1}\n')
    run_rel = "fixture/20260101T000000Z/codex/0"
    atom = {
        "atom_id": "atom:failure",
        "run_dir": run_rel,
        "artifact_ref": {
            "path": "command_failures/failure.json",
            "sha256": sha256(artifact.read_bytes()).hexdigest(),
            "size_bytes": artifact.stat().st_size,
        },
    }
    base = _manifest(root, root_role="retained")
    extended = extend_semantic_manifest_atom_closure(
        base,
        atoms=[atom],
        repo_root=tmp_path / "unrelated-repo",
    )

    assert extended["atom_artifact_paths"] == [
        f"{run_rel}/command_failures/failure.json"
    ]
    assert extended["run_receipts"] != base["run_receipts"]
    assert verify_semantic_run_evidence_manifest(extended, name="fixture") == []

    moved_root = tmp_path / "moved-retained-root"
    moved_run = _canonical_run(moved_root)
    _write(moved_run / "command_failures" / "failure.json", '{"exit":1}\n')
    moved_atom = {**atom, "run_dir": run_rel}
    moved = extend_semantic_manifest_atom_closure(
        _manifest(moved_root, root_role="retained"),
        atoms=[moved_atom],
        repo_root=tmp_path / "unrelated-repo",
    )
    assert extended["run_receipts"] == moved["run_receipts"]

    _write(artifact, '{"exit":2}\n')
    assert verify_semantic_run_evidence_manifest(extended, name="fixture") == [
        "qualification_input_semantic_tree_changed:fixture"
    ]


def test_atom_referenced_directory_seals_only_its_recursive_contents(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    run = _canonical_run(root)
    command_dir = run / "command_failures" / "cmd_01"
    stdout = _write(command_dir / "stdout.txt", "failed\n")
    _write(command_dir / "request" / "command.json", '{"command":"false"}\n')
    unrelated = _write(run / "sandbox" / "ordinary.bin", "unrelated\n")
    atom = {
        "run_dir": str(run),
        "artifacts": {"command_failure": str(command_dir)},
    }
    sealed = extend_semantic_manifest_atom_closure(
        _manifest(root),
        atoms=[atom],
        repo_root=tmp_path,
    )
    paths = {
        entry["path"]: entry["kind"] for entry in sealed["atom_artifact_entries"]
    }

    assert paths[
        "fixture/20260101T000000Z/codex/0/command_failures/cmd_01"
    ] == "directory"
    assert paths[
        "fixture/20260101T000000Z/codex/0/command_failures/cmd_01/stdout.txt"
    ] == "file"
    assert paths[
        "fixture/20260101T000000Z/codex/0/command_failures/cmd_01/request/command.json"
    ] == "file"
    assert not any("ordinary.bin" in path for path in paths)

    _write(unrelated, "changed but still unrelated\n")
    assert verify_semantic_run_evidence_manifest(sealed, name="fixture") == []
    _write(stdout, "different failure\n")
    assert verify_semantic_run_evidence_manifest(sealed, name="fixture") == [
        "qualification_input_semantic_tree_changed:fixture"
    ]


def test_atom_referenced_directory_rejects_nested_reparse_point(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    run = _canonical_run(root)
    command_dir = run / "command_failures" / "cmd_01"
    _write(command_dir / "stdout.txt", "failed\n")
    outside = tmp_path / "outside"
    _write(outside / "secret.txt", "secret\n")
    try:
        os.symlink(outside, command_dir / "escape", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable on this host")
    atom = {
        "run_dir": str(run),
        "artifacts": {"command_failure": str(command_dir)},
    }

    with pytest.raises(ValueError, match="reparse_rejected"):
        extend_semantic_manifest_atom_closure(
            _manifest(root),
            atoms=[atom],
            repo_root=tmp_path,
        )


@pytest.mark.parametrize("mismatch", ["sha256", "size_bytes"])
def test_atom_closure_rejects_declared_receipt_mismatch(
    tmp_path: Path,
    mismatch: str,
) -> None:
    root = tmp_path / "runs"
    run = _canonical_run(root)
    artifact = _write(run / "command_failures" / "failure.json", "failure\n")
    ref: dict[str, object] = {
        "path": "command_failures/failure.json",
        "sha256": sha256(artifact.read_bytes()).hexdigest(),
        "size_bytes": artifact.stat().st_size,
    }
    ref[mismatch] = "0" * 64 if mismatch == "sha256" else artifact.stat().st_size + 1
    atom = {"run_dir": str(run), "artifact_ref": ref}

    expected_error = "hash_mismatch" if mismatch == "sha256" else "size_mismatch"
    with pytest.raises(ValueError, match=f"atom_artifact_{expected_error}"):
        extend_semantic_manifest_atom_closure(
            _manifest(root),
            atoms=[atom],
            repo_root=tmp_path,
        )


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("sha256", "not-a-hash", "sha256_invalid"),
        ("size_bytes", "eight", "size_invalid"),
        ("exists", "yes", "exists_invalid"),
    ],
)
def test_atom_closure_rejects_malformed_declared_expectations(
    tmp_path: Path,
    field: str,
    value: object,
    expected_error: str,
) -> None:
    root = tmp_path / "runs"
    run = _canonical_run(root)
    _write(run / "command_failures" / "failure.json", "failure\n")
    atom = {
        "run_dir": str(run),
        "artifact_ref": {
            "path": "command_failures/failure.json",
            field: value,
        },
    }
    with pytest.raises(ValueError, match=f"atom_artifact_{expected_error}"):
        extend_semantic_manifest_atom_closure(
            _manifest(root),
            atoms=[atom],
            repo_root=tmp_path,
        )


def test_atom_closure_enforces_existence_and_exact_run_binding(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run = _canonical_run(root)
    missing_atom = {
        "run_dir": str(run),
        "artifact_ref": {"path": "command_failures/missing.json", "exists": False},
    }
    sealed = extend_semantic_manifest_atom_closure(
        _manifest(root),
        atoms=[missing_atom],
        repo_root=tmp_path,
    )
    assert verify_semantic_run_evidence_manifest(sealed, name="fixture") == []
    _write(run / "command_failures" / "missing.json", "{}\n")
    assert verify_semantic_run_evidence_manifest(sealed, name="fixture") == [
        "qualification_input_semantic_tree_changed:fixture"
    ]

    present_required = {
        "run_dir": str(run),
        "artifact_ref": {"path": "command_failures/absent.json", "exists": True},
    }
    with pytest.raises(ValueError, match="expected_artifact_missing"):
        extend_semantic_manifest_atom_closure(
            _manifest(root),
            atoms=[present_required],
            repo_root=tmp_path,
        )

    broad_run = {
        "run_dir": str(root),
        "artifact_ref": {
            "path": "fixture/20260101T000000Z/codex/0/report.json"
        },
    }
    with pytest.raises(ValueError, match="run_dir_not_canonical"):
        collect_atom_artifact_specs([broad_run], repo_root=tmp_path, roots=[root])


def test_atom_closure_rejects_escape_and_reparse_paths(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    run = _canonical_run(root)
    outside = _write(tmp_path / "outside" / "secret.json", "{}\n")
    escape = {
        "run_dir": str(run),
        "artifact_ref": {"path": "../../../../outside/secret.json"},
    }
    with pytest.raises(ValueError, match="semantic_path_invalid"):
        collect_atom_artifact_specs([escape], repo_root=tmp_path, roots=[root])

    link = run / "linked"
    try:
        os.symlink(outside.parent, link, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable on this host")
    linked = {
        "run_dir": str(run),
        "artifact_ref": {"path": "linked/secret.json"},
    }
    with pytest.raises(ValueError, match="reparse_rejected"):
        collect_atom_artifact_specs([linked], repo_root=tmp_path, roots=[root])


def test_non_run_outcome_provenance_is_root_sealed_not_run_bound(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runs"
    _canonical_run(root)
    compiled = root / "fixture" / "_compiled" / "relation_review"
    response = _write(compiled / "response.txt", "same cause\n")
    receipt = _write(
        compiled / "receipt.json",
        json.dumps({"relation_review_response_path": str(response)}) + "\n",
    )
    outcome = {
        "schema_version": 1,
        "case_id": "case:one",
        "outcome_scope": "case",
        "state": "duplicate",
        "relation_receipt": {"receipt_path": str(receipt)},
    }
    paths = collect_outcome_artifact_paths([outcome], roots=[root])[root.resolve()]
    sealed = _manifest(root, outcome_paths=paths)

    assert len(sealed["outcome_artifact_entries"]) == 2
    assert verify_semantic_run_evidence_manifest(sealed, name="fixture") == []
    _write(response, "different cause\n")
    assert verify_semantic_run_evidence_manifest(sealed, name="fixture") == [
        "qualification_input_semantic_tree_changed:fixture"
    ]
