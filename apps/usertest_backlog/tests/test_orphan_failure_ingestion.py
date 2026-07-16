from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from backlog_core import (
    build_operational_failure_candidates,
    eligible_problem_mining_atoms,
    operational_candidate_receipt_errors,
)
from backlog_core.case_lineage import empty_case_registry

import usertest_backlog.workflows.orphan_implementation_history as orphan_history_module
from usertest_backlog.workflows.derived_evidence import (
    filter_derived_history_records,
    inferred_implementation_runs_root,
    ingest_derived_evidence_records,
)
from usertest_backlog.workflows.orphan_implementation_history import (
    recover_orphan_implementation_history,
)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _write_orphan_run(
    primary_runs_root: Path,
    *,
    owner_root: Path,
    timestamp: str = "20260708T234924Z",
    target_slug: str = "usertest",
    export_kind: str = "implementation",
    error: dict[str, Any] | None = None,
    include_target_ref: bool = False,
    include_run_finished: bool = True,
) -> Path:
    run_dir = (
        inferred_implementation_runs_root(primary_runs_root)
        / target_slug
        / timestamp
        / "codex"
        / "0"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        run_dir / "error.json",
        error
        or {
            "type": "RuntimeError",
            "subtype": "disk_full",
            "message": "IDEA proposal prose and private clone path must stay excluded.",
        },
    )
    run_meta = {
        "schema_version": 1,
        "run_started_utc": "2026-07-08T23:49:24Z",
        "run_wall_seconds": 21.0,
        "phases": {"setup_seconds": 21.0},
    }
    if include_run_finished:
        run_meta["run_finished_utc"] = "2026-07-08T23:49:45Z"
    _write_json(run_dir / "run_meta.json", run_meta)
    _write_json(
        run_dir / "ticket_ref.json",
        {
            "schema_version": 1,
            "fingerprint": "bd30a84436fefaa5",
            "title": "IDEA-003 Ticket 02: content that must never become evidence",
            "proposal": "An external IDEA proposal that must remain excluded.",
            "export_kind": export_kind,
            "owner_repo": {
                "root": str(owner_root),
                "idea_path": str(owner_root / ".agents" / "plans" / "idea.md"),
            },
        },
    )
    if include_target_ref:
        _write_json(
            run_dir / "target_ref.json",
            {
                "repo_input": str(owner_root),
                "mission_id": "implement_backlog_ticket_v1",
            },
        )
    return run_dir


