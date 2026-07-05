from __future__ import annotations

from pathlib import Path

from backlog_repo.plan_index import (
    reconcile_atom_actions_from_plan_folders,
    scan_plan_ticket_index,
    sync_atom_actions_from_dequeued_plan_folders,
)


def test_sync_atom_actions_from_dequeued_plan_folders_demotes_queued(tmp_path: Path) -> None:
    owner_root = tmp_path
    dequeued_dir = owner_root / ".agents" / "plans" / "_dequeued"
    dequeued_dir.mkdir(parents=True, exist_ok=True)

    atom_id = "usertest/20260220T194226Z/codex/0:suggested_change:2"
    (dequeued_dir / "ticket.md").write_text(f"Evidence: `{atom_id}`\n", encoding="utf-8")

    atom_actions = {atom_id: {"atom_id": atom_id, "status": "queued"}}
    meta = sync_atom_actions_from_dequeued_plan_folders(
        atom_actions=atom_actions,
        owner_roots=[owner_root],
        generated_at="2026-02-28T00:00:00Z",
    )

    assert meta["atoms_demoted"] == 1
    assert atom_actions[atom_id]["status"] == "new"
    assert atom_actions[atom_id]["last_dequeued_at"] == "2026-02-28T00:00:00Z"


def test_sync_atom_actions_from_dequeued_plan_folders_demotes_actioned_when_removed(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path
    dequeued_dir = owner_root / ".agents" / "plans" / "_dequeued"
    dequeued_dir.mkdir(parents=True, exist_ok=True)

    atom_id = "usertest/20260220T194226Z/codex/0:suggested_change:2"
    (dequeued_dir / "ticket.md").write_text(f"Evidence: `{atom_id}`\n", encoding="utf-8")

    atom_actions = {atom_id: {"atom_id": atom_id, "status": "actioned"}}
    meta = sync_atom_actions_from_dequeued_plan_folders(
        atom_actions=atom_actions,
        owner_roots=[owner_root],
        generated_at="2026-02-28T00:00:00Z",
    )

    assert meta["atoms_demoted"] == 1
    assert atom_actions[atom_id]["status"] == "new"


def test_sync_atom_actions_from_dequeued_plan_folders_ignores_hidden_archive(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path
    archived_snapshot_dir = owner_root / ".agents" / "plans" / "_archive" / "snapshot"
    archived_snapshot_dir.mkdir(parents=True, exist_ok=True)
    atom_id = "usertest/20260220T194226Z/codex/0:suggested_change:2"
    (archived_snapshot_dir / "20260227_0123456789abcdef_old-ticket.md").write_text(
        f"# Old ticket\n\nEvidence: `{atom_id}`\n",
        encoding="utf-8",
    )
    atom_actions = {atom_id: {"status": "actioned"}}

    meta = sync_atom_actions_from_dequeued_plan_folders(
        atom_actions=atom_actions,
        owner_roots=[owner_root],
        generated_at="2026-02-28T00:00:00Z",
    )

    assert meta["ticket_files_scanned"] == 0
    assert meta["atoms_demoted"] == 0
    assert atom_actions[atom_id]["status"] == "actioned"


def test_scan_plan_ticket_index_treats_archived_as_actioned(tmp_path: Path) -> None:
    owner_root = tmp_path
    archived_dir = owner_root / ".agents" / "plans" / "6 - archived"
    archived_dir.mkdir(parents=True, exist_ok=True)

    fingerprint = "0123456789abcdef"
    plan_name = f"20260228_BLG-001_{fingerprint}_Archived-plan.md"
    (archived_dir / plan_name).write_text("# Archived plan\n", encoding="utf-8")

    index = scan_plan_ticket_index(owner_root=owner_root)

    assert fingerprint in index
    assert index[fingerprint]["status"] == "actioned"
    assert index[fingerprint]["buckets"] == ["6 - archived"]


def test_scan_plan_ticket_index_can_exclude_discarded_for_export_dedupe(
    tmp_path: Path,
) -> None:
    owner_root = tmp_path
    discarded_dir = owner_root / ".agents" / "plans" / "0.2 - discarded"
    discarded_dir.mkdir(parents=True, exist_ok=True)

    fingerprint = "0123456789abcdef"
    (discarded_dir / f"20260228_{fingerprint}_bad-solution.md").write_text(
        "# Discarded plan\n",
        encoding="utf-8",
    )

    full_index = scan_plan_ticket_index(owner_root=owner_root)
    export_dedupe_index = scan_plan_ticket_index(
        owner_root=owner_root,
        include_discarded=False,
    )

    assert full_index[fingerprint]["status"] == "discarded"
    assert fingerprint not in export_dedupe_index


def test_scan_plan_ticket_index_normalizes_legacy_tkt_filenames(tmp_path: Path) -> None:
    owner_root = tmp_path
    complete_dir = owner_root / ".agents" / "plans" / "5 - complete"
    complete_dir.mkdir(parents=True, exist_ok=True)

    fingerprint = "0123456789abcdef"
    legacy_path = (
        complete_dir
        / f"20260228_TKT-123456789abc_{fingerprint}_legacy-complete-ticket.md"
    )
    legacy_path.write_text(
        "# Legacy ticket\n\n- Source ticket: `TKT-123456789abc`\n",
        encoding="utf-8",
    )

    index = scan_plan_ticket_index(owner_root=owner_root)
    normalized_path = complete_dir / f"20260228_{fingerprint}_legacy-complete-ticket.md"

    assert fingerprint in index
    assert normalized_path.exists()
    assert not legacy_path.exists()
    assert index[fingerprint]["paths"] == [str(normalized_path)]
    assert "Source ticket" not in normalized_path.read_text(encoding="utf-8")


def test_reconcile_discarded_plan_demotes_by_fingerprint(tmp_path: Path) -> None:
    owner_root = tmp_path
    discarded_dir = owner_root / ".agents" / "plans" / "0.2 - discarded"
    discarded_dir.mkdir(parents=True, exist_ok=True)

    fingerprint = "0123456789abcdef"
    atom_id = "usertest/20260220T194226Z/codex/0:suggested_change:2"
    (discarded_dir / f"20260228_{fingerprint}_bad-solution.md").write_text(
        "# Discarded plan\n",
        encoding="utf-8",
    )

    atom_actions = {
        atom_id: {"atom_id": atom_id, "status": "queued", "fingerprints": [fingerprint]}
    }
    meta = reconcile_atom_actions_from_plan_folders(
        atom_actions=atom_actions,
        owner_roots=[owner_root],
        generated_at="2026-02-28T00:00:00Z",
    )

    assert meta["removal_sync"]["discarded_ticket_files_scanned"] == 1
    assert atom_actions[atom_id]["status"] == "new"
    assert atom_actions[atom_id]["last_discarded_at"] == "2026-02-28T00:00:00Z"
    assert atom_actions[atom_id]["discarded_fingerprints"] == [fingerprint]


def test_reconcile_archived_plan_promotes_by_fingerprint_without_atom_ids(tmp_path: Path) -> None:
    owner_root = tmp_path
    archived_dir = owner_root / ".agents" / "plans" / "6 - archived"
    archived_dir.mkdir(parents=True, exist_ok=True)

    fingerprint = "0123456789abcdef"
    atom_id = "usertest/20260220T194226Z/codex/0:suggested_change:2"
    (archived_dir / f"20260228_{fingerprint}_archived.md").write_text(
        "# Archived plan\n",
        encoding="utf-8",
    )

    atom_actions = {
        atom_id: {"atom_id": atom_id, "status": "queued", "fingerprints": [fingerprint]}
    }
    meta = reconcile_atom_actions_from_plan_folders(
        atom_actions=atom_actions,
        owner_roots=[owner_root],
        generated_at="2026-02-28T00:00:00Z",
    )

    assert meta["plan_sync"]["atoms_promoted"] == 1
    assert atom_actions[atom_id]["status"] == "actioned"
    assert atom_actions[atom_id]["last_plan_bucket"] == "6 - archived"


def test_reconcile_missing_queue_path_demotes_stale_atom(tmp_path: Path) -> None:
    owner_root = tmp_path
    (owner_root / ".agents" / "plans").mkdir(parents=True, exist_ok=True)

    atom_id = "usertest/20260220T194226Z/codex/0:suggested_change:2"
    atom_actions = {
        atom_id: {
            "atom_id": atom_id,
            "status": "queued",
            "fingerprints": ["0123456789abcdef"],
            "queue_paths": [str(owner_root / ".agents" / "plans" / "2 - ready" / "missing.md")],
        }
    }
    meta = reconcile_atom_actions_from_plan_folders(
        atom_actions=atom_actions,
        owner_roots=[owner_root],
        generated_at="2026-02-28T00:00:00Z",
    )

    assert meta["missing_plan_sync"]["atoms_demoted"] == 1
    assert atom_actions[atom_id]["status"] == "new"
    assert atom_actions[atom_id]["last_reconciled_missing_plan_at"] == "2026-02-28T00:00:00Z"
