from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

import yaml

from usertest_implement.selection import (
    _compose_ticket_blob,
    _resolve_default_branch_name,
    _should_move_ticket_to_review,
)
from usertest_implement.shared import SelectedTicket


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_yaml(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(obj, sort_keys=False), encoding="utf-8")


def test_repository_default_settings_pin_long_verification_timeout() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    settings_path = repo_root / "configs" / "usertest_implement_settings.yaml"
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))

    timeout_seconds = settings["profiles"]["default"]["run_common"][
        "verification_timeout_seconds"
    ]

    assert timeout_seconds == 10800


def test_repository_default_settings_discard_execution_containers() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    settings_path = repo_root / "configs" / "usertest_implement_settings.yaml"
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))

    keep_container = settings["profiles"]["default"]["run_common"][
        "exec_keep_container"
    ]

    assert keep_container is False


def test_initial_ticket_prompt_projects_large_research_history_without_mutation(
    tmp_path: Path,
) -> None:
    proof = {
        "case_id": "case:root-cause",
        "experiments": [{"result": "causal intervention reproduced the symptom"}],
        "root_cause_hypotheses": [{"mechanism": "verification errors entered schema channel"}],
        "inspected_files": ["packages/runner_core/src/runner_core/runner.py"],
        "evidence_assignment": {
            "expected_atom_ids": ["atom:one"],
            "atom_receipts": [
                {
                    "atom_id": "atom:one",
                    "atom_sha256": "a" * 64,
                    "atom_snapshot": {"raw": "z" * 120_000},
                }
            ],
            "origin_attachment_evidence": {
                "manifest_file": "origin_evidence/manifest.json",
                "run_context": {"raw": "q" * 120_000},
            },
        },
        "research_attempts": [{"transcript": "x" * 120_000}],
        "evidence_verification": {"raw": "y" * 120_000},
    }
    ticket_markdown = (
        "# Ticket\n\n"
        "### Full verified research proof\n\n"
        f"```json\n{json.dumps(proof, indent=2)}\n```\n\n"
        "## Implementation plan\n\nChange the verified root mechanism.\n"
    )
    ticket_path = tmp_path / "ticket.md"
    ticket_path.write_text(ticket_markdown, encoding="utf-8")
    selected = SelectedTicket(
        fingerprint="1234567890abcdef",
        title="Ticket",
        export_kind="implementation",
        stage="ready_for_ticket",
        owner_root=tmp_path,
        idea_path=ticket_path,
        ticket_markdown=ticket_markdown,
        tickets_export_path=None,
        export_index=None,
    )

    prompt = _compose_ticket_blob(selected)

    assert len(prompt) < 20_000
    assert "causal intervention reproduced the symptom" in prompt
    assert "verification errors entered schema channel" in prompt
    assert "packages/runner_core/src/runner_core/runner.py" in prompt
    assert "atom:one" in prompt
    assert "origin_evidence/manifest.json" in prompt
    assert "atom_snapshot_receipt" in prompt
    assert "run_context_receipt" in prompt
    assert "x" * 1000 not in prompt
    assert "z" * 1000 not in prompt
    assert "q" * 1000 not in prompt
    assert "full_proof_sha256" in prompt
    assert json.dumps(str(ticket_path))[1:-1] in prompt
    assert ticket_path.read_text(encoding="utf-8") == ticket_markdown


def test_resolve_default_branch_name_uses_rerun_suffix_when_remote_branch_exists(
    monkeypatch,
) -> None:
    selected = SelectedTicket(
        fingerprint="682b47583ab4e1e1",
        title="Ticket",
        export_kind="implementation",
        stage="ready_for_ticket",
        owner_root=None,
        idea_path=None,
        ticket_markdown="# Ticket\n",
        tickets_export_path=None,
        export_index=None,
    )

    seen: list[str] = []

    def _fake_remote_branch_exists(*, remote_url: str, branch: str) -> bool:
        seen.append(branch)
        return branch in {"backlog/682b47583ab4", "backlog/682b47583ab4-rerun-1"}

    monkeypatch.setattr(
        "usertest_implement.selection._resolve_remote_url_for_push",
        lambda **_: "https://github.com/jcmullwh/usertest.git",
    )
    monkeypatch.setattr(
        "usertest_implement.selection._remote_branch_exists",
        _fake_remote_branch_exists,
    )

    branch = _resolve_default_branch_name(
        selected=selected,
        remote_name="origin",
        remote_url=None,
        candidate_repo_dirs=[],
        wants_remote_handoff=True,
    )

    assert branch == "backlog/682b47583ab4-rerun-2"
    assert seen == [
        "backlog/682b47583ab4",
        "backlog/682b47583ab4-rerun-1",
        "backlog/682b47583ab4-rerun-2",
    ]


