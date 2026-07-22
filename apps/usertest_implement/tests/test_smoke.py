from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from usertest_implement.cli import build_parser


def test_parser_smoke() -> None:
    parser = build_parser()
    args = parser.parse_args(["run", "--ticket-path", "C:\\tmp\\ticket.md", "--dry-run"])
    assert args.ticket_path == Path("C:\\tmp\\ticket.md")
    assert args.dry_run is True
    assert args.base_branch == "dev"
    assert args.exec_backend == "docker"
    assert args.exec_keep_container is False
    assert args.move_on_start is True
    assert args.move_on_commit is True
    assert args.draft_pr_on_ci_failure is True
    assert args.ci_timeout_seconds is None


def test_parser_base_branch_override() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["run", "--ticket-path", "C:\\tmp\\ticket.md", "--base-branch", "main", "--dry-run"]
    )
    assert args.base_branch == "main"


def test_parser_no_docker_overrides_default() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["run", "--ticket-path", "C:\\tmp\\ticket.md", "--dry-run", "--no-docker"]
    )
    assert args.exec_backend == "local"


def test_parser_accepts_pre_resolved_maintenance_image_metadata() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--ticket-path",
            "C:\\tmp\\ticket.md",
            "--dry-run",
            "--exec-maintenance-image-metadata",
            "C:\\tmp\\maintenance_image.json",
        ]
    )
    assert args.exec_maintenance_image_metadata_path == Path("C:\\tmp\\maintenance_image.json")


def test_parser_container_retention_is_an_explicit_opt_in() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--ticket-path",
            "C:\\tmp\\ticket.md",
            "--dry-run",
            "--exec-keep-container",
            "--no-move-on-start",
            "--no-move-on-commit",
            "--no-draft-pr-on-ci-failure",
        ]
    )
    assert args.exec_keep_container is True
    assert args.move_on_start is False
    assert args.move_on_commit is False
    assert args.draft_pr_on_ci_failure is False


def test_all_execution_parser_surfaces_default_to_discarding_containers() -> None:
    parser = build_parser()

    run_args = parser.parse_args(
        ["run", "--ticket-path", "C:\\tmp\\ticket.md", "--dry-run"]
    )
    review_args = parser.parse_args(
        ["review", "run", "--ticket-path", "C:\\tmp\\ticket.md", "--dry-run"]
    )
    resume_args = parser.parse_args(
        ["resume", "--run-dir", "C:\\tmp\\run", "--dry-run"]
    )

    assert run_args.exec_keep_container is False
    assert review_args.exec_keep_container is False
    assert resume_args.exec_keep_container is False


def test_help_smoke() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "usertest_implement.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "usertest-implement" in proc.stdout


def test_parser_maintenance_images_cleanup_defaults_to_config_dry_run_choice() -> None:
    parser = build_parser()
    args = parser.parse_args(["maintenance-images", "cleanup"])
    assert args.dry_run is None
    assert args.timeout_seconds is None


def test_parser_maintenance_images_cleanup_dry_run_override() -> None:
    parser = build_parser()
    args = parser.parse_args(["maintenance-images", "cleanup", "--dry-run"])
    assert args.dry_run is True


def test_parser_maintenance_images_list() -> None:
    parser = build_parser()
    args = parser.parse_args(["maintenance-images", "list", "--timeout-seconds", "9"])
    assert args.timeout_seconds == 9.0


def test_parser_accepts_model_free_existing_pr_adoption() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "handoff",
            "adopt-pr",
            "--owner-root",
            "C:\\owner",
            "--ticket-path",
            "C:\\owner\\.agents\\plans\\2 - ready\\ticket.md",
            "--source-run-dir",
            "U:\\runs\\source",
            "--runs-dir",
            "U:\\runs",
            "--pr-url",
            "https://github.com/example/project/pull/213",
            "--base-branch",
            "dev",
            "--remote-name",
            "github",
        ]
    )

    assert args.handoff_cmd == "adopt-pr"
    assert args.source_run_dir == Path("U:\\runs\\source")
    assert args.remote_name == "github"
    assert args.base_branch == "dev"
    assert not hasattr(args, "agent")
    assert not hasattr(args, "model")
