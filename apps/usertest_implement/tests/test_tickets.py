from __future__ import annotations

from pathlib import Path

from usertest_implement.tickets import build_ticket_index, move_ticket_file, select_next_ticket


def test_ticket_index_and_move(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    ready_dir = owner_root / ".agents" / "plans" / "2 - ready"
    in_progress_dir = owner_root / ".agents" / "plans" / "3 - in_progress"
    ready_dir.mkdir(parents=True)
    in_progress_dir.mkdir(parents=True)

    fingerprint = "deadbeefdeadbeef"
    ticket_path = ready_dir / f"20260220_BLG-003_{fingerprint}_fix-something.md"
    ticket_path.write_text("# Fix something\n\n- Fingerprint: `deadbeefdeadbeef`\n", encoding="utf-8")

    index = build_ticket_index(owner_root=owner_root)
    assert fingerprint in index

    entry = select_next_ticket(index, bucket_priority=["2 - ready"])
    assert entry is not None
    assert entry.fingerprint == fingerprint
    assert entry.ticket_id == "BLG-003"

    dest_dry = move_ticket_file(
        owner_root=owner_root,
        fingerprint=fingerprint,
        to_bucket="3 - in_progress",
        dry_run=True,
    )
    assert dest_dry == in_progress_dir / ticket_path.name
    assert ticket_path.exists()

    dest = move_ticket_file(
        owner_root=owner_root,
        fingerprint=fingerprint,
        to_bucket="3 - in_progress",
        dry_run=False,
    )
    assert dest.exists()
    assert not ticket_path.exists()