def test_should_move_ticket_to_review_requires_reviewable_handoff_state() -> None:
    assert _should_move_ticket_to_review(
        commit_performed=True,
        push_requested=False,
        pr_requested=False,
        push_ref=None,
        pr_ref=None,
    )
    assert not _should_move_ticket_to_review(
        commit_performed=True,
        push_requested=True,
        pr_requested=False,
        push_ref={"pushed": False},
        pr_ref=None,
    )
    assert _should_move_ticket_to_review(
        commit_performed=True,
        push_requested=True,
        pr_requested=False,
        push_ref={"pushed": True},
        pr_ref=None,
    )
    assert not _should_move_ticket_to_review(
        commit_performed=True,
        push_requested=True,
        pr_requested=True,
        push_ref={"pushed": True},
        pr_ref={"created": False},
    )
    assert _should_move_ticket_to_review(
        commit_performed=True,
        push_requested=True,
        pr_requested=True,
        push_ref={"pushed": True},
        pr_ref={"created": True},
    )


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
    assert "Provide --fingerprint with --tickets-export." in (proc.stderr + proc.stdout)


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


def test_run_dry_run_honors_verify_timeout_seconds(tmp_path: Path) -> None:
    export_path = tmp_path / "tickets_export.json"
    _write_json(
        export_path,
        {
            "schema_version": 1,
            "exports": [
                {
                    "fingerprint": "1212121212121212",
                    "export_kind": "implementation",
                    "title": "Ticket Timeout",
                    "labels": [],
                    "body_markdown": "# Timeout\n",
                    "source_ticket": {
                        "fingerprint": "1212121212121212",
                        "stage": "ready_for_ticket",
                        "severity": "low",
                    },
                    "owner_repo": {
                        "root": str(tmp_path),
                        "repo_input": str(tmp_path),
                        "idea_path": str(tmp_path / "timeout.md"),
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
            "1212121212121212",
            "--verify-timeout-seconds",
            "1234",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["run_request"]["verification_timeout_seconds"] == 1234.0


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
        assert payload["run_request"]["commit"] is True
        assert payload["run_request"]["push"] is True
        assert payload["run_request"]["pr"] is True
        assert payload["run_request"]["persona_id"] == "thoughtful_maintainer"
        assert payload["run_request"]["mission_id"] == "implement_maintenance_backlog_ticket_v1"
        assert payload["run_request"]["verification_profile"] == "default_handoff"
        assert payload["run_request"]["verification_reuse_mode"] == "auto"
        assert payload["run_request"]["verification_commands"][0].startswith(
            "bash ./scripts/smoke.sh --skip-install --use-pythonpath"
        )
    finally:
        export_path.unlink(missing_ok=True)


def test_run_dry_run_applies_settings_profile_and_reports_effective_handoff_flags(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_root), check=True, capture_output=True, text=True)
    _write_yaml(repo_root / "configs" / "agents.yaml", {"agents": {}})
    _write_yaml(repo_root / "configs" / "policies.yaml", {"policies": {}})
    _write_yaml(
        repo_root / "configs" / "usertest_implement_settings.yaml",
        {
            "version": 1,
            "default_profile": "default",
            "profiles": {
                "default": {
                    "run_common": {
                        "exec_backend": "docker",
                        "verification_profile": "default_handoff",
                        "verify_reuse": "auto",
                    },
                    "run": {},
                    "tickets_run_next": {},
                },
                "custom_profile": {
                    "run_common": {
                        "exec_backend": "docker",
                        "exec_docker_profile": "maintenance",
                        "persona_id": "thoughtful_maintainer",
                        "mission_id": "implement_maintenance_backlog_ticket_v1",
                        "verification_profile": "default_handoff",
                        "commit": True,
                        "push": False,
                        "pr": False,
                    },
                    "run": {},
                    "tickets_run_next": {},
                },
            },
        },
    )
    export_path = repo_root / "tickets_export.json"
    _write_json(
        export_path,
        {
            "schema_version": 1,
            "exports": [
                {
                    "fingerprint": "f0f0f0f0f0f0f0f0",
                    "export_kind": "implementation",
                    "title": "Ticket F",
                    "labels": [],
                    "body_markdown": "# F\n",
                    "source_ticket": {
                        "fingerprint": "f0f0f0f0f0f0f0f0",
                        "stage": "ready_for_ticket",
                        "severity": "low",
                    },
                    "owner_repo": {
                        "root": str(repo_root),
                        "repo_input": str(repo_root),
                        "idea_path": str(repo_root / ".agents" / "plans" / "2 - ready" / "f.md"),
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
            "--repo-root",
            str(repo_root),
            "run",
            "--dry-run",
            "--settings-profile",
            "custom_profile",
            "--tickets-export",
            str(export_path),
            "--fingerprint",
            "f0f0f0f0f0f0f0f0",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["settings"]["profile"] == "custom_profile"
    assert PurePosixPath(payload["settings"]["config_path"].replace("\\", "/")).parts[-2:] == (
        "configs",
        "usertest_implement_settings.yaml",
    )
    assert payload["run_request"]["exec_docker_profile"] == "maintenance"
    assert payload["run_request"]["persona_id"] == "thoughtful_maintainer"
    assert payload["run_request"]["mission_id"] == "implement_maintenance_backlog_ticket_v1"
    assert payload["run_request"]["verification_profile"] == "default_handoff"
    assert payload["run_request"]["verification_commands"]
    assert payload["run_request"]["commit"] is True
    assert payload["run_request"]["push"] is False
    assert payload["run_request"]["pr"] is False


def test_run_dry_run_uses_maintenance_persona_and_mission_without_settings_file(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_root), check=True, capture_output=True, text=True)
    _write_yaml(repo_root / "configs" / "agents.yaml", {"agents": {}})
    _write_yaml(repo_root / "configs" / "policies.yaml", {"policies": {}})
    export_path = repo_root / "tickets_export.json"
    _write_json(
        export_path,
        {
            "schema_version": 1,
            "exports": [
                {
                    "fingerprint": "efefefefefefefef",
                    "export_kind": "implementation",
                    "title": "Ticket Persona",
                    "labels": [],
                    "body_markdown": "# Persona\n",
                    "source_ticket": {
                        "fingerprint": "efefefefefefefef",
                        "stage": "ready_for_ticket",
                        "severity": "low",
                    },
                    "owner_repo": {
                        "root": str(repo_root),
                        "repo_input": str(repo_root),
                        "idea_path": str(repo_root / ".agents" / "plans" / "2 - ready" / "p.md"),
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
            "--repo-root",
            str(repo_root),
            "run",
            "--dry-run",
            "--tickets-export",
            str(export_path),
            "--fingerprint",
            "efefefefefefefef",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["settings"]["config_path"] is None
    assert payload["settings"]["profile"] is None
    assert payload["run_request"]["persona_id"] == "thoughtful_maintainer"
    assert payload["run_request"]["mission_id"] == "implement_maintenance_backlog_ticket_v1"


def test_run_dry_run_explicit_flags_override_settings_profile(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_root), check=True, capture_output=True, text=True)
    _write_yaml(repo_root / "configs" / "agents.yaml", {"agents": {}})
    _write_yaml(repo_root / "configs" / "policies.yaml", {"policies": {}})
    _write_yaml(
        repo_root / "configs" / "usertest_implement_settings.yaml",
        {
            "version": 1,
            "default_profile": "custom_profile",
            "profiles": {
                "custom_profile": {
                    "run_common": {
                        "exec_backend": "docker",
                        "verification_profile": "default_handoff",
                        "commit": True,
                        "push": False,
                        "pr": False,
                    },
                    "run": {},
                    "tickets_run_next": {},
                }
            },
        },
    )
    export_path = repo_root / "tickets_export.json"
    _write_json(
        export_path,
        {
            "schema_version": 1,
            "exports": [
                {
                    "fingerprint": "abababababababab",
                    "export_kind": "implementation",
                    "title": "Ticket Override",
                    "labels": [],
                    "body_markdown": "# Override\n",
                    "source_ticket": {
                        "fingerprint": "abababababababab",
                        "stage": "ready_for_ticket",
                        "severity": "low",
                    },
                    "owner_repo": {
                        "root": str(repo_root),
                        "repo_input": str(repo_root),
                        "idea_path": str(
                            repo_root / ".agents" / "plans" / "2 - ready" / "override.md"
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
            "--repo-root",
            str(repo_root),
            "run",
            "--dry-run",
            "--tickets-export",
            str(export_path),
            "--fingerprint",
            "abababababababab",
            "--push",
            "--pr",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    assert payload["settings"]["profile"] == "custom_profile"
    assert payload["run_request"]["commit"] is True
    assert payload["run_request"]["push"] is True
    assert payload["run_request"]["pr"] is True


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
