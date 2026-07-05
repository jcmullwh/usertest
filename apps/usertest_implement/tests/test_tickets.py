from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from usertest_implement.cli import main
from usertest_implement.tickets import build_ticket_index, move_ticket_file, select_next_ticket_path


def test_ticket_index_and_move(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    ready_dir = owner_root / ".agents" / "plans" / "2 - ready"
    in_progress_dir = owner_root / ".agents" / "plans" / "3 - in_progress"
    ready_dir.mkdir(parents=True)
    in_progress_dir.mkdir(parents=True)

    fingerprint = "deadbeefdeadbeef"
    ticket_path = ready_dir / f"20260220_{fingerprint}_fix-something.md"
    ticket_path.write_text(
        "# Fix something\n\n"
        "- Export kind: `implementation`\n"
        "- Stage: `ready_for_ticket`\n"
        "- Fingerprint: `deadbeefdeadbeef`\n",
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
    impl_path = ready_dir / f"20260220_{impl_fp}_implementation.md"
    impl_path.write_text(
        "# Impl\n\n"
        "- Export kind: `implementation`\n"
        "- Stage: `ready_for_ticket`\n"
        "- Fingerprint: `aaaaaaaaaaaaaaaa`\n",
        encoding="utf-8",
    )

    research_fp = "bbbbbbbbbbbbbbbb"
    research_path = ready_dir / f"20260220_{research_fp}_research.md"
    research_path.write_text(
        "# Research\n\n"
        "- Export kind: `research`\n"
        "- Stage: `research_required`\n"
        "- Fingerprint: `bbbbbbbbbbbbbbbb`\n",
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


def test_select_next_ticket_path_skips_non_stage6_implementation(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    ready_dir = owner_root / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True)

    triage_fp = "aaaaaaaaaaaaaaaa"
    (ready_dir / f"20260220_{triage_fp}_triage.md").write_text(
        "# Triage\n\n"
        "- Export kind: `implementation`\n"
        "- Stage: `triage`\n"
        "- Fingerprint: `aaaaaaaaaaaaaaaa`\n",
        encoding="utf-8",
    )

    ready_fp = "bbbbbbbbbbbbbbbb"
    ready_path = ready_dir / f"20260220_{ready_fp}_ready.md"
    ready_path.write_text(
        "# Ready\n\n"
        "- Export kind: `implementation`\n"
        "- Stage: `ready_for_ticket`\n"
        "- Fingerprint: `bbbbbbbbbbbbbbbb`\n",
        encoding="utf-8",
    )

    index = build_ticket_index(owner_root=owner_root)
    selected = select_next_ticket_path(
        index,
        bucket_priority=["2 - ready"],
        kind_priority=["implementation"],
    )
    assert selected is not None
    _, path = selected
    assert path == ready_path


def test_move_ticket_file_dedupes_actioned_buckets_and_prevents_downgrade(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    complete_dir = owner_root / ".agents" / "plans" / "5 - complete"
    in_progress_dir = owner_root / ".agents" / "plans" / "3 - in_progress"
    for_review_dir = owner_root / ".agents" / "plans" / "4 - for_review"
    complete_dir.mkdir(parents=True)
    in_progress_dir.mkdir(parents=True)
    for_review_dir.mkdir(parents=True)

    fingerprint = "deadbeefdeadbeef"
    name = f"20260220_{fingerprint}_fix-something.md"
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


def test_tickets_discard_moves_ticket_and_demotes_atom(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    ready_dir = repo_root / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True)

    fingerprint = "deadbeefdeadbeef"
    atom_id = "usertest/20260220T194226Z/codex/0:suggested_change:2"
    ticket_path = ready_dir / f"20260220_{fingerprint}_bad-solution.md"
    ticket_path.write_text(
        "# Bad solution\n\n"
        f"- Fingerprint: `{fingerprint}`\n\n"
        "## Evidence atom ids\n\n"
        f"- `{atom_id}`\n",
        encoding="utf-8",
    )
    atom_actions_path = repo_root / "configs" / "backlog_atom_actions.yaml"
    atom_actions_path.parent.mkdir(parents=True, exist_ok=True)
    atom_actions_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "atoms": [
                    {
                        "atom_id": atom_id,
                        "status": "queued",
                        "fingerprints": [fingerprint],
                        "queue_paths": [str(ticket_path)],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--repo-root",
                str(repo_root),
                "tickets",
                "discard",
                "--owner-root",
                str(repo_root),
                "--fingerprint",
                fingerprint,
                "--reason",
                "bad_solution",
                "--note",
                "Generated fix was not acceptable.",
            ]
        )

    assert exc.value.code == 0
    discarded_path = repo_root / ".agents" / "plans" / "0.2 - discarded" / ticket_path.name
    assert discarded_path.exists()
    assert not ticket_path.exists()

    actions_doc = yaml.safe_load((repo_root / "configs" / "backlog_actions.yaml").read_text())
    action = actions_doc["actions"][0]
    assert action["fingerprint"] == fingerprint
    assert action["status"] == "discarded"
    assert action["discard_reason"] == "bad_solution"

    atom_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atom = atom_doc["atoms"][0]
    assert atom["status"] == "new"
    assert atom["discarded_fingerprints"] == [fingerprint]
    assert atom["last_discard_reason"] == "bad_solution"
    assert atom["last_discard_note"] == "Generated fix was not acceptable."
