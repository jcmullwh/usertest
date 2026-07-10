from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from backlog_repo.plan_scope import build_plan_target_contract

from usertest_implement.implementation_provenance import (
    record_verified_implementation_head,
    validate_verified_implementation_head,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc.stdout.strip()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _fixture(tmp_path: Path) -> tuple[Path, Path, str, str]:
    repo = tmp_path / "repo"
    source = repo / "src" / "core.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.name", "Verified Head Test")
    _git(repo, "config", "user.email", "verified@example.test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    revision = _git(repo, "rev-parse", "HEAD")
    contract = build_plan_target_contract(
        {
            "case_id": "case:head",
            "problem_id": "problem:head",
            "selected_option_id": "option:head",
            "repo_revision": revision,
            "change_targets": [
                {
                    "action": "modify",
                    "path": "src/core.py",
                    "symbols": ["core.run"],
                    "change": "Apply the researched behavior at core.run.",
                }
            ],
        },
        repo_root=repo,
    )
    source.write_text("def run():\n    return 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "implementation")
    head = _git(repo, "rev-parse", "HEAD")
    run_dir = tmp_path / "run"
    _write_json(
        run_dir / "ticket_ref.json",
        {
            "schema_version": 2,
            "ticket_provenance": {"target_contract": contract},
        },
    )
    _write_json(run_dir / "verification.json", {"schema_version": 1, "passed": True})
    _write_json(run_dir / "target_ref.json", {"commit_sha": revision})
    _write_json(run_dir / "workspace_ref.json", {"workspace_dir": str(repo)})
    _write_json(
        run_dir / "git_ref.json",
        {
            "commit_performed": True,
            "base_commit": revision,
            "head_commit": head,
        },
    )
    return run_dir, repo, revision, head


def test_verified_head_receipt_binds_passing_tests_to_committed_head(tmp_path: Path) -> None:
    run_dir, _repo, revision, head = _fixture(tmp_path)

    receipt = record_verified_implementation_head(
        run_dir=run_dir,
        require_exact_base=True,
    )

    assert receipt["repo_revision"] == revision
    assert receipt["verified_implementation_head"] == head
    assert validate_verified_implementation_head(run_dir=run_dir) == receipt


def test_verified_head_receipt_rejects_tampered_verification(tmp_path: Path) -> None:
    run_dir, _repo, _revision, _head = _fixture(tmp_path)
    record_verified_implementation_head(run_dir=run_dir, require_exact_base=True)
    _write_json(run_dir / "verification.json", {"schema_version": 1, "passed": False})

    with pytest.raises(ValueError, match="verification_sha256_mismatch"):
        validate_verified_implementation_head(run_dir=run_dir)


def test_initial_implementation_receipt_requires_exact_researched_base(
    tmp_path: Path,
) -> None:
    run_dir, _repo, _revision, head = _fixture(tmp_path)
    _write_json(run_dir / "target_ref.json", {"commit_sha": head})
    git_ref = json.loads((run_dir / "git_ref.json").read_text(encoding="utf-8"))
    git_ref["base_commit"] = head
    _write_json(run_dir / "git_ref.json", git_ref)

    with pytest.raises(ValueError, match="target_revision_mismatch"):
        record_verified_implementation_head(run_dir=run_dir, require_exact_base=True)
