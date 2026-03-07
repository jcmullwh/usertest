from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
                        "fingerprint": "aaaaaaaaaaaaaaaa",
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
                        "fingerprint": "bbbbbbbbbbbbbbbb",
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
    assert "ticket_id" not in payload["selected_ticket"]


def test_run_dry_run_rejects_ticket_id_selector(tmp_path: Path) -> None:
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
                        "fingerprint": "aaaaaaaaaaaaaaaa",
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
    assert proc.returncode != 0


def test_run_dry_run_requires_fingerprint_with_tickets_export(tmp_path: Path) -> None:
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
    (ready_dir / f"20260220_{impl_fp}_implementation.md").write_text(
        "# Impl\n\n"
        "- Export kind: `implementation`\n"
        "- Stage: `ready_for_ticket`\n"
        "- Fingerprint: `aaaaaaaaaaaaaaaa`\n",
        encoding="utf-8",
    )

    research_fp = "bbbbbbbbbbbbbbbb"
    (ready_dir / f"20260220_{research_fp}_research.md").write_text(
        "# Research\n\n"
        "- Export kind: `research`\n"
        "- Stage: `research_required`\n"
        "- Fingerprint: `bbbbbbbbbbbbbbbb`\n",
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
    assert payload["run_request"]["verification_reuse_mode"] == "auto"
    assert payload["run_request"]["exec_cache"] == "warm"
    assert payload["run_request"]["exec_docker_profile"] == "standard"
    assert payload["run_request"]["exec_maintenance_venv_cache"] is True


def test_run_dry_run_can_disable_maintenance_venv_cache(tmp_path: Path) -> None:
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
                        "fingerprint": "aaaaaaaaaaaaaaaa",
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
            "--fingerprint",
            "aaaaaaaaaaaaaaaa",
            "--no-maintenance-venv-cache",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["run_request"]["exec_maintenance_venv_cache"] is False


def test_run_dry_run_can_disable_verification_reuse(tmp_path: Path) -> None:
    export_path = tmp_path / "tickets_export.json"
    _write_json(
        export_path,
        {
            "schema_version": 1,
            "exports": [
                {
                    "fingerprint": "eeeeeeeeeeeeeeee",
                    "export_kind": "implementation",
                    "title": "Ticket E",
                    "labels": [],
                    "body_markdown": "# E\n",
                    "source_ticket": {
                        "fingerprint": "eeeeeeeeeeeeeeee",
                        "stage": "ready_for_ticket",
                        "severity": "low",
                    },
                    "owner_repo": {
                        "root": str(tmp_path),
                        "repo_input": str(tmp_path),
                        "idea_path": str(tmp_path / "e.md"),
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
            "--fingerprint",
            "eeeeeeeeeeeeeeee",
            "--verify-reuse",
            "off",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["run_request"]["verification_reuse_mode"] == "off"


def test_run_dry_run_defaults_to_maintenance_profile_for_same_repo_targets() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    export_path = repo_root / "runs" / "_tmp_ticket_selection_same_repo.json"
    try:
        _write_json(
            export_path,
            {
                "schema_version": 1,
                "exports": [
                    {
                        "fingerprint": "cccccccccccccccc",
                        "export_kind": "implementation",
                        "title": "Ticket C",
                        "labels": [],
                        "body_markdown": "# C\n",
                        "source_ticket": {
                            "fingerprint": "cccccccccccccccc",
                            "stage": "ready_for_ticket",
                            "severity": "low",
                        },
                        "owner_repo": {
                            "root": str(repo_root),
                            "repo_input": str(repo_root),
                            "idea_path": str(
                                repo_root / ".agents" / "plans" / "2 - ready" / "c.md"
                            ),
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
                "--commit",
                "--tickets-export",
                str(export_path),
                "--fingerprint",
                "cccccccccccccccc",
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(repo_root),
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout
        payload = json.loads(proc.stdout)
        assert payload["run_request"]["exec_docker_profile"] == "maintenance"
        assert payload["run_request"]["exec_docker_profile_eligible"] is True
        assert payload["run_request"]["verification_reuse_mode"] == "auto"
        assert payload["run_request"]["verification_commands"][0].startswith(
            "bash ./scripts/smoke.sh --skip-install --use-pythonpath"
        )
    finally:
        export_path.unlink(missing_ok=True)


def test_run_dry_run_rejects_maintenance_profile_for_external_target(tmp_path: Path) -> None:
    export_path = tmp_path / "tickets_export.json"
    _write_json(
        export_path,
        {
            "schema_version": 1,
            "exports": [
                {
                    "fingerprint": "dddddddddddddddd",
                    "export_kind": "implementation",
                    "title": "Ticket D",
                    "labels": [],
                    "body_markdown": "# D\n",
                    "source_ticket": {
                        "fingerprint": "dddddddddddddddd",
                        "stage": "ready_for_ticket",
                        "severity": "low",
                    },
                    "owner_repo": {
                        "root": str(tmp_path),
                        "repo_input": str(tmp_path),
                        "idea_path": str(tmp_path / "d.md"),
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
            "--fingerprint",
            "dddddddddddddddd",
            "--exec-docker-profile",
            "maintenance",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "same-repo maintenance targets" in (proc.stderr + proc.stdout)


def test_tickets_run_next_dry_run_ignores_actioned_fingerprints(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    ready_dir = owner_root / ".agents" / "plans" / "2 - ready"
    complete_dir = owner_root / ".agents" / "plans" / "5 - complete"
    ready_dir.mkdir(parents=True)
    complete_dir.mkdir(parents=True)

    # Fingerprint has both queued + actioned copies -> merged status is actioned.
    stale_fp = "aaaaaaaaaaaaaaaa"
    name = f"20260220_{stale_fp}_stale.md"
    queued_text = (
        "# Stale queued copy\n\n"
        "- Export kind: `implementation`\n"
        "- Stage: `ready_for_ticket`\n"
        "- Fingerprint: `aaaaaaaaaaaaaaaa`\n"
    )
    (ready_dir / name).write_text(
        queued_text,
        encoding="utf-8",
    )
    actioned_text = (
        "# Actioned copy\n\n"
        "- Export kind: `implementation`\n"
        "- Stage: `ready_for_ticket`\n"
        "- Fingerprint: `aaaaaaaaaaaaaaaa`\n"
    )
    (complete_dir / name).write_text(
        actioned_text,
        encoding="utf-8",
    )

    good_fp = "bbbbbbbbbbbbbbbb"
    (ready_dir / f"20260220_{good_fp}_ok.md").write_text(
        "# Next\n\n"
        "- Export kind: `implementation`\n"
        "- Stage: `ready_for_ticket`\n"
        "- Fingerprint: `bbbbbbbbbbbbbbbb`\n",
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


def test_run_dry_run_rejects_non_stage6_ticket(tmp_path: Path) -> None:
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
                        "fingerprint": "aaaaaaaaaaaaaaaa",
                        "stage": "triage",
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
            "--fingerprint",
            "aaaaaaaaaaaaaaaa",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "ready_for_ticket" in (proc.stderr + proc.stdout)


def test_tickets_run_next_dry_run_skips_research_only_queue(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    ready_dir = owner_root / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True)

    research_fp = "bbbbbbbbbbbbbbbb"
    (ready_dir / f"20260220_{research_fp}_research.md").write_text(
        "# Research\n\n"
        "- Export kind: `research`\n"
        "- Stage: `research_required`\n"
        "- Fingerprint: `bbbbbbbbbbbbbbbb`\n",
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
    assert "No tickets found." in proc.stdout


def test_run_dry_run_rejects_non_stage6_ticket_path(tmp_path: Path) -> None:
    ticket_path = tmp_path / "ticket.md"
    ticket_path.write_text(
        "# Ticket\n\n"
        "- Export kind: `implementation`\n"
        "- Stage: `triage`\n"
        "- Fingerprint: `aaaaaaaaaaaaaaaa`\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "usertest_implement.cli",
            "run",
            "--dry-run",
            "--ticket-path",
            str(ticket_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "ready_for_ticket" in (proc.stderr + proc.stdout)
