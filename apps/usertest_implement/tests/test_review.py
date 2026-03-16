from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from runner_core.runner import RunResult

import usertest_implement.cli as implement_cli
from usertest_implement.cli import (
    SelectedTicket,
    _build_final_review_summary,
    _build_pr_review_body,
    _cmd_review_merge,
    _cmd_review_run,
    _collect_pr_review_context,
    _read_json,
    _run_gh_json,
    _run_gh_text,
)


@pytest.fixture(autouse=True)
def _stub_gh_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "usertest_implement.cli.shutil.which",
        lambda cmd: "/usr/bin/gh" if cmd == "gh" else None,
    )


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _make_ticket(owner_root: Path, *, bucket: str, fingerprint: str) -> Path:
    bucket_dir = owner_root / ".agents" / "plans" / bucket
    bucket_dir.mkdir(parents=True, exist_ok=True)
    ticket_path = bucket_dir / f"20260309_{fingerprint}_ticket.md"
    ticket_path.write_text(
        "# Ticket\n\n"
        f"- Fingerprint: `{fingerprint}`\n"
        "- Export kind: `implementation`\n"
        "- Stage: `ready_for_ticket`\n",
        encoding="utf-8",
    )
    return ticket_path


def _review_run_args(
    *,
    repo_root: Path,
    owner_root: Path,
    ticket_path: Path,
    ledger: Path,
) -> argparse.Namespace:
    return argparse.Namespace(
        repo_root=repo_root,
        owner_root=owner_root,
        ticket_path=ticket_path,
        fingerprint=None,
        ledger=ledger,
        agent="codex",
        model=None,
        policy="write",
        persona_id="compliance_sentinel",
        mission_id="review_backlog_implementation_pr_v1",
        seed=0,
        agent_config_override=[],
        keep_workspace=False,
        exec_backend="local",
        exec_use_host_agent_login=True,
        exec_use_target_sandbox_cli_install=False,
        exec_docker_profile=None,
        exec_keep_container=True,
        exec_cache="warm",
        exec_cache_dir=None,
        maintenance_venv_cache=True,
        dry_run=False,
    )


def _review_simple_args(
    *,
    repo_root: Path,
    owner_root: Path,
    ticket_path: Path,
    ledger: Path,
) -> argparse.Namespace:
    return argparse.Namespace(
        repo_root=repo_root,
        owner_root=owner_root,
        ticket_path=ticket_path,
        fingerprint=None,
        ledger=ledger,
    )


def test_build_final_review_summary_requires_green_ci_and_alignment(tmp_path: Path) -> None:
    owner_root = tmp_path / "repo"
    ticket_path = _make_ticket(owner_root, bucket="4 - for_review", fingerprint="abc123abc123abcd")
    report = {
        "issues": [
            {
                "severity": "warn",
                "title": "Scope note",
                "details": "Touched one extra file.",
            }
        ]
    }
    summary = _build_final_review_summary(
        selected=type("Selected", (), {
            "fingerprint": "abc123abc123abcd",
            "idea_path": ticket_path,
        })(),
        review_run_dir=tmp_path / "review_run",
        pr_url="https://example.invalid/pr/1",
        pr_context={
            "pr": {
                "number": 1,
                "title": "PR",
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "headRefName": "branch",
                "baseRefName": "dev",
            },
            "ci_status": "completed",
            "ci_conclusion": "success",
        },
        agent_summary={
            "review_decision": "approved",
            "approach_alignment": "aligned",
            "scope_assessment": "appropriate",
            "rationale": "Looks good.",
        },
        report=report,
    )
    assert summary["merge_ready"] is True
    assert len(summary["findings"]) == 1

    blocked = _build_final_review_summary(
        selected=type("Selected", (), {
            "fingerprint": "abc123abc123abcd",
            "idea_path": ticket_path,
        })(),
        review_run_dir=tmp_path / "review_run2",
        pr_url="https://example.invalid/pr/1",
        pr_context={
            "pr": {
                "number": 1,
                "title": "PR",
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "headRefName": "branch",
                "baseRefName": "dev",
            },
            "ci_status": "pending",
            "ci_conclusion": None,
        },
        agent_summary={
            "review_decision": "approved",
            "approach_alignment": "aligned",
            "scope_assessment": "appropriate",
            "rationale": "Looks good.",
        },
        report=report,
    )
    assert blocked["merge_ready"] is False


