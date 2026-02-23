from __future__ import annotations

from pathlib import Path

from usertest_implement.tickets import build_ticket_index, move_ticket_file, select_next_ticket_path


def test_ticket_index_and_move(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    ready_dir = owner_root / ".agents" / "plans" / "2 - ready"
    in_progress_dir = owner_root / ".agents" / "plans" / "3 - in_progress"
    ready_dir.mkdir(parents=True)
    in_progress_dir.mkdir(parents=True)

    fingerprint = "deadbeefdeadbeef"
    ticket_path = ready_dir / f"20260220_BLG-003_{fingerprint}_fix-something.md"
    ticket_path.write_text(
        "# Fix something\n\n- Fingerprint: `deadbeefdeadbeef`\n",
        encoding="utf-8",
    )

    index = build_ticket_index(owner_root=owner_root)
    assert fingerprint in index

    selected = select_next_ticket_path(
        index,
        bucket_priority=["2 - ready"],
        kind_priority=["research", "implementation"],
    )
    assert selected is not None
    entry, _ = selected
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


def test_select_next_ticket_path_prefers_research(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    ready_dir = owner_root / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True)

    impl_fp = "aaaaaaaaaaaaaaaa"
    impl_path = ready_dir / f"20260220_BLG-001_{impl_fp}_implementation.md"
    impl_path.write_text(
        "# Impl\n\n- Export kind: `implementation`\n- Fingerprint: `aaaaaaaaaaaaaaaa`\n",
        encoding="utf-8",
    )

    research_fp = "bbbbbbbbbbbbbbbb"
    research_path = ready_dir / f"20260220_BLG-002_{research_fp}_research.md"
    research_path.write_text(
        "# Research\n\n- Export kind: `research`\n- Fingerprint: `bbbbbbbbbbbbbbbb`\n",
        encoding="utf-8",
    )

    index = build_ticket_index(owner_root=owner_root)
    selected = select_next_ticket_path(
        index,
        bucket_priority=["2 - ready"],
        kind_priority=["research", "implementation"],
    )
    assert selected is not None
    _, path = selected
    assert path == research_path


def test_move_ticket_file_dedupes_actioned_buckets_and_prevents_downgrade(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    complete_dir = owner_root / ".agents" / "plans" / "5 - complete"
    in_progress_dir = owner_root / ".agents" / "plans" / "3 - in_progress"
    for_review_dir = owner_root / ".agents" / "plans" / "4 - for_review"
    complete_dir.mkdir(parents=True)
    in_progress_dir.mkdir(parents=True)
    for_review_dir.mkdir(parents=True)

    fingerprint = "deadbeefdeadbeef"
    name = f"20260220_BLG-003_{fingerprint}_fix-something.md"
    complete_path = complete_dir / name
    in_progress_path = in_progress_dir / name
    complete_path.write_text("# Done\n\n- Fingerprint: `deadbeefdeadbeef`\n", encoding="utf-8")
    in_progress_path.write_text(
        "# WIP\n\n- Fingerprint: `deadbeefdeadbeef`\n",
        encoding="utf-8",
    )
    assert complete_path.exists()
    assert in_progress_path.exists()

    # Attempting to move "back" from complete -> for_review should no-op to complete.
    dest = move_ticket_file(
        owner_root=owner_root,
        fingerprint=fingerprint,
        to_bucket="4 - for_review",
        dry_run=False,
    )
    assert dest == complete_path
    assert complete_path.exists()
    assert not in_progress_path.exists()
