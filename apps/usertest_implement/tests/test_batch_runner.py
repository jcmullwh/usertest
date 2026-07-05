from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from usertest_implement.batch_failure import classify_run_outcome
from usertest_implement.batch_runner import (
    BacklogSource,
    BatchCandidate,
    _add_batch_resource_conflicts,
    _batch_subprocess_env,
    _collect_wave_candidates,
    _pick_launchable_candidate_index,
    _refresh_backlog,
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


def test_docker_batch_resource_conflict_serializes_ticket_runs(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    ticket_path = owner_root / ".agents" / "plans" / "2 - ready" / "ticket.md"
    queue = [
        _add_batch_resource_conflicts(
            BatchCandidate(
                source_name="src",
                export_path=tmp_path / "export.json",
                fingerprint="aaaaaaaaaaaaaaaa",
                severity="high",
                title="First",
                owner_root=owner_root,
                ticket_path=ticket_path,
                execution_domain="runner_core",
                execution_conflict_keys=("ticket:aaaaaaaaaaaaaaaa",),
            ),
            exec_backend="docker",
        ),
        _add_batch_resource_conflicts(
            BatchCandidate(
                source_name="src",
                export_path=tmp_path / "export.json",
                fingerprint="bbbbbbbbbbbbbbbb",
                severity="high",
                title="Second",
                owner_root=owner_root,
                ticket_path=ticket_path,
                execution_domain="docs",
                execution_conflict_keys=("ticket:bbbbbbbbbbbbbbbb",),
            ),
            exec_backend="docker",
        ),
    ]

    assert queue[0].execution_conflict_keys == (
        "ticket:aaaaaaaaaaaaaaaa",
        "batch_resource:docker",
    )
    assert _pick_launchable_candidate_index(
        queue,
        active_conflict_keys={"batch_resource:docker"},
    ) is None


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


def test_classify_run_outcome_marks_completed_failure_ci_as_ticket_regression(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "bootstrap_pip.log").write_text(
        "\n".join(
            [
                "$ docker exec ...",
                (
                    '    echo "Missing '
                    'GITLAB_PYPI_USERNAME/GITLAB_PYPI_PASSWORD '
                    'in container env." 1>&2'
                ),
                "exit_code=0",
            ]
        ),
        encoding="utf-8",
    )

    failure = classify_run_outcome(
        run_dir=run_dir,
        handoff_summary={
            "pr_created": True,
            "pr_url": "https://example.invalid/pr/2",
            "ci_status": "completed",
            "ci_conclusion": "failure",
            "ci_run_url": "https://example.invalid/run/2",
            "final_status": "success",
        },
    )

    assert failure["failure_class"] == "ticket_regression"
    assert failure["global_blocker"] is True


def test_classify_run_outcome_accepts_local_exercise_success(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "verification.json").write_text(
        json.dumps({"passed": True}),
        encoding="utf-8",
    )

    failure = classify_run_outcome(
        run_dir=run_dir,
        handoff_summary={
            "pr_created": False,
            "ci_required": False,
            "final_status": "success",
        },
    )

    assert failure["failure_class"] == "success"
    assert failure["global_blocker"] is False


def test_classify_run_outcome_rejects_missing_requested_pr(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "verification.json").write_text(
        json.dumps({"passed": True}),
        encoding="utf-8",
    )

    failure = classify_run_outcome(
        run_dir=run_dir,
        handoff_summary={
            "commit_requested": True,
            "commit_performed": False,
            "push_requested": True,
            "pushed": False,
            "pr_requested": True,
            "pr_created": False,
            "ci_required": False,
            "final_status": "success",
        },
    )

    assert failure["failure_class"] == "ticket_regression"
    assert failure["global_blocker"] is False
    assert "commit" in failure["summary"].lower()


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


def test_refresh_backlog_uses_normal_export_dedupe_flags(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[list[str]] = []

    def _capture(argv: list[str], **_: Any) -> None:
        calls.append(argv)

    monkeypatch.setattr("usertest_implement.batch_runner._run_logged_command", _capture)

    source = BacklogSource(
        name="usertest",
        runs_dir=tmp_path / "runs" / "usertest",
        target="usertest",
    )

    _refresh_backlog(
        repo_root=tmp_path,
        source=source,
        repo_input=str(tmp_path),
        backlog_python=tmp_path / "python.exe",
        agent="codex",
        model="gpt-5.5",
        batch_dir_path=tmp_path / "batch",
    )

    export_calls = [argv for argv in calls if "export-tickets" in argv]
    assert len(export_calls) == 1
    export_cmd = export_calls[0]
    assert "--stage" in export_cmd
    assert export_cmd[export_cmd.index("--stage") + 1] == "ready_for_ticket"
    assert "--include-actioned" not in export_cmd
    assert "--skip-plan-folder-dedupe" not in export_cmd


def test_collect_wave_candidates_prefers_ready_queue_over_refresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ready_dir = tmp_path / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True)
    ticket_path = ready_dir / "20260314_deadbeefdeadbeef_ticket.md"
    ticket_path.write_text(
        "\n".join(
            [
                "# Queue-first ticket",
                "",
                "- Fingerprint: `deadbeefdeadbeef`",
                "- Execution domain: `runner_core`",
                (
                    "- Execution conflict keys: "
                    "`execution_domain:runner_core`, "
                    "`subsystem:python_runtime`"
                ),
                "- Export kind: `implementation`",
                "- Stage: `ready_for_ticket`",
                "- Severity: `blocker`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    source = BacklogSource(
        name="usertest",
        runs_dir=tmp_path / "runs" / "usertest",
        target="usertest",
    )

    monkeypatch.setattr(
        "usertest_implement.batch_runner._refresh_backlog",
        lambda **_: (_ for _ in ()).throw(AssertionError("refresh should be skipped")),
    )

    candidates = _collect_wave_candidates(
        repo_root=tmp_path,
        repo_input=str(tmp_path),
        backlog_python=tmp_path / "python.exe",
        refresh_agent="codex",
        refresh_model="gpt-5.5",
        batch_dir_path=tmp_path / "batch",
        sources=[source],
        severities={"blocker", "high"},
        processed=set(),
        refresh_state={},
    )

    assert [candidate.fingerprint for candidate in candidates] == ["deadbeefdeadbeef"]
    assert candidates[0].ticket_path == ticket_path.resolve()
    assert candidates[0].execution_domain == "runner_core"
    assert candidates[0].execution_conflict_keys == (
        "execution_domain:runner_core",
        "subsystem:python_runtime",
    )


def test_collect_wave_candidates_skips_refresh_while_other_ready_work_exists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ready_dir = tmp_path / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True)
    (ready_dir / "20260314_feedfacefeedface_ticket.md").write_text(
        "\n".join(
            [
                "# Medium ticket",
                "",
                "- Fingerprint: `feedfacefeedface`",
                "- Export kind: `implementation`",
                "- Stage: `ready_for_ticket`",
                "- Severity: `medium`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    source = BacklogSource(
        name="usertest",
        runs_dir=tmp_path / "runs" / "usertest",
        target="usertest",
    )

    monkeypatch.setattr(
        "usertest_implement.batch_runner._refresh_backlog",
        lambda **_: (_ for _ in ()).throw(AssertionError("refresh should be skipped")),
    )

    candidates = _collect_wave_candidates(
        repo_root=tmp_path,
        repo_input=str(tmp_path),
        backlog_python=tmp_path / "python.exe",
        refresh_agent="codex",
        refresh_model="gpt-5.5",
        batch_dir_path=tmp_path / "batch",
        sources=[source],
        severities={"blocker", "high"},
        processed=set(),
        refresh_state={},
    )

    assert candidates == []


def test_collect_wave_candidates_uses_per_ticket_fallback_when_conflict_metadata_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ready_dir = tmp_path / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True)
    ticket_path = ready_dir / "20260314_facefeedfacefeed_ticket.md"
    ticket_path.write_text(
        "\n".join(
            [
                "# Missing conflict metadata",
                "",
                "- Fingerprint: `facefeedfacefeed`",
                "- Export kind: `implementation`",
                "- Stage: `ready_for_ticket`",
                "- Severity: `high`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    source = BacklogSource(
        name="usertest",
        runs_dir=tmp_path / "runs" / "usertest",
        target="usertest",
    )

    monkeypatch.setattr(
        "usertest_implement.batch_runner._refresh_backlog",
        lambda **_: (_ for _ in ()).throw(AssertionError("refresh should be skipped")),
    )

    candidates = _collect_wave_candidates(
        repo_root=tmp_path,
        repo_input=str(tmp_path),
        backlog_python=tmp_path / "python.exe",
        refresh_agent="codex",
        refresh_model="gpt-5.5",
        batch_dir_path=tmp_path / "batch",
        sources=[source],
        severities={"blocker", "high"},
        processed=set(),
        refresh_state={},
    )

    assert [candidate.fingerprint for candidate in candidates] == ["facefeedfacefeed"]
    assert candidates[0].execution_domain == "unknown"
    assert candidates[0].execution_conflict_keys == ("ticket:facefeedfacefeed",)