def test_build_pr_review_body_includes_findings_and_merge_state() -> None:
    body = _build_pr_review_body(
        review_summary={
            "review_decision": "changes_requested",
            "approach_alignment": "diverged",
            "scope_assessment": "excessive",
            "rationale": "The implementation drifted from the selected approach.",
            "merge_ready": False,
            "findings": [
                {
                    "severity": "high",
                    "title": "Behavior regression",
                    "details": "The PR changes the CLI contract.",
                    "evidence": {"path": "apps/usertest_implement/src/usertest_implement/cli.py"},
                    "suggested_fix": "Restore the original CLI arguments.",
                }
            ],
        }
    )

    assert "## Automated implementation review" in body
    assert "- Decision: `changes_requested`" in body
    assert "- Merge ready: `no`" in body
    assert "1. [high] Behavior regression" in body
    assert "Evidence:" in body
    assert "Suggested fix: Restore the original CLI arguments." in body


def test_review_run_writes_review_summary_and_updates_ledger(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo_root"
    owner_root = repo_root
    owner_root.mkdir(parents=True)
    ticket_path = _make_ticket(owner_root, bucket="4 - for_review", fingerprint="feedfacefeedface")
    ledger_path = repo_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    impl_run_dir = repo_root / "runs" / "impl" / "0"
    review_run_dir = repo_root / "runs" / "review" / "0"
    _write_json(
        impl_run_dir / "handoff_summary.json",
        {
            "schema_version": 1,
            "pr_created": True,
            "pr_url": "https://example.invalid/pr/2",
            "ci_conclusion": "success",
        },
    )
    _write_json(
        impl_run_dir / "pr_ref.json",
        {"created": True, "url": "https://example.invalid/pr/2"},
    )
    _write_json(impl_run_dir / "ci_gate.json", {"passed": True})
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        "schema_version: 1\nupdated_at: null\nactions:\n"
        "  feedfacefeedface:\n"
        "    fingerprint: feedfacefeedface\n"
        f"    last_run_dir: {json.dumps(str(impl_run_dir))}\n",
        encoding="utf-8",
    )

    def _fake_collect_pr_review_context(*, workspace_dir: Path, pr_url: str) -> dict[str, object]:
        assert workspace_dir == owner_root
        assert pr_url == "https://example.invalid/pr/2"
        return {
            "pr": {
                "number": 2,
                "url": pr_url,
                "title": "PR",
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "headRefName": "backlog/review",
                "baseRefName": "dev",
            },
            "checks": [{"name": "CI", "state": "SUCCESS"}],
            "ci_status": "completed",
            "ci_conclusion": "success",
            "changed_files": ["apps/usertest_implement/src/usertest_implement/cli.py"],
            "diff_excerpt": "diff --git a/file b/file",
            "diff_truncated": False,
        }

    def _fake_run_once(_cfg, _request):
        assert _request.repo == "https://example.invalid/repo.git"
        assert _request.ref == "backlog/review"
        assert _request.agent_append_system_prompt is None
        assert _request.agent_append_system_prompt_file is not None
        assert _request.agent_append_system_prompt_file.exists()
        _write_json(
            review_run_dir / "report.json",
            {
                "schema_version": 1,
                "kind": "task_run_v1",
                "status": "success",
                "goal": "Review",
                "summary": "Reviewed.",
                "steps": [
                    {
                        "name": "Review",
                        "attempts": [{"action": "Reviewed"}],
                        "outcome": "done",
                    }
                ],
                "outputs": [],
                "next_actions": ["Merge after approval."],
                "issues": [],
                "extensions": {
                    "review_summary": {
                        "review_decision": "approved",
                        "approach_alignment": "aligned",
                        "scope_assessment": "appropriate",
                        "rationale": "Aligned and scoped correctly.",
                    }
                },
            },
        )
        return RunResult(run_dir=review_run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr("usertest_implement.cli._load_runner_config", lambda _repo_root: object())
    monkeypatch.setattr(
        "usertest_implement.cli._collect_pr_review_context",
        _fake_collect_pr_review_context,
    )
    monkeypatch.setattr(
        "usertest_implement.cli._git_remote_url",
        lambda *, repo_dir, remote_name: "https://example.invalid/repo.git",
    )
    monkeypatch.setattr(
        "usertest_implement.cli._infer_git_root",
        lambda path: owner_root,
    )
    monkeypatch.setattr(
        "usertest_implement.cli._maintenance_profile_is_eligible",
        lambda *, repo_root, repo_input: False,
    )
    monkeypatch.setattr("usertest_implement.cli.run_once", _fake_run_once)

    def _fake_subprocess_run(
        argv,
        cwd=None,
        capture_output=None,
        text=None,
        encoding=None,
        errors=None,
        check=None,
    ):
        assert cwd == str(owner_root)
        assert capture_output is True
        assert text is True
        assert encoding == "utf-8"
        assert errors == "replace"
        assert check is False
        assert argv[:4] == ["gh", "pr", "review", "https://example.invalid/pr/2"]
        assert "--comment" in argv
        body_path = Path(argv[argv.index("--body-file") + 1])
        body_text = body_path.read_text(encoding="utf-8")
        assert "Automated implementation review" in body_text
        assert "- Decision: `approved`" in body_text
        return SimpleNamespace(returncode=0, stdout="review submitted", stderr="")

    monkeypatch.setattr("usertest_implement.cli.subprocess.run", _fake_subprocess_run)

    exit_code = _cmd_review_run(
        _review_run_args(
            repo_root=repo_root,
            owner_root=owner_root,
            ticket_path=ticket_path,
            ledger=ledger_path,
        )
    )
    assert exit_code == 0
    review_summary = _read_json(review_run_dir / "review_summary.json")
    assert isinstance(review_summary, dict)
    assert review_summary["merge_ready"] is True
    assert review_summary["review_decision"] == "approved"
    pr_review_ref = _read_json(review_run_dir / "pr_review_ref.json")
    assert isinstance(pr_review_ref, dict)
    assert pr_review_ref["submitted"] is True
    assert pr_review_ref["event"] == "COMMENT"

    ledger_text = ledger_path.read_text(encoding="utf-8")
    assert "last_review_run_dir" in ledger_text
    assert "last_review_merge_ready: true" in ledger_text.lower()


def test_review_merge_refuses_when_summary_not_merge_ready(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo_root"
    owner_root = repo_root
    owner_root.mkdir(parents=True)
    ticket_path = _make_ticket(owner_root, bucket="4 - for_review", fingerprint="deadbeefdeadbeef")
    ledger_path = repo_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    review_run_dir = repo_root / "runs" / "review" / "1"
    _write_json(
        review_run_dir / "review_summary.json",
        {
            "schema_version": 1,
            "pr_url": "https://example.invalid/pr/3",
            "review_decision": "changes_requested",
            "merge_ready": False,
            "ci_conclusion": "failure",
        },
    )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        "schema_version: 1\nupdated_at: null\nactions:\n"
        "  deadbeefdeadbeef:\n"
        "    fingerprint: deadbeefdeadbeef\n"
        f"    last_review_run_dir: {json.dumps(str(review_run_dir))}\n",
        encoding="utf-8",
    )

    try:
        _cmd_review_merge(
            _review_simple_args(
                repo_root=repo_root,
                owner_root=owner_root,
                ticket_path=ticket_path,
                ledger=ledger_path,
            )
        )
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("Expected review merge to refuse non-merge-ready summary")


def test_review_merge_moves_ticket_to_complete(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo_root"
    owner_root = repo_root
    owner_root.mkdir(parents=True)
    ticket_path = _make_ticket(owner_root, bucket="4 - for_review", fingerprint="cafebabecafebabe")
    complete_path = owner_root / ".agents" / "plans" / "5 - complete" / ticket_path.name
    (owner_root / ".agents" / "plans" / "5 - complete").mkdir(parents=True, exist_ok=True)
    ledger_path = repo_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    review_run_dir = repo_root / "runs" / "review" / "2"
    _write_json(
        review_run_dir / "review_summary.json",
        {
            "schema_version": 1,
            "pr_url": "https://example.invalid/pr/4",
            "review_decision": "approved",
            "merge_ready": True,
            "ci_conclusion": "success",
        },
    )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        "schema_version: 1\nupdated_at: null\nactions:\n"
        "  cafebabecafebabe:\n"
        "    fingerprint: cafebabecafebabe\n"
        f"    last_review_run_dir: {json.dumps(str(review_run_dir))}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "usertest_implement.cli._collect_pr_review_context",
        lambda **_: {
            "pr": {
                "number": 4,
                "url": "https://example.invalid/pr/4",
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
            },
            "ci_status": "completed",
            "ci_conclusion": "success",
        },
    )

    def _fake_subprocess_run(argv, cwd=None, capture_output=None, text=None, check=None):
        assert argv[:3] == ["gh", "pr", "merge"]

        class _Proc:
            returncode = 0
            stdout = "merged"
            stderr = ""

        return _Proc()

    monkeypatch.setattr("usertest_implement.cli.subprocess.run", _fake_subprocess_run)

    exit_code = _cmd_review_merge(
        _review_simple_args(
            repo_root=repo_root,
            owner_root=owner_root,
            ticket_path=ticket_path,
            ledger=ledger_path,
        )
    )
    assert exit_code == 0
    assert complete_path.exists()
    merge_ref = _read_json(review_run_dir / "merge_ref.json")
    assert isinstance(merge_ref, dict)
    assert merge_ref["merged"] is True


def test_run_defers_review_until_for_review_and_green_ci(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo_root"
    repo_root.mkdir(parents=True)
    target_repo = tmp_path / "target_repo"
    target_repo.mkdir(parents=True)
    ticket_path = _make_ticket(target_repo, bucket="2 - ready", fingerprint="facefacefaceface")
    impl_run_dir = repo_root / "runs" / "impl" / "0"
    impl_run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    def _fake_run_once(_cfg, _request):
        _write_json(
            impl_run_dir / "workspace_ref.json",
            {"workspace_dir": str(workspace_dir)},
        )
        return SimpleNamespace(run_dir=impl_run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr("usertest_implement.cli.run_once", _fake_run_once)
    monkeypatch.setattr(
        "usertest_implement.cli._maintenance_profile_is_eligible",
        lambda **_: False,
    )
    monkeypatch.setattr("usertest_implement.cli._git_head_sha", lambda _workspace_dir: "abc123")
    monkeypatch.setattr(
        "usertest_implement.cli.finalize_commit",
        lambda **_: {"commit_performed": True, "branch": "backlog/test", "head_commit": "abc123"},
    )
    monkeypatch.setattr(
        "usertest_implement.cli.finalize_push",
        lambda **_: {"pushed": True, "remote_name": "origin", "remote_url": "https://example.invalid/repo.git"},
    )
    review_run_dir = repo_root / "runs" / "review" / "0"

    def _fake_run_review_for_selected_ticket(**kwargs):
        assert kwargs["repo_root"] == repo_root
        assert kwargs["owner_root"] == target_repo
        assert kwargs["implementation_run_dir"] == impl_run_dir
        assert kwargs["review_agent"] == "claude"
        assert kwargs["review_model"] == "review-model"
        return (
            review_run_dir,
            {
                "review_decision": "approved",
                "merge_ready": True,
                "ci_conclusion": "success",
            },
        )

    monkeypatch.setattr(
        "usertest_implement.cli._run_review_for_selected_ticket",
        _fake_run_review_for_selected_ticket,
    )

    def _fake_subprocess_run(argv, cwd=None, capture_output=None, text=None, check=None):
        if argv[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://example.invalid/pr/55\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess call: {argv}")

    monkeypatch.setattr("usertest_implement.cli.subprocess.run", _fake_subprocess_run)
    monkeypatch.setattr(
        "usertest_implement.cli.shutil.which", 
        lambda cmd: "/usr/bin/gh" if cmd == "gh" else None
    )

    args = argparse.Namespace(
        repo_root=repo_root,
        settings=None,
        settings_profile=None,
        repo=str(target_repo),
        ref=None,
        agent="codex",
        model=None,
        policy="write",
        persona_id="thoughtful_maintainer",
        mission_id="implement_maintenance_backlog_ticket_v1",
        implementation_review_agent="claude",
        implementation_review_model="review-model",
        seed=0,
        agent_config_override=[],
        keep_workspace=False,
        exec_backend="local",
        exec_use_host_agent_login=True,
        exec_use_target_sandbox_cli_install=False,
        exec_docker_profile=None,
        exec_keep_container=True,
        exec_cache="warm",
        exec_cache_dir=None,
        maintenance_venv_cache=True,
        dry_run=False,
        verification_commands=[],
        verification_timeout_seconds=None,
        skip_verify=False,
        verify_reuse="auto",
        ci_timeout_seconds=60.0,
        skip_ci_wait=True,
        draft_pr_on_ci_failure=True,
        commit=True,
        branch=None,
        commit_message=None,
        git_user_name=None,
        git_user_email=None,
        push=True,
        remote_name="origin",
        remote_url=None,
        force_push=False,
        base_branch="dev",
        pr=True,
        move_on_start=False,
        move_on_commit=True,
        ledger=Path(".agents/state/backlog_implement_actions.yaml"),
        ticket_path=ticket_path,
        tickets_export=None,
        fingerprint=None,
        _settings_info=None,
    )

    cfg = object()
    selected = SelectedTicket(
        fingerprint="facefacefaceface",
        title="Ticket",
        export_kind="implementation",
        stage="ready_for_ticket",
        owner_root=target_repo,
        idea_path=ticket_path,
        ticket_markdown=ticket_path.read_text(encoding="utf-8"),
        tickets_export_path=None,
        export_index=None,
    )

    exit_code = implement_cli._run_selected_ticket(
        args=args,
        repo_root=repo_root,
        cfg=cfg,
        selected=selected,
    )
    assert exit_code == 0
    handoff_summary = _read_json(impl_run_dir / "handoff_summary.json")
    assert isinstance(handoff_summary, dict)
    assert handoff_summary["pr_created"] is True
    assert handoff_summary["review_required"] is True
    assert handoff_summary["review_run_dir"] == str(review_run_dir)
    assert handoff_summary["review_merge_ready"] is True
    assert handoff_summary["final_status"] == "success"


def test_run_records_missing_gh_when_pr_create_exec_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo_root"
    repo_root.mkdir(parents=True)
    target_repo = tmp_path / "target_repo"
    target_repo.mkdir(parents=True)
    ticket_path = _make_ticket(target_repo, bucket="2 - ready", fingerprint="ghghghghghghghgh")
    impl_run_dir = repo_root / "runs" / "impl" / "0"
    impl_run_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    def _fake_run_once(_cfg, _request):
        _write_json(
            impl_run_dir / "workspace_ref.json",
            {"workspace_dir": str(workspace_dir)},
        )
        return SimpleNamespace(run_dir=impl_run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr("usertest_implement.cli.run_once", _fake_run_once)
    monkeypatch.setattr(
        "usertest_implement.cli._maintenance_profile_is_eligible",
        lambda **_: False,
    )
    monkeypatch.setattr("usertest_implement.cli._git_head_sha", lambda _workspace_dir: "abc123")
    monkeypatch.setattr(
        "usertest_implement.cli.finalize_commit",
        lambda **_: {"commit_performed": True, "branch": "backlog/test", "head_commit": "abc123"},
    )
    monkeypatch.setattr(
        "usertest_implement.cli.finalize_push",
        lambda **_: {"pushed": True, "remote_name": "origin", "remote_url": "https://example.invalid/repo.git"},
    )

    def _fake_subprocess_run(argv, cwd=None, capture_output=None, text=None, check=None):
        if argv[:3] == ["gh", "pr", "create"]:
            raise OSError("CreateProcess failed")
        raise AssertionError(f"unexpected subprocess call: {argv}")

    monkeypatch.setattr("usertest_implement.cli.subprocess.run", _fake_subprocess_run)

    args = argparse.Namespace(
        repo_root=repo_root,
        settings=None,
        settings_profile=None,
        repo=str(target_repo),
        ref=None,
        agent="codex",
        model=None,
        policy="write",
        persona_id="thoughtful_maintainer",
        mission_id="implement_maintenance_backlog_ticket_v1",
        implementation_review_agent="claude",
        implementation_review_model="review-model",
        seed=0,
        agent_config_override=[],
        keep_workspace=False,
        exec_backend="local",
        exec_use_host_agent_login=True,
        exec_use_target_sandbox_cli_install=False,
        exec_docker_profile=None,
        exec_keep_container=True,
        exec_cache="warm",
        exec_cache_dir=None,
        maintenance_venv_cache=True,
        dry_run=False,
        verification_commands=[],
        verification_timeout_seconds=None,
        skip_verify=False,
        verify_reuse="auto",
        ci_timeout_seconds=60.0,
        skip_ci_wait=True,
        draft_pr_on_ci_failure=True,
        commit=True,
        branch=None,
        commit_message=None,
        git_user_name=None,
        git_user_email=None,
        push=True,
        remote_name="origin",
        remote_url=None,
        force_push=False,
        base_branch="dev",
        pr=True,
        move_on_start=False,
        move_on_commit=True,
        ledger=Path(".agents/state/backlog_implement_actions.yaml"),
        ticket_path=ticket_path,
        tickets_export=None,
        fingerprint=None,
        _settings_info=None,
    )

    cfg = object()
    selected = SelectedTicket(
        fingerprint="ghghghghghghghgh",
        title="Ticket",
        export_kind="implementation",
        stage="ready_for_ticket",
        owner_root=target_repo,
        idea_path=ticket_path,
        ticket_markdown=ticket_path.read_text(encoding="utf-8"),
        tickets_export_path=None,
        export_index=None,
    )

    exit_code = implement_cli._run_selected_ticket(
        args=args,
        repo_root=repo_root,
        cfg=cfg,
        selected=selected,
    )

    assert exit_code == 5
    pr_ref = _read_json(impl_run_dir / "pr_ref.json")
    assert isinstance(pr_ref, dict)
    assert pr_ref["created"] is False
    assert pr_ref["error"] == "gh not found on PATH"


def test_review_run_refuses_when_ticket_not_in_for_review(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo_root"
    repo_root.mkdir(parents=True)
    owner_root = repo_root
    owner_root.mkdir(parents=True, exist_ok=True)
    ticket_path = _make_ticket(owner_root, bucket="2 - ready", fingerprint="beadbeadbeadbead")
    ledger_path = repo_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    impl_run_dir = repo_root / "runs" / "impl" / "1"
    _write_json(
        impl_run_dir / "handoff_summary.json",
        {
            "schema_version": 1,
            "pr_created": True,
            "pr_url": "https://example.invalid/pr/56",
            "ci_conclusion": "success",
        },
    )
    _write_json(
        impl_run_dir / "pr_ref.json",
        {"created": True, "url": "https://example.invalid/pr/56"},
    )
    _write_json(impl_run_dir / "ci_gate.json", {"passed": True})
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        "schema_version: 1\nupdated_at: null\nactions:\n"
        "  beadbeadbeadbead:\n"
        "    fingerprint: beadbeadbeadbead\n"
        f"    last_run_dir: {json.dumps(str(impl_run_dir))}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("usertest_implement.cli._load_runner_config", lambda _repo_root: object())

    try:
        _cmd_review_run(
            _review_run_args(
                repo_root=repo_root,
                owner_root=owner_root,
                ticket_path=ticket_path,
                ledger=ledger_path,
            )
        )
    except SystemExit as exc:
        assert "not in 4 - for_review" in str(exc)
    else:
        raise AssertionError("Expected review run to refuse non-for_review tickets")


def test_review_run_refuses_when_pr_gate_not_green(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo_root"
    owner_root = repo_root
    owner_root.mkdir(parents=True, exist_ok=True)
    ticket_path = _make_ticket(owner_root, bucket="4 - for_review", fingerprint="beadbeadbeadbead")
    ledger_path = repo_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    impl_run_dir = repo_root / "runs" / "impl" / "2"
    _write_json(
        impl_run_dir / "handoff_summary.json",
        {
            "schema_version": 1,
            "pr_created": True,
            "pr_url": "https://example.invalid/pr/57",
            "ci_conclusion": None,
        },
    )
    _write_json(
        impl_run_dir / "pr_ref.json",
        {"created": True, "url": "https://example.invalid/pr/57"},
    )
    _write_json(impl_run_dir / "ci_gate.json", {"passed": True})
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        "schema_version: 1\nupdated_at: null\nactions:\n"
        "  beadbeadbeadbead:\n"
        "    fingerprint: beadbeadbeadbead\n"
        f"    last_run_dir: {json.dumps(str(impl_run_dir))}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("usertest_implement.cli._load_runner_config", lambda _repo_root: object())

    monkeypatch.setattr(
        "usertest_implement.cli._collect_pr_review_context",
        lambda **_: {
            "pr": {
                "number": 57,
                "url": "https://example.invalid/pr/57",
                "title": "PR",
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "UNKNOWN",
                "headRefName": "backlog/review",
                "baseRefName": "dev",
            },
            "checks": [{"name": "CI", "state": "PENDING"}],
            "ci_status": "pending",
            "ci_conclusion": None,
            "changed_files": [],
            "diff_excerpt": "",
            "diff_truncated": False,
        },
    )

    try:
        _cmd_review_run(
            _review_run_args(
                repo_root=repo_root,
                owner_root=owner_root,
                ticket_path=ticket_path,
                ledger=ledger_path,
            )
        )
    except SystemExit as exc:
        assert "PR gate is green" in str(exc)
    else:
        raise AssertionError("Expected review run to refuse non-green PR gate")


def test_wait_for_ci_success_polls_view_until_completed_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True)
    monotonic_values = iter([0.0, 0.0, 1.0, 2.0, 3.0, 4.0])
    view_calls = {"count": 0}

    def _fake_run(argv, cwd=None, capture_output=None, text=None, check=None):
        assert cwd == str(workspace_dir)
        if argv[:3] == ["gh", "run", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "databaseId": 123,
                            "headSha": "abc123",
                            "event": "push",
                            "status": "queued",
                            "conclusion": "",
                            "createdAt": "2026-03-14T20:00:00Z",
                            "url": "https://example.invalid/runs/123",
                        }
                    ]
                ),
                stderr="",
            )
        if argv[:3] == ["gh", "run", "view"]:
            view_calls["count"] += 1
            payload = (
                {
                    "status": "in_progress",
                    "conclusion": "",
                    "url": "https://example.invalid/runs/123",
                }
                if view_calls["count"] == 1
                else {
                    "status": "completed",
                    "conclusion": "success",
                    "url": "https://example.invalid/runs/123",
                }
            )
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        raise AssertionError(f"unexpected subprocess call: {argv}")

    monkeypatch.setattr("usertest_implement.cli.subprocess.run", _fake_run)
    monkeypatch.setattr("usertest_implement.cli.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "usertest_implement.cli.time.monotonic",
        lambda: next(monotonic_values),
    )

    summary = implement_cli._wait_for_ci_success(
        run_dir=run_dir,
        workspace_dir=workspace_dir,
        branch="backlog/test",
        head_sha="abc123",
        workflow="CI",
        timeout_seconds=60.0,
    )

    assert summary["run_id"] == 123
    assert summary["status"] == "completed"
    assert summary["conclusion"] == "success"
    assert summary["passed"] is True
    ci_gate = _read_json(run_dir / "ci_gate.json")
    assert isinstance(ci_gate, dict)
    assert ci_gate["finished_at_utc"] is not None


def test_run_gh_text_returns_empty_string_when_stdout_missing(monkeypatch, tmp_path: Path) -> None:
    def _fake_subprocess_run(
        argv,
        cwd=None,
        capture_output=None,
        text=None,
        encoding=None,
        errors=None,
        check=None,
    ):
        assert text is True
        assert encoding == "utf-8"
        assert errors == "replace"
        return SimpleNamespace(returncode=0, stdout=None, stderr=None)

    monkeypatch.setattr("usertest_implement.cli.subprocess.run", _fake_subprocess_run)

    assert _run_gh_text(cwd=tmp_path, argv=["gh", "pr", "diff", "123"]) == ""


def test_run_gh_json_accepts_missing_stdout_as_null(monkeypatch, tmp_path: Path) -> None:
    def _fake_subprocess_run(
        argv,
        cwd=None,
        capture_output=None,
        text=None,
        encoding=None,
        errors=None,
        check=None,
    ):
        assert text is True
        assert encoding == "utf-8"
        assert errors == "replace"
        return SimpleNamespace(returncode=0, stdout=None, stderr=None)

    monkeypatch.setattr("usertest_implement.cli.subprocess.run", _fake_subprocess_run)

    assert _run_gh_json(cwd=tmp_path, argv=["gh", "pr", "view", "123", "--json", "number"]) is None


def test_collect_pr_review_context_handles_empty_diff(monkeypatch, tmp_path: Path) -> None:
    calls = {"text": 0}

    def _fake_gh_json(*, cwd: Path, argv: list[str]):
        if "checks" in argv:
            return [{"name": "CI", "state": "SUCCESS"}]
        return {
            "number": 123,
            "url": "https://example.invalid/pr/123",
            "title": "PR",
            "state": "OPEN",
            "isDraft": False,
            "headRefName": "branch",
            "baseRefName": "dev",
            "mergeable": "MERGEABLE",
        }

    def _fake_gh_text(*, cwd: Path, argv: list[str]):
        calls["text"] += 1
        if "--name-only" in argv:
            return "apps/usertest_implement/src/usertest_implement/cli.py\n"
        return ""

    monkeypatch.setattr("usertest_implement.cli._run_gh_json", _fake_gh_json)
    monkeypatch.setattr("usertest_implement.cli._run_gh_text", _fake_gh_text)

    context = _collect_pr_review_context(
        workspace_dir=tmp_path,
        pr_url="https://example.invalid/pr/123",
    )
    assert context["ci_conclusion"] == "success"
    assert context["diff_excerpt"] == ""
    assert context["diff_truncated"] is False
    assert calls["text"] == 2
