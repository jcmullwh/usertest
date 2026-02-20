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

