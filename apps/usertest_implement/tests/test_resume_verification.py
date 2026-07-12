from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from runner_core import RunnerConfig

import usertest_implement.commands.resume as resume_commands
from usertest_implement.cli import build_parser


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _make_resume_run(tmp_path: Path, *, workspace_exists: bool = True) -> tuple[Path, Path, Path]:
    run_dir = tmp_path / "original_run"
    workspace = tmp_path / "workspace"
    if workspace_exists:
        workspace.mkdir(parents=True)
    ticket_path = tmp_path / "ticket.md"
    ticket_path.write_text("# Resume Ticket\n", encoding="utf-8")
    _write_json(
        run_dir / "verification.json",
        {
            "schema_version": 1,
            "passed": False,
            "terminal_reason": "failed",
            "failure_reason": "verification_failed",
            "commands": [
                {
                    "index": 1,
                    "command": "python -m pytest tests/test_resume.py",
                    "exit_code": 1,
                    "stdout_tail": "assert 1 == 2",
                }
            ],
        },
    )
    _write_json(
        run_dir / "verification_reuse.json",
        {
            "schema_version": 1,
            "mode": "auto",
            "selected_source": "post_agent_rerun",
            "selected_artifacts_dir": str(run_dir / "verify_artifacts"),
        },
    )
    _write_json(
        run_dir / "agent_attempts.json",
        {
            "attempts": [
                {
                    "attempt": 1,
                    "followup_reason": "verification_failed",
                    "last_message_path": "agent_last_message.txt",
                }
            ]
        },
    )
    (run_dir / "agent_last_message.txt").write_text('{"status":"partial"}\n', encoding="utf-8")
    _write_json(run_dir / "report.json", {"summary": "prior report"})
    _write_json(run_dir / "report.schema.json", {"type": "object"})
    _write_json(run_dir / "workspace_ref.json", {"workspace_dir": str(workspace)})
    _write_json(
        run_dir / "ticket_ref.json",
        {
            "schema_version": 1,
            "fingerprint": "abc123abc123abcd",
            "title": "Resume Ticket",
            "export_kind": "implementation",
            "tickets_export_path": None,
            "export_index": None,
            "owner_repo": {"root": str(tmp_path), "idea_path": str(ticket_path)},
        },
    )
    _write_json(
        run_dir / "target_ref.json",
        {"repo_input": str(tmp_path / "remote.git"), "agent": "codex"},
    )
    (run_dir / "raw_events.jsonl").write_text(
        json.dumps(
            {
                "type": "thread.started",
                "thread_id": "019f5000-0000-7000-8000-000000000002",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_json(
        run_dir / "ticket_resume_state.json",
        {
            "schema_version": 1,
            "kind": "ticket_resume_state",
            "ticket": {
                "fingerprint": "abc123abc123abcd",
                "path": str(ticket_path),
                "title": "Resume Ticket",
                "export_kind": "implementation",
            },
            "owner_root": str(tmp_path),
            "run_dir": str(run_dir),
            "workspace_path": str(workspace),
            "branch": "backlog/abc123abc123",
            "lifecycle_state": "verification_failed_resume_ready",
            "blocking_reason": "Verification failed: python -m pytest tests/test_resume.py",
            "source_evidence_paths": {},
        },
    )
    _write_json(
        run_dir / "verification_config.json",
        {"commands": ["python -m pytest tests/test_resume.py"], "timeout_seconds": 123.0},
    )
    return run_dir, workspace, ticket_path


def test_resume_dry_run_builds_focused_prompt_from_structured_artifacts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, workspace, _ = _make_resume_run(tmp_path)
    parser = build_parser()
    args = parser.parse_args(
        [
            "--repo-root",
            str(tmp_path),
            "resume",
            "--run-dir",
            str(run_dir),
            "--no-docker",
            "--dry-run",
        ]
    )

    assert resume_commands._cmd_resume(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["workspace_strategy"] == "same_workspace"
    assert payload["run_request"]["resume_workspace_dir"] == str(workspace)
    assert payload["run_request"]["verification_commands"] == [
        "python -m pytest tests/test_resume.py"
    ]
    assert (
        payload["run_request"]["codex_resume_session_id"]
        == "019f5000-0000-7000-8000-000000000002"
    )
    assert payload["implementation_author_continuity"]["status"] == "exact_author_session"
    prompt = payload["prompt"]
    assert "Do not restart the original full ticket prompt from scratch" in prompt
    assert "verification.json" in prompt
    assert "verification_reuse.json" in prompt
    assert "agent_attempts.json" in prompt
    assert "Prior report output" in prompt
    assert "python -m pytest tests/test_resume.py" in prompt


def test_resume_uses_same_workspace_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, workspace, _ = _make_resume_run(tmp_path)
    resumed_run = tmp_path / "resumed_run"
    seen = {}

    def fake_run_once(_cfg: object, request: object) -> object:
        seen["request"] = request
        _write_json(resumed_run / "workspace_ref.json", {"workspace_dir": str(workspace)})
        _write_json(resumed_run / "verification.json", {"passed": True, "commands": []})
        return SimpleNamespace(run_dir=resumed_run, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(resume_commands, "run_once", fake_run_once)
    monkeypatch.setattr(
        resume_commands,
        "_load_runner_config",
        lambda repo_root: RunnerConfig(
            repo_root=repo_root,
            runs_dir=tmp_path / "runs",
            agents={},
            policies={},
        ),
    )
    parser = build_parser()
    args = parser.parse_args(
        ["--repo-root", str(tmp_path), "resume", "--run-dir", str(run_dir), "--no-docker"]
    )

    assert resume_commands._cmd_resume(args) == 0
    request = seen["request"]
    assert request.resume_workspace_dir == workspace
    assert request.ref == "backlog/abc123abc123"
    assert request.keep_workspace is True
    assert request.agent == "codex"
    assert request.codex_resume_session_id == "019f5000-0000-7000-8000-000000000002"
    assert request.exec_use_host_agent_login is True
    resume_ref = json.loads((resumed_run / "resume_ref.json").read_text(encoding="utf-8"))
    assert resume_ref["resumed_from_run_dir"] == str(run_dir)
    original_state = json.loads((run_dir / "ticket_resume_state.json").read_text(encoding="utf-8"))
    assert original_state["last_resumed_run_dir"] == str(resumed_run)


def test_resume_missing_workspace_falls_back_to_recorded_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, _, _ = _make_resume_run(tmp_path, workspace_exists=False)
    fallback_repo = tmp_path / "fallback_repo"
    fallback_repo.mkdir()
    resumed_run = tmp_path / "resumed_run"
    seen = {}

    def fake_run_once(_cfg: object, request: object) -> object:
        seen["request"] = request
        resumed_workspace = tmp_path / "resumed_workspace"
        resumed_workspace.mkdir(exist_ok=True)
        _write_json(resumed_run / "workspace_ref.json", {"workspace_dir": str(resumed_workspace)})
        _write_json(resumed_run / "verification.json", {"passed": True, "commands": []})
        return SimpleNamespace(run_dir=resumed_run, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(resume_commands, "run_once", fake_run_once)
    monkeypatch.setattr(
        resume_commands,
        "_load_runner_config",
        lambda repo_root: RunnerConfig(
            repo_root=repo_root,
            runs_dir=tmp_path / "runs",
            agents={},
            policies={},
        ),
    )
    parser = build_parser()
    args = parser.parse_args(
        [
            "--repo-root",
            str(tmp_path),
            "resume",
            "--run-dir",
            str(run_dir),
            "--repo",
            str(fallback_repo),
            "--no-docker",
        ]
    )

    assert resume_commands._cmd_resume(args) == 0
    request = seen["request"]
    assert request.resume_workspace_dir is None
    assert request.repo == str(fallback_repo)
    assert request.ref == "backlog/abc123abc123"
    resume_ref = json.loads((resumed_run / "resume_ref.json").read_text(encoding="utf-8"))
    assert resume_ref["workspace_strategy"] == "recorded_branch_fallback"


def test_resume_rejects_invalid_resume_state(tmp_path: Path) -> None:
    run_dir, _, _ = _make_resume_run(tmp_path)
    state = json.loads((run_dir / "ticket_resume_state.json").read_text(encoding="utf-8"))
    state["lifecycle_state"] = "push_failed"
    _write_json(run_dir / "ticket_resume_state.json", state)
    parser = build_parser()
    args = parser.parse_args(
        ["--repo-root", str(tmp_path), "resume", "--run-dir", str(run_dir), "--no-docker"]
    )

    with pytest.raises(SystemExit) as exc:
        resume_commands._cmd_resume(args)
    assert "verification_failed_resume_ready" in str(exc.value)


def _make_pr_resume_run(
    tmp_path: Path,
    *,
    lifecycle_state: str = "review_changes_requested",
    review_decision: str = "changes_requested",
) -> tuple[Path, Path, Path, Path]:
    run_dir = tmp_path / "original_pr_run"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / ".git").mkdir()
    ticket_path = tmp_path / ".agents" / "plans" / "4 - for_review" / "ticket.md"
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
    ticket_path.write_text("# PR Resume Ticket\n\nImplement the thing.\n", encoding="utf-8")
    review_run_dir = tmp_path / "review_run"
    _write_json(run_dir / "workspace_ref.json", {"workspace_dir": str(workspace)})
    _write_json(run_dir / "target_ref.json", {"agent": "codex"})
    (run_dir / "raw_events.jsonl").write_text(
        json.dumps(
            {
                "type": "thread.started",
                "thread_id": "019f5000-0000-7000-8000-000000000003",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_json(
        run_dir / "ticket_ref.json",
        {
            "schema_version": 1,
            "fingerprint": "def456def456def0",
            "title": "PR Resume Ticket",
            "export_kind": "implementation",
            "owner_repo": {"root": str(workspace), "idea_path": str(ticket_path)},
        },
    )
    _write_json(
        run_dir / "handoff_summary.json",
        {"pr_url": "https://example.invalid/pr/9", "branch": "stale/branch", "summary": "prior"},
    )
    _write_json(
        run_dir / "pr_ref.json",
        {"created": True, "url": "https://example.invalid/pr/9", "branch": "stale/branch"},
    )
    _write_json(
        run_dir / "ci_gate.json",
        {"passed": lifecycle_state != "ci_failed", "run_url": "https://example.invalid/runs/old"},
    )
    _write_json(run_dir / "report.json", {"summary": "prior implementation summary"})
    _write_json(
        review_run_dir / "review_summary.json",
        {
            "review_decision": review_decision,
            "merge_ready": review_decision == "approved",
            "rationale": "Fix the edge case.",
            "pr_url": "https://example.invalid/pr/9",
            "head_ref_name": "stale/branch",
            "findings": [
                {
                    "severity": "error",
                    "title": "Edge case broken",
                    "details": "Handle empty input.",
                    "suggested_fix": "Add the guard.",
                }
            ],
        },
    )
    _write_json(review_run_dir / "review_ref.json", {"implementation_run_dir": str(run_dir)})
    _write_json(
        run_dir / "ticket_resume_state.json",
        {
            "schema_version": 1,
            "kind": "ticket_resume_state",
            "ticket": {
                "fingerprint": "def456def456def0",
                "path": str(ticket_path),
                "title": "PR Resume Ticket",
                "export_kind": "implementation",
            },
            "owner_root": str(workspace),
            "run_dir": str(run_dir),
            "workspace_path": str(workspace),
            "branch": "stale/branch",
            "pr_url": "https://example.invalid/pr/9",
            "lifecycle_state": lifecycle_state,
            "blocking_reason": "Fix the edge case."
            if lifecycle_state != "ci_failed"
            else "CI failed.",
            "source_evidence_paths": {
                "review_summary": str(review_run_dir / "review_summary.json")
            },
        },
    )
    return run_dir, workspace, ticket_path, review_run_dir


def _pr_context(
    *,
    check_state: str = "FAILURE",
    mergeable: str = "MERGEABLE",
    review_decision: str | None = None,
) -> dict[str, object]:
    return {
        "pr": {
            "number": 9,
            "url": "https://example.invalid/pr/9",
            "title": "PR title from refreshed state",
            "state": "OPEN",
            "isDraft": False,
            "headRefName": "backlog/current-pr-branch",
            "baseRefName": "dev",
            "mergeable": mergeable,
            "reviewDecision": review_decision,
        },
        "checks": [
            {
                "name": "CI",
                "state": check_state,
                "link": "https://example.invalid/runs/current",
                "bucket": "fail" if check_state == "FAILURE" else "pass",
            }
        ],
        "ci_status": "completed",
        "ci_conclusion": "success" if check_state == "SUCCESS" else "failure",
        "changed_files": ["src/app.py"],
        "diff_excerpt": "diff --git a/src/app.py b/src/app.py\n",
        "diff_truncated": False,
    }


def test_pr_resume_review_changes_requested_dry_run_refreshes_and_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, workspace, _, _ = _make_pr_resume_run(tmp_path)
    calls = []

    def fake_collect(*, workspace_dir: Path, pr_url: str) -> dict[str, object]:
        calls.append((workspace_dir, pr_url))
        return _pr_context()

    monkeypatch.setattr(resume_commands, "_collect_pr_review_context", fake_collect)
    parser = build_parser()
    args = parser.parse_args(
        [
            "--repo-root",
            str(tmp_path),
            "resume",
            "--run-dir",
            str(run_dir),
            "--no-docker",
            "--dry-run",
        ]
    )

    assert resume_commands._cmd_resume(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert calls == [(workspace, "https://example.invalid/pr/9")]
    assert payload["resume_kind"] == "pr"
    assert payload["branch"] == "backlog/current-pr-branch"
    assert payload["run_request"]["ref"] == "backlog/current-pr-branch"
    assert payload["run_request"]["pr"] is False
    assert (
        payload["run_request"]["codex_resume_session_id"]
        == "019f5000-0000-7000-8000-000000000003"
    )
    assert payload["implementation_author_continuity"]["fresh_restart"] is False
    assert payload["failing_check_pointers"][0]["link"] == "https://example.invalid/runs/current"
    prompt = payload["prompt"]
    assert "Current PR metadata (refreshed immediately before this prompt)" in prompt
    assert "PR title from refreshed state" in prompt
    assert "Edge case broken" in prompt
    assert "Run artifact paths" in prompt
    assert "backlog/current-pr-branch" in prompt


def test_pr_resume_ci_failure_prompt_includes_failing_check_pointers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, _, _, _ = _make_pr_resume_run(
        tmp_path, lifecycle_state="ci_failed", review_decision="approved"
    )
    monkeypatch.setattr(
        resume_commands, "_collect_pr_review_context", lambda **_: _pr_context(check_state="ERROR")
    )
    parser = build_parser()
    args = parser.parse_args(
        [
            "--repo-root",
            str(tmp_path),
            "resume",
            "--run-dir",
            str(run_dir),
            "--no-docker",
            "--dry-run",
        ]
    )

    assert resume_commands._cmd_resume(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["failing_check_pointers"] == [
        {
            "name": "CI",
            "state": "ERROR",
            "link": "https://example.invalid/runs/current",
            "bucket": "pass",
            "startedAt": None,
            "completedAt": None,
        }
    ]
    assert "Failing check/log pointers" in payload["prompt"]
    assert "https://example.invalid/runs/current" in payload["prompt"]


def test_pr_resume_noops_when_stale_ci_failure_is_currently_green(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, _, _, _ = _make_pr_resume_run(
        tmp_path, lifecycle_state="ci_failed", review_decision="approved"
    )
    monkeypatch.setattr(
        resume_commands,
        "_collect_pr_review_context",
        lambda **_: _pr_context(check_state="SUCCESS"),
    )

    def fail_run_once(*_args, **_kwargs):
        raise AssertionError("stale green PR should not launch a resumed agent")

    monkeypatch.setattr(resume_commands, "run_once", fail_run_once)
    parser = build_parser()
    args = parser.parse_args(
        ["--repo-root", str(tmp_path), "resume", "--run-dir", str(run_dir), "--no-docker"]
    )

    assert resume_commands._cmd_resume(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "noop_current_gates_green"
    assert payload["branch"] == "backlog/current-pr-branch"


def test_pr_resume_noops_when_review_changes_requested_is_now_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, _, _, _ = _make_pr_resume_run(tmp_path)
    monkeypatch.setattr(
        resume_commands,
        "_collect_pr_review_context",
        lambda **_: _pr_context(check_state="SUCCESS", review_decision="APPROVED"),
    )

    def fail_run_once(*_args, **_kwargs):
        raise AssertionError("approved green PR should not launch a resumed agent")

    monkeypatch.setattr(resume_commands, "run_once", fail_run_once)
    parser = build_parser()
    args = parser.parse_args(
        ["--repo-root", str(tmp_path), "resume", "--run-dir", str(run_dir), "--no-docker"]
    )

    assert resume_commands._cmd_resume(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "noop_current_gates_green"
    assert payload["current_pr_context"]["pr"]["reviewDecision"] == "APPROVED"


def test_pr_resume_runs_agent_then_commits_and_pushes_existing_pr_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, workspace, _, _ = _make_pr_resume_run(
        tmp_path, lifecycle_state="ci_failed", review_decision="approved"
    )
    resumed_run = tmp_path / "resumed_pr_run"
    seen = {}
    monkeypatch.setattr(
        resume_commands,
        "_collect_pr_review_context",
        lambda **_: _pr_context(check_state="FAILURE"),
    )
    monkeypatch.setattr(
        resume_commands,
        "_load_runner_config",
        lambda repo_root: RunnerConfig(
            repo_root=repo_root, runs_dir=tmp_path / "runs", agents={}, policies={}
        ),
    )

    def fake_run_once(_cfg: object, request: object) -> object:
        seen["request"] = request
        _write_json(resumed_run / "workspace_ref.json", {"workspace_dir": str(workspace)})
        _write_json(resumed_run / "verification.json", {"passed": True, "commands": []})
        return SimpleNamespace(run_dir=resumed_run, exit_code=0, report_validation_errors=[])

    def fake_commit(**kwargs):
        seen["commit"] = kwargs
        _write_json(
            resumed_run / "git_ref.json",
            {"branch": kwargs["branch"], "commit_performed": True, "head_commit": "abc"},
        )
        return {
            "branch": kwargs["branch"],
            "commit_performed": True,
            "head_commit": "abc",
            "error": None,
        }

    def fake_push(**kwargs):
        seen["push"] = kwargs
        _write_json(
            resumed_run / "push_ref.json",
            {
                "branch": kwargs["branch"],
                "pushed": True,
                "remote_name": kwargs["remote_name"],
                "remote_url": "origin-url",
            },
        )
        return {
            "branch": kwargs["branch"],
            "pushed": True,
            "remote_name": kwargs["remote_name"],
            "remote_url": "origin-url",
            "error": None,
        }

    monkeypatch.setattr(resume_commands, "run_once", fake_run_once)
    monkeypatch.setattr(resume_commands, "finalize_commit", fake_commit)
    monkeypatch.setattr(resume_commands, "finalize_push", fake_push)
    monkeypatch.setattr(resume_commands, "_git_head_sha", lambda _workspace: "abc")
    monkeypatch.setattr(
        resume_commands,
        "_wait_for_ci_success",
        lambda **kwargs: {
            "passed": True,
            "status": "completed",
            "conclusion": "success",
            "run_url": "https://example.invalid/runs/new",
        },
    )
    parser = build_parser()
    args = parser.parse_args(
        ["--repo-root", str(tmp_path), "resume", "--run-dir", str(run_dir), "--no-docker"]
    )

    assert resume_commands._cmd_resume(args) == 0
    request = seen["request"]
    assert request.ref == "backlog/current-pr-branch"
    assert request.keep_workspace is True
    assert request.agent == "codex"
    assert request.codex_resume_session_id == "019f5000-0000-7000-8000-000000000003"
    assert request.exec_use_host_agent_login is True
    assert seen["commit"]["branch"] == "backlog/current-pr-branch"
    assert seen["push"]["branch"] == "backlog/current-pr-branch"
    pr_ref = json.loads((resumed_run / "pr_ref.json").read_text(encoding="utf-8"))
    assert pr_ref["existing_pr"] is True
    assert pr_ref["created"] is True
    assert pr_ref["requested"] is False
    handoff = json.loads((resumed_run / "handoff_summary.json").read_text(encoding="utf-8"))
    assert handoff["pr_created"] is True
    assert handoff["review_required"] is True
