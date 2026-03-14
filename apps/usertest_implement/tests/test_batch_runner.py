from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from usertest_implement.batch_failure import classify_run_outcome
from usertest_implement.batch_runner import (
    BatchCandidate,
    _batch_subprocess_env,
    _pick_launchable_candidate_index,
    _write_stream,
)


def test_pick_launchable_candidate_index_respects_conflict_keys(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    ticket_path = owner_root / ".agents" / "plans" / "2 - ready" / "ticket.md"
    queue = [
        BatchCandidate(
            source_name="src",
            export_path=tmp_path / "export.json",
            fingerprint="aaaaaaaaaaaaaaaa",
            severity="high",
            title="Broker fix",
            owner_root=owner_root,
            ticket_path=ticket_path,
            execution_domain="runner_core",
            execution_conflict_keys=(
                "execution_domain:runner_core",
                "subsystem:verification_broker",
            ),
        ),
        BatchCandidate(
            source_name="src",
            export_path=tmp_path / "export.json",
            fingerprint="bbbbbbbbbbbbbbbb",
            severity="high",
            title="Docs fix",
            owner_root=owner_root,
            ticket_path=ticket_path,
            execution_domain="docs_onboarding",
            execution_conflict_keys=("execution_domain:docs_onboarding",),
        ),
    ]

    index = _pick_launchable_candidate_index(
        queue,
        active_conflict_keys={"execution_domain:runner_core"},
    )

    assert index == 1


def test_classify_run_outcome_detects_registry_json_failure(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "bootstrap_pip.log").write_text(
        "\n".join(
            [
                "USERTEST_GITLAB_INDEX_URL=https://gitlab.com/api/v4/projects/123/packages/pypi/simple",
                "json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
            ]
        ),
        encoding="utf-8",
    )

    failure = classify_run_outcome(run_dir=run_dir, handoff_summary=None)

    assert failure["failure_class"] == "registry_or_auth"
    assert failure["global_blocker"] is True


def test_classify_run_outcome_detects_missing_broker_response(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    requests_dir = run_dir / "verification_broker" / "attempt1" / "requests"
    requests_dir.mkdir(parents=True)
    (requests_dir / "req_01.json").write_text(
        json.dumps({"request_id": "req_01"}),
        encoding="utf-8",
    )

    failure = classify_run_outcome(run_dir=run_dir, handoff_summary=None)

    assert failure["failure_class"] == "verification_control_plane"
    assert failure["global_blocker"] is True


def test_classify_run_outcome_marks_red_pr_as_ticket_regression(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)

    failure = classify_run_outcome(
        run_dir=run_dir,
        handoff_summary={
            "pr_created": True,
            "pr_url": "https://example.invalid/pr/1",
            "ci_status": "failure",
            "ci_run_url": "https://example.invalid/run/1",
            "final_status": "failure",
        },
    )

    assert failure["failure_class"] == "ticket_regression"
    assert failure["global_blocker"] is True


def test_batch_subprocess_env_includes_repo_src_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "apps" / "usertest_backlog" / "src").mkdir(parents=True)
    (tmp_path / "packages" / "backlog_core" / "src").mkdir(parents=True)
    (tmp_path / "packages" / "run_artifacts" / "src").mkdir(parents=True)
    monkeypatch.setenv("PYTHONPATH", f"existing_one{os.pathsep}existing_two")

    env = _batch_subprocess_env(tmp_path)

    assert env["PYTHONPATH"] == os.pathsep.join(
        [
            "apps/usertest_backlog/src",
            "packages/backlog_core/src",
            "packages/run_artifacts/src",
            "existing_one",
            "existing_two",
        ]
    )


def test_write_stream_ignores_oserror_from_closed_pipe() -> None:
    class BrokenStream:
        encoding = "utf-8"

        def write(self, text: str) -> Any:
            raise OSError(22, "Invalid argument")

        def flush(self) -> None:
            raise AssertionError("flush should not be called after a write failure")

    _write_stream(BrokenStream(), "hello")
