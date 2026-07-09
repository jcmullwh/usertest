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
    _write_json(run_dir / "target_ref.json", {"repo_input": str(tmp_path / "remote.git")})
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