def test_orphan_setup_failure_is_recovered_and_disk_full_is_the_only_candidate(
    tmp_path: Path,
) -> None:
    primary_root = tmp_path / "runs" / "usertest"
    primary_root.mkdir(parents=True)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    run_dir = _write_orphan_run(primary_root, owner_root=repo_root)

    recovered, recovery_meta = recover_orphan_implementation_history(
        primary_root,
        target_slug="usertest",
        scoped_repo_root=repo_root,
    )

    assert len(recovered) == 1
    recovered_record = recovered[0]
    assert recovered_record["run_dir"] == str(run_dir.resolve())
    assert recovered_record["run_rel"] == "usertest/20260708T234924Z/codex/0"
    assert recovered_record["target_ref"] is None
    assert recovered_record["status"] == "error"
    assert recovered_record["error"]["subtype"] == "disk_full"
    receipt = recovered_record["orphan_history_recovery_receipt"]
    receipt_sha256 = receipt.pop("receipt_sha256")
    assert receipt_sha256 == _canonical_sha256(receipt)
    receipt["receipt_sha256"] = receipt_sha256
    assert set(receipt["artifact_sha256"]) == {
        "error.json",
        "run_meta.json",
        "ticket_ref.json",
    }
    assert recovery_meta["records_recovered"] == 1
    assert recovery_meta["recovery_receipts"] == [receipt]

    scoped, filter_meta = filter_derived_history_records(
        recovered,
        target_slug="usertest",
        repo_input=str(repo_root),
        repo_root=repo_root,
    )
    assert len(scoped) == 1
    assert filter_meta["match_counts"] == {"ticket_owner_root": 1}
    ingestion = ingest_derived_evidence_records(
        scoped,
        source_root=inferred_implementation_runs_root(primary_root),
        repo_root=repo_root,
        atom_actions={},
        case_registry=empty_case_registry(),
    )

    assert ingestion.records[0]["derived_source_orphan_recovery_receipt_sha256"] == receipt_sha256
    assert ingestion.atoms
    assert all(atom["evidence_role"] == "implementation" for atom in ingestion.atoms)
    assert all(atom["derived_parent_binding_status"] == "unavailable" for atom in ingestion.atoms)
    assert all(
        atom["derived_source_orphan_recovery_receipt_sha256"] == receipt_sha256
        for atom in ingestion.atoms
    )
    assert eligible_problem_mining_atoms(ingestion.atoms) == []

    candidates = build_operational_failure_candidates(
        ingestion.records,
        ingestion.atoms,
        parent_bindings_by_run=ingestion.parent_bindings_by_run,
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["operational_failure_class"] == "infrastructure"
    assert candidate["operational_failure_phase"] == "storage"
    assert operational_candidate_receipt_errors(candidate) == []
    serialized_candidate = json.dumps(candidate, sort_keys=True)
    assert "IDEA-003" not in serialized_candidate
    assert "external IDEA proposal" not in serialized_candidate
    assert "private clone path" not in serialized_candidate
    assert candidate["operational_candidate_receipt"]["parent_binding_statuses"] == ["unavailable"]


def test_orphan_recovery_rejects_unscoped_untyped_and_normal_history_runs(
    tmp_path: Path,
) -> None:
    primary_root = tmp_path / "runs" / "usertest"
    primary_root.mkdir(parents=True)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    other_repo = tmp_path / "other"
    other_repo.mkdir()

    _write_orphan_run(
        primary_root,
        owner_root=other_repo,
        timestamp="20260708T210000Z",
    )
    _write_orphan_run(
        primary_root,
        owner_root=repo_root,
        timestamp="20260708T220000Z",
        export_kind="idea",
    )
    _write_orphan_run(
        primary_root,
        owner_root=repo_root,
        timestamp="20260708T230000Z",
        include_target_ref=True,
    )
    untyped_run = _write_orphan_run(
        primary_root,
        owner_root=repo_root,
        timestamp="20260708T225000Z",
        error={
            "type": "RuntimeError",
            "message": "No space left on device and disk_full only in prose.",
        },
    )

    recovered, metadata = recover_orphan_implementation_history(
        primary_root,
        target_slug="usertest",
        scoped_repo_root=repo_root,
    )

    assert [record["run_dir"] for record in recovered] == [str(untyped_run.resolve())]
    assert metadata["exclusion_reason_counts"] == {
        "target_ref_present_or_untrusted": 1,
        "ticket_export_kind_invalid": 1,
        "ticket_owner_root_scope_mismatch": 1,
    }
    ingestion = ingest_derived_evidence_records(
        recovered,
        source_root=inferred_implementation_runs_root(primary_root),
        repo_root=repo_root,
        atom_actions={},
        case_registry=empty_case_registry(),
    )
    assert (
        build_operational_failure_candidates(
            ingestion.records,
            ingestion.atoms,
            parent_bindings_by_run=ingestion.parent_bindings_by_run,
        )
        == []
    )


def test_terminal_error_recovers_even_when_run_meta_has_no_finish_timestamp(
    tmp_path: Path,
) -> None:
    primary_root = tmp_path / "runs" / "usertest"
    primary_root.mkdir(parents=True)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_orphan_run(
        primary_root,
        owner_root=repo_root,
        include_run_finished=False,
    )

    recovered, metadata = recover_orphan_implementation_history(
        primary_root,
        target_slug="usertest",
        scoped_repo_root=repo_root,
    )

    assert len(recovered) == 1
    assert recovered[0]["status"] == "error"
    assert "run_finished_utc" not in recovered[0]["run_meta"]
    assert metadata["records_recovered"] == 1


def test_orphan_recovery_rejects_missing_owner_and_wrong_target(tmp_path: Path) -> None:
    primary_root = tmp_path / "runs" / "usertest"
    primary_root.mkdir(parents=True)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    missing_owner_run = _write_orphan_run(primary_root, owner_root=repo_root)
    ticket_ref = json.loads((missing_owner_run / "ticket_ref.json").read_text(encoding="utf-8"))
    ticket_ref.pop("owner_repo")
    _write_json(missing_owner_run / "ticket_ref.json", ticket_ref)
    _write_orphan_run(
        primary_root,
        owner_root=repo_root,
        target_slug="another_target",
    )

    recovered, metadata = recover_orphan_implementation_history(
        primary_root,
        target_slug="usertest",
        scoped_repo_root=repo_root,
    )

    assert recovered == []
    assert metadata["exclusion_reason_counts"] == {
        "target_slug_mismatch": 1,
        "ticket_owner_root_missing": 1,
    }


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (
            lambda run_dir: (run_dir / "run_meta.json").unlink(),
            "required_artifact_missing_or_untrusted",
        ),
        (
            lambda run_dir: (run_dir / "error.json").write_text("{not-json}", encoding="utf-8"),
            "required_artifact_invalid_json",
        ),
    ],
    ids=("missing-required-artifact", "malformed-required-artifact"),
)
def test_orphan_recovery_requires_all_parseable_runner_owned_artifacts(
    tmp_path: Path,
    mutator: Any,
    reason: str,
) -> None:
    primary_root = tmp_path / "runs" / "usertest"
    primary_root.mkdir(parents=True)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    run_dir = _write_orphan_run(primary_root, owner_root=repo_root)
    mutator(run_dir)

    recovered, metadata = recover_orphan_implementation_history(
        primary_root,
        target_slug="usertest",
        scoped_repo_root=repo_root,
    )

    assert recovered == []
    assert metadata["exclusion_reason_counts"] == {reason: 1}


