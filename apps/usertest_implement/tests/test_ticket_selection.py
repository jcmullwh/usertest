from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_run_dry_run_selects_by_fingerprint(tmp_path: Path) -> None:
    export_path = tmp_path / "tickets_export.json"
    _write_json(
        export_path,
        {
            "schema_version": 1,
            "exports": [
                {
                    "fingerprint": "aaaaaaaaaaaaaaaa",
                    "export_kind": "implementation",
                    "title": "Ticket A",
                    "labels": [],
                    "body_markdown": "# A\n",
                    "source_ticket": {
                        "ticket_id": "BLG-001",
                        "stage": "ready_for_ticket",
                        "severity": "low",
                    },
                    "owner_repo": {
                        "root": str(tmp_path),
                        "repo_input": str(tmp_path),
                        "idea_path": str(tmp_path / "a.md"),
                    },
                },
                {
                    "fingerprint": "bbbbbbbbbbbbbbbb",
                    "export_kind": "implementation",
                    "title": "Ticket B",
                    "labels": [],
                    "body_markdown": "# B\n",
                    "source_ticket": {
                        "ticket_id": "BLG-002",
                        "stage": "ready_for_ticket",
                        "severity": "low",
                    },
                    "owner_repo": {
                        "root": str(tmp_path),
                        "repo_input": str(tmp_path),
                        "idea_path": str(tmp_path / "b.md"),
                    },
                },
            ],
        },
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "usertest_implement.cli",
            "run",
            "--dry-run",
            "--tickets-export",
            str(export_path),
            "--fingerprint",
            "bbbbbbbbbbbbbbbb",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["selected_ticket"]["fingerprint"] == "bbbbbbbbbbbbbbbb"
    assert payload["selected_ticket"]["ticket_id"] == "BLG-002"


def test_run_dry_run_selects_by_ticket_id(tmp_path: Path) -> None:
    export_path = tmp_path / "tickets_export.json"
    _write_json(
        export_path,
        {
            "schema_version": 1,
            "exports": [
                {
                    "fingerprint": "aaaaaaaaaaaaaaaa",
                    "export_kind": "implementation",
                    "title": "Ticket A",
                    "labels": [],
                    "body_markdown": "# A\n",
                    "source_ticket": {
                        "ticket_id": "BLG-001",
                        "stage": "ready_for_ticket",
                        "severity": "low",
                    },
                    "owner_repo": {
                        "root": str(tmp_path),
                        "repo_input": str(tmp_path),
                        "idea_path": str(tmp_path / "a.md"),
                    },
                }
            ],
        },
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "usertest_implement.cli",
            "run",
            "--dry-run",
            "--tickets-export",
            str(export_path),
            "--ticket-id",
            "BLG-001",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["selected_ticket"]["fingerprint"] == "aaaaaaaaaaaaaaaa"
    assert payload["selected_ticket"]["ticket_id"] == "BLG-001"


def test_run_dry_run_requires_exactly_one_selector(tmp_path: Path) -> None:
    export_path = tmp_path / "tickets_export.json"
    _write_json(export_path, {"schema_version": 1, "exports": []})

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "usertest_implement.cli",
            "run",
            "--dry-run",
            "--tickets-export",
            str(export_path),
            "--fingerprint",
            "aaaaaaaaaaaaaaaa",
            "--ticket-id",
            "BLG-001",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0


def test_tickets_run_next_dry_run_defaults_to_implementation_only(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    ready_dir = owner_root / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True)

    impl_fp = "aaaaaaaaaaaaaaaa"
    (ready_dir / f"20260220_BLG-001_{impl_fp}_implementation.md").write_text(
        "# Impl\n\n- Export kind: `implementation`\n- Fingerprint: `aaaaaaaaaaaaaaaa`\n",
        encoding="utf-8",
    )

    research_fp = "bbbbbbbbbbbbbbbb"
    (ready_dir / f"20260220_BLG-002_{research_fp}_research.md").write_text(
        "# Research\n\n- Export kind: `research`\n- Fingerprint: `bbbbbbbbbbbbbbbb`\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "usertest_implement.cli",
            "tickets",
            "run-next",
            "--owner-root",
            str(owner_root),
            "--no-refresh-backlog",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["selected_ticket"]["fingerprint"] == impl_fp
    assert payload["run_request"]["verification_commands"]


def test_tickets_run_next_dry_run_ignores_actioned_fingerprints(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    ready_dir = owner_root / ".agents" / "plans" / "2 - ready"
    complete_dir = owner_root / ".agents" / "plans" / "5 - complete"
    ready_dir.mkdir(parents=True)
    complete_dir.mkdir(parents=True)

    # Fingerprint has both queued + actioned copies -> merged status is actioned.
    stale_fp = "aaaaaaaaaaaaaaaa"
    name = f"20260220_BLG-001_{stale_fp}_stale.md"
    (ready_dir / name).write_text(
        "# Stale queued copy\n\n- Export kind: `implementation`\n- Fingerprint: `aaaaaaaaaaaaaaaa`\n",
        encoding="utf-8",
    )
    (complete_dir / name).write_text(
        "# Actioned copy\n\n- Export kind: `implementation`\n- Fingerprint: `aaaaaaaaaaaaaaaa`\n",
        encoding="utf-8",
    )

    good_fp = "bbbbbbbbbbbbbbbb"
    (ready_dir / f"20260220_BLG-002_{good_fp}_ok.md").write_text(
        "# Next\n\n- Export kind: `implementation`\n- Fingerprint: `bbbbbbbbbbbbbbbb`\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "usertest_implement.cli",
            "tickets",
            "run-next",
            "--owner-root",
            str(owner_root),
            "--no-refresh-backlog",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["selected_ticket"]["fingerprint"] == good_fp
