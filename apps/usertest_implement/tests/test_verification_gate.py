from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from runner_core import RunnerConfig

import usertest_implement.commands.run as run_commands
from usertest_implement.cli import build_parser
from usertest_implement.ledger import load_ledger
from usertest_implement.shared import SelectedTicket


def test_verification_failure_blocks_commit_and_returns_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "verification.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "passed": False,
                "commands": [{"index": 1, "command": "echo nope", "exit_code": 1}],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_run_once(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    def fail_if_called(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("finalize_commit must not be called when verification fails")

    monkeypatch.setattr(run_commands, "run_once", fake_run_once)
    monkeypatch.setattr(run_commands, "finalize_commit", fail_if_called)

    target_repo = tmp_path / "target_repo"
    target_repo.mkdir(parents=True, exist_ok=True)
    ticket_path = tmp_path / "ticket.md"
    ticket_path.write_text("# ticket\n", encoding="utf-8")
    ledger_path = tmp_path / "ledger.yaml"

    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--ticket-path",
            str(ticket_path),
            "--repo",
            str(target_repo),
            "--no-docker",
            "--commit",
            "--verify-command",
            "echo ok",
            "--ledger",
            str(ledger_path),
            "--no-move-on-start",
            "--no-move-on-commit",
        ]
    )

    cfg = RunnerConfig(
        repo_root=tmp_path,
        runs_dir=tmp_path / "runs",
        agents={},
        policies={},
    )
    selected = SelectedTicket(
        fingerprint="fp",
        title="Test ticket",
        export_kind="implementation",
        stage="ready_for_ticket",
        owner_root=None,
        idea_path=None,
        ticket_markdown="# ticket\n",
        tickets_export_path=None,
        export_index=None,
    )

    exit_code = run_commands._run_selected_ticket(
        args=args,
        repo_root=tmp_path,
        cfg=cfg,
        selected=selected,
    )

    assert exit_code == 2
    assert (run_dir / "ticket_ref.json").exists()
    assert (run_dir / "timing.json").exists()
    resume_state = json.loads((run_dir / "ticket_resume_state.json").read_text(encoding="utf-8"))
    assert resume_state["lifecycle_state"] == "verification_failed"
    assert resume_state["blocking_reason"] == "Verification failed: echo nope"
    ledger = load_ledger(ledger_path)
    entry = ledger["actions"]["fp"]
    assert entry["last_resume_state_path"] == str(run_dir / "ticket_resume_state.json")
    assert entry["last_resume_lifecycle_state"] == "verification_failed"

    captured = capsys.readouterr()
    assert captured.out.strip().splitlines()[-1] == str(run_dir)


def test_push_failure_resume_state_uses_resolved_branch_not_remediation_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    def fake_run_once(*_args: object, **_kwargs: object) -> object:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "workspace_ref.json").write_text(
            json.dumps({"workspace_dir": str(workspace_dir)}, indent=2) + "\n",
            encoding="utf-8",
        )
        return SimpleNamespace(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    def fake_finalize_commit(*_args: object, **_kwargs: object) -> dict[str, object]:
        git_ref = {
            "schema_version": 1,
            "commit_attempted": True,
            "commit_performed": True,
            "head_commit": "abc123",
            "error": None,
        }
        (run_dir / "git_ref.json").write_text(
            json.dumps(git_ref, indent=2) + "\n",
            encoding="utf-8",
        )
        return git_ref

    def fake_finalize_push(**kwargs: object) -> dict[str, object]:
        push_ref = {
            "schema_version": 1,
            "remote_name": kwargs["remote_name"],
            "remote_url": kwargs["remote_url"],
            "branch": kwargs["branch"],
            "pushed": False,
            "error": "network unavailable",
        }
        (run_dir / "push_ref.json").write_text(
            json.dumps(push_ref, indent=2) + "\n",
            encoding="utf-8",
        )
        return push_ref

    monkeypatch.setattr(run_commands, "run_once", fake_run_once)
    monkeypatch.setattr(run_commands, "_resolve_default_branch_name", lambda **_: "backlog/resolved")
    monkeypatch.setattr(run_commands, "finalize_commit", fake_finalize_commit)
    monkeypatch.setattr(run_commands, "finalize_push", fake_finalize_push)

    target_repo = tmp_path / "target_repo"
    target_repo.mkdir(parents=True, exist_ok=True)
    ticket_path = tmp_path / "ticket.md"
    ticket_path.write_text("# ticket\n", encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--ticket-path",
            str(ticket_path),
            "--repo",
            str(target_repo),
            "--no-docker",
            "--commit",
            "--push",
            "--remote-url",
            "https://example.invalid/repo.git",
            "--ledger",
            str(tmp_path / "ledger.yaml"),
            "--no-move-on-start",
            "--no-move-on-commit",
        ]
    )

    cfg = RunnerConfig(
        repo_root=tmp_path,
        runs_dir=tmp_path / "runs",
        agents={},
        policies={},
    )
    selected = SelectedTicket(
        fingerprint="fp",
        title="Test ticket",
        export_kind="implementation",
        stage="ready_for_ticket",
        owner_root=None,
        idea_path=None,
        ticket_markdown="# ticket\n",
        tickets_export_path=None,
        export_index=None,
    )

    assert run_commands._run_selected_ticket(
        args=args,
        repo_root=tmp_path,
        cfg=cfg,
        selected=selected,
    ) == 4

    resume_state = json.loads((run_dir / "ticket_resume_state.json").read_text(encoding="utf-8"))
    assert resume_state["lifecycle_state"] == "push_failed"
    assert resume_state["branch"] == "backlog/resolved"
    assert resume_state["branch"] != "<branch>"
