from __future__ import annotations

from pathlib import Path

from backlog_repo.plan_index import scan_plan_ticket_index, sync_atom_actions_from_dequeued_plan_folders


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


def test_sync_atom_actions_from_dequeued_plan_folders_never_demotes_actioned(tmp_path: Path) -> None:
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

    assert meta["atoms_demoted"] == 0
    assert meta["atoms_skipped_actioned"] == 1
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