def test_orphan_recovery_rejects_invalid_layout_and_reparse_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_root = tmp_path / "runs" / "usertest"
    primary_root.mkdir(parents=True)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    invalid_timestamp_run = _write_orphan_run(
        primary_root,
        owner_root=repo_root,
        timestamp="latest",
    )
    assert invalid_timestamp_run.is_dir()

    reparse_run = _write_orphan_run(
        primary_root,
        owner_root=repo_root,
        timestamp="20260708T234925Z",
    )
    reparse_timestamp = reparse_run.parents[1]
    original_is_reparse_point = orphan_history_module._is_reparse_point

    def selected_path_is_reparse_point(path: Path) -> bool:
        return path == reparse_timestamp or original_is_reparse_point(path)

    monkeypatch.setattr(
        orphan_history_module,
        "_is_reparse_point",
        selected_path_is_reparse_point,
    )

    recovered, metadata = recover_orphan_implementation_history(
        primary_root,
        target_slug="usertest",
        scoped_repo_root=repo_root,
    )

    assert recovered == []
    assert metadata["exclusion_reason_counts"] == {
        "timestamp_directory_invalid": 1,
        "timestamp_directory_untrusted": 1,
    }


def test_orphan_scope_filter_fails_closed_without_matching_repo_identity(
    tmp_path: Path,
) -> None:
    primary_root = tmp_path / "runs" / "usertest"
    primary_root.mkdir(parents=True)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_orphan_run(primary_root, owner_root=repo_root)
    recovered, _ = recover_orphan_implementation_history(
        primary_root,
        target_slug="usertest",
        scoped_repo_root=repo_root,
    )

    scoped, metadata = filter_derived_history_records(
        recovered,
        target_slug="usertest",
        repo_input="https://example.invalid/not-the-owner.git",
        repo_root=repo_root,
    )

    assert scoped == []
    assert metadata["records_excluded_repo"] == 1


def test_reparse_check_is_compatible_with_path_without_is_junction() -> None:
    class Python311PathShape:
        @staticmethod
        def is_symlink() -> bool:
            return False

    assert orphan_history_module._is_reparse_point(Python311PathShape()) is False  # type: ignore[arg-type]
