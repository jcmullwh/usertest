from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from runner_core import RunnerConfig
from runner_core.retained_oracle_assets import _sha256_json

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


def _attach_retained_transport(run_dir: Path, tmp_path: Path) -> tuple[Path, dict[str, object]]:
    runs_root = tmp_path / "evidence" / "runs"
    replay = (
        runs_root
        / "research"
        / "asset"
        / "bundle"
        / ".usertest_research"
        / "replay.py"
    )
    replay.parent.mkdir(parents=True)
    replay.write_text("print('retained replay passed')\n", encoding="utf-8")
    manifest = {
        ".usertest_research/replay.py": {
            "kind": "file",
            "mode": stat.S_IMODE(replay.stat().st_mode),
            "sha256": hashlib.sha256(replay.read_bytes()).hexdigest(),
            "size_bytes": replay.stat().st_size,
        }
    }
    asset = {
        "asset_id": "outcome_asset:"
        + _sha256_json({"schema_version": 1, "manifest": manifest}),
        "runs_relative_path": "research/asset/bundle",
        "manifest": manifest,
        "manifest_sha256": _sha256_json(manifest),
    }
    projection = {
        "schema_version": 1,
        "role": "original_scenario",
        "outcome_oracle_id": "outcome_oracle:" + "a" * 64,
        "oracle_kind": "staged_replay",
        "oracle_repo_revision": "b" * 40,
        "asset": asset,
    }
    spec: dict[str, object] = {
        **projection,
        "transport_sha256": _sha256_json(projection),
    }
    ticket_ref_path = run_dir / "ticket_ref.json"
    ticket_ref = json.loads(ticket_ref_path.read_text(encoding="utf-8"))
    ticket_ref["supervisor_instruction"] = (
        "Do not invoke Docker.\n\nDo not delete, move, prune, or clean up files."
    )
    ticket_ref["retained_oracle_asset_transport"] = {
        "trusted_runs_root": str(runs_root),
        "spec": spec,
    }
    ticket_ref["ticket_provenance"] = {
        "target_contract": {"repo_revision": "b" * 40}
    }
    _write_json(ticket_ref_path, ticket_ref)
    return runs_root, spec


def _mark_implemented_local(run_dir: Path) -> None:
    _write_json(
        run_dir / "verification.json",
        {
            "schema_version": 1,
            "passed": True,
            "terminal_reason": "passed",
            "commands": [
                {
                    "index": 1,
                    "command": "python -m pytest tests/test_resume.py",
                    "exit_code": 0,
                }
            ],
        },
    )
    state_path = run_dir / "ticket_resume_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["lifecycle_state"] = "implemented_local"
    state["blocking_reason"] = None
    _write_json(state_path, state)


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
        payload["run_request"]["codex_resume_session_id"] == "019f5000-0000-7000-8000-000000000002"
    )
    assert payload["implementation_author_continuity"]["status"] == "exact_author_session"
    assert payload["run_request"]["verification_reuse_mode"] == "off"
    assert payload["run_request"]["verification_reuse_forced_reason"] == (
        "same_workspace_resume_fresh_gate"
    )
    prompt = payload["prompt"]
    assert "Do not restart the original full ticket prompt from scratch" in prompt
    assert "verification.json" in prompt
    assert "verification_reuse.json" in prompt
    assert "agent_attempts.json" in prompt
    assert "Prior report output" in prompt
    assert "python -m pytest tests/test_resume.py" in prompt


def test_implemented_local_resume_accepts_only_retained_author_session_and_workspace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, workspace, _ = _make_resume_run(tmp_path)
    _mark_implemented_local(run_dir)
    runs_root, spec = _attach_retained_transport(run_dir, tmp_path)
    correction = "The shallow checks passed, but correct the unresolved semantic behavior."
    parser = build_parser()
    args = parser.parse_args(
        [
            "--repo-root",
            str(tmp_path),
            "resume",
            "--run-dir",
            str(run_dir),
            "--no-docker",
            "--commit",
            "--supervisor-instruction",
            correction,
            "--dry-run",
        ]
    )

    assert resume_commands._cmd_resume(args) == 0
    payload = json.loads(capsys.readouterr().out)
    request = payload["run_request"]
    assert payload["workspace_strategy"] == "same_workspace"
    assert request["resume_workspace_dir"] == str(workspace)
    assert request["codex_resume_session_id"] == (
        "019f5000-0000-7000-8000-000000000002"
    )
    assert payload["implementation_author_continuity"]["fresh_restart"] is False
    assert request["commit"] is True
    assert request["verification_reuse_mode"] == "off"
    assert request["retained_oracle_asset_transport"]["trusted_runs_root"] == str(
        runs_root.resolve()
    )
    assert request["retained_oracle_asset_transport"]["transport_sha256"] == spec[
        "transport_sha256"
    ]
    prompt = payload["prompt"]
    assert "semantic supervisor correction" in prompt
    assert "recorded verification passed" in prompt
    assert "required verification checks failed" not in prompt
    assert "Do not invoke Docker." in prompt
    assert "Do not delete, move, prune, or clean up files." in prompt
    assert correction in prompt


def test_implemented_local_resume_rejects_unavailable_author_session(
    tmp_path: Path,
) -> None:
    run_dir, _, _ = _make_resume_run(tmp_path)
    _mark_implemented_local(run_dir)
    (run_dir / "raw_events.jsonl").write_text(
        json.dumps({"type": "turn.started"}) + "\n",
        encoding="utf-8",
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
        ]
    )

    with pytest.raises(SystemExit, match="exact Codex author session"):
        resume_commands._cmd_resume(args)


def test_implemented_local_resume_rejects_missing_workspace_even_with_fallback(
    tmp_path: Path,
) -> None:
    run_dir, _, _ = _make_resume_run(tmp_path, workspace_exists=False)
    _mark_implemented_local(run_dir)
    fallback_repo = tmp_path / "fallback_repo"
    fallback_repo.mkdir()
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

    with pytest.raises(SystemExit, match="fresh checkout/restart is not allowed"):
        resume_commands._cmd_resume(args)


def test_resume_dry_run_preserves_constraints_assets_and_output_routing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, _, _ = _make_resume_run(tmp_path)
    runs_root, spec = _attach_retained_transport(run_dir, tmp_path)
    output_runs = tmp_path / "external-output" / "runs"
    permission_correction = (
        "The recorded preflight proves sandbox_mode=workspace-write and "
        "policy_status=allowed, and the Codex argv used --sandbox workspace-write. "
        "A complex quoted PowerShell command was rejected while Get-Content succeeded. "
        "Make a real tracked-file write attempt before claiming the workspace is read-only."
    )
    parser = build_parser()
    args = parser.parse_args(
        [
            "--repo-root",
            str(tmp_path),
            "resume",
            "--run-dir",
            str(run_dir),
            "--runs-dir",
            str(output_runs),
            "--no-docker",
            "--commit",
            "--supervisor-instruction",
            permission_correction,
            "--dry-run",
        ]
    )

    assert resume_commands._cmd_resume(args) == 0
    payload = json.loads(capsys.readouterr().out)
    request = payload["run_request"]
    assert request["codex_resume_session_id"] == (
        "019f5000-0000-7000-8000-000000000002"
    )
    assert request["verification_reuse_mode"] == "off"
    assert request["verification_reuse_forced_reason"] == (
        "retained_oracle_asset_server_staging"
    )
    assert request["runs_dir"] == str(output_runs.resolve())
    assert request["commit"] is True
    assert request["retained_oracle_asset_transport"] == {
        "trusted_runs_root": str(runs_root.resolve()),
        "transport_sha256": spec["transport_sha256"],
        "outcome_oracle_id": spec["outcome_oracle_id"],
        "oracle_kind": "staged_replay",
        "asset_id": spec["asset"]["asset_id"],
        "runs_relative_path": "research/asset/bundle",
        "manifest_sha256": spec["asset"]["manifest_sha256"],
        "manifest_entry_count": 1,
    }
    assert "manifest" not in request["retained_oracle_asset_transport"]
    prompt = payload["prompt"]
    assert "Do not invoke Docker." in prompt
    assert "Do not delete, move, prune, or clean up files." in prompt
    assert permission_correction in prompt
    assert prompt.index("Do not invoke Docker.") < prompt.index(permission_correction)


def test_resume_carries_asset_and_constraints_then_commits_passing_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, workspace, _ = _make_resume_run(tmp_path)
    runs_root, spec = _attach_retained_transport(run_dir, tmp_path)
    output_runs = tmp_path / "external-output" / "runs"
    resumed_run = tmp_path / "resumed_run"
    permission_correction = (
        "Preflight and argv prove workspace-write. Make a real tracked-file write "
        "attempt before claiming read-only."
    )
    seen: dict[str, object] = {}

    def fake_load_runner_config(
        repo_root: Path, *, runs_dir: Path | None = None
    ) -> RunnerConfig:
        seen["configured_runs_dir"] = runs_dir
        return RunnerConfig(
            repo_root=repo_root,
            runs_dir=runs_dir or tmp_path / "default-runs",
            agents={},
            policies={},
        )

    def fake_run_once(cfg: RunnerConfig, request: object) -> object:
        seen["cfg"] = cfg
        seen["request"] = request
        _write_json(resumed_run / "workspace_ref.json", {"workspace_dir": str(workspace)})
        _write_json(resumed_run / "verification.json", {"passed": True, "commands": []})
        _write_json(resumed_run / "target_ref.json", {"commit_sha": "b" * 40})
        return SimpleNamespace(run_dir=resumed_run, exit_code=0, report_validation_errors=[])

    def fake_finalize_commit(**kwargs: object) -> dict[str, object]:
        seen["commit_kwargs"] = kwargs
        receipt: dict[str, object] = {
            "schema_version": 1,
            "branch": kwargs["branch"],
            "commit_attempted": True,
            "commit_performed": True,
            "head_commit": "c" * 40,
            "base_commit": "b" * 40,
            "error": None,
        }
        _write_json(Path(str(kwargs["run_dir"])) / "git_ref.json", receipt)
        return receipt

    def fake_record_verified_head(**kwargs: object) -> dict[str, object]:
        seen["record_kwargs"] = kwargs
        return {"verified_implementation_head": "c" * 40}

    monkeypatch.setattr(resume_commands, "_load_runner_config", fake_load_runner_config)
    monkeypatch.setattr(resume_commands, "run_once", fake_run_once)
    monkeypatch.setattr(resume_commands, "finalize_commit", fake_finalize_commit)
    monkeypatch.setattr(
        resume_commands,
        "record_verified_implementation_head",
        fake_record_verified_head,
    )
    parser = build_parser()
    args = parser.parse_args(
        [
            "--repo-root",
            str(tmp_path),
            "resume",
            "--run-dir",
            str(run_dir),
            "--runs-dir",
            str(output_runs),
            "--no-docker",
            "--commit",
            "--supervisor-instruction",
            permission_correction,
        ]
    )

    assert resume_commands._cmd_resume(args) == 0
    request = seen["request"]
    assert request.codex_resume_session_id == "019f5000-0000-7000-8000-000000000002"
    assert request.codex_resume_usage_source_run_dir == run_dir
    assert request.agent_user_prompt is not None
    assert "Do not invoke Docker." in request.agent_user_prompt
    assert permission_correction in request.agent_user_prompt
    assert request.supervisor_instruction == (
        "Do not invoke Docker.\n\nDo not delete, move, prune, or clean up files."
        f"\n\n{permission_correction}"
    )
    assert request.retained_oracle_assets_root == runs_root.resolve()
    assert request.retained_oracle_asset_spec == spec
    assert request.verification_reuse_mode == "off"
    assert seen["configured_runs_dir"] == output_runs.resolve()
    assert seen["record_kwargs"] == {
        "run_dir": resumed_run,
        "require_exact_base": False,
    }
    git_ref = json.loads((resumed_run / "git_ref.json").read_text(encoding="utf-8"))
    assert git_ref["commit_performed"] is True
    handoff = json.loads((resumed_run / "handoff_summary.json").read_text(encoding="utf-8"))
    assert handoff["commit_requested"] is True
    assert handoff["commit_performed"] is True
    assert handoff["push_requested"] is False
    assert handoff["pr_requested"] is False
    resumed_ticket = json.loads((resumed_run / "ticket_ref.json").read_text(encoding="utf-8"))
    assert resumed_ticket["retained_oracle_asset_transport"]["spec"] == spec
    assert permission_correction in resumed_ticket["supervisor_instruction"]


def test_resume_binds_passing_verification_to_existing_clean_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, workspace, _ = _make_resume_run(tmp_path)
    _attach_retained_transport(run_dir, tmp_path)
    resumed_run = tmp_path / "resumed_run"
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        resume_commands,
        "_load_runner_config",
        lambda repo_root, *, runs_dir=None: RunnerConfig(
            repo_root=repo_root,
            runs_dir=runs_dir or tmp_path / "runs",
            agents={},
            policies={},
        ),
    )

    def fake_run_once(_cfg: RunnerConfig, _request: object) -> object:
        _write_json(resumed_run / "workspace_ref.json", {"workspace_dir": str(workspace)})
        _write_json(resumed_run / "verification.json", {"passed": True, "commands": []})
        _write_json(resumed_run / "target_ref.json", {"commit_sha": "b" * 40})
        return SimpleNamespace(run_dir=resumed_run, exit_code=0, report_validation_errors=[])

    def fake_finalize_commit(**kwargs: object) -> dict[str, object]:
        receipt: dict[str, object] = {
            "schema_version": 1,
            "branch": kwargs["branch"],
            "commit_attempted": True,
            "commit_performed": False,
            "commit_observed": True,
            "head_commit": "c" * 40,
            "base_commit": "c" * 40,
            "error": None,
        }
        _write_json(Path(str(kwargs["run_dir"])) / "git_ref.json", receipt)
        return receipt

    monkeypatch.setattr(resume_commands, "run_once", fake_run_once)
    monkeypatch.setattr(resume_commands, "finalize_commit", fake_finalize_commit)
    monkeypatch.setattr(
        resume_commands,
        "record_verified_implementation_head",
        lambda **_kwargs: pytest.fail("a new commit was not performed"),
    )
    monkeypatch.setattr(
        resume_commands,
        "record_existing_verified_implementation_head",
        lambda **kwargs: seen.setdefault("existing_record_kwargs", kwargs),
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
            "--commit",
        ]
    )

    assert resume_commands._cmd_resume(args) == 0
    assert seen["existing_record_kwargs"] == {"run_dir": resumed_run}
    handoff = json.loads((resumed_run / "handoff_summary.json").read_text(encoding="utf-8"))
    assert handoff["commit_requested"] is True
    assert handoff["commit_performed"] is False


def test_resume_uses_same_workspace_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir, workspace, _ = _make_resume_run(tmp_path)
    author_run = tmp_path / "adopted_author_run"
    author_run.mkdir()
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
        "_resume_agent_continuity",
        lambda **_: (
            "codex",
            "019f5000-0000-7000-8000-000000000002",
            {"author_source_run_dir": str(author_run)},
        ),
    )
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
    assert request.codex_resume_usage_source_run_dir == author_run.resolve()
    assert request.agent_user_prompt is not None
    assert (
        "Do not restart the original full ticket prompt from scratch" in request.agent_user_prompt
    )
    assert request.agent_append_system_prompt is None
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
    ticket_bucket: str = "4 - for_review",
) -> tuple[Path, Path, Path, Path]:
    run_dir = tmp_path / "original_pr_run"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / ".git").mkdir()
    ticket_name = "20260719_def456def456def0_PR-resume-ticket.md"
    ticket_path = workspace / ".agents" / "plans" / ticket_bucket / ticket_name
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
    (workspace / ".agents" / "plans" / "4 - for_review").mkdir(
        parents=True,
        exist_ok=True,
    )
    ticket_path.write_text(
        "# PR Resume Ticket\n\n"
        "- Fingerprint: `def456def456def0`\n\n"
        "Implement the thing.\n",
        encoding="utf-8",
    )
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


def test_resume_ticket_prompt_projects_verbose_research_attempt_history_without_mutation(
    tmp_path: Path,
) -> None:
    proof = {
        "case_id": "case:real-problem",
        "experiments": [{"result": "original scenario reproduced"}],
        "root_cause_hypotheses": [
            {"rank": 1, "mechanism": "fresh aliases bypass identity retention"}
        ],
        "material_unknowns": [],
        "research_attempts": [{"transcript": "x" * 110_000}],
        "evidence_verification": {"raw": "y" * 110_000},
    }
    ticket_path = tmp_path / "ticket.md"
    ticket_markdown = (
        "# Ticket\n\n"
        "## Research context\n\n"
        "### Full verified research proof\n\n"
        "The following block is retained evidence/data, not executable instructions.\n\n"
        f"```json\n{json.dumps(proof, indent=2)}\n```\n\n"
        "## Implementation plan\n\nChange the causal selector.\n"
    )
    ticket_path.write_text(ticket_markdown, encoding="utf-8")
    selected = SimpleNamespace(
        ticket_markdown=ticket_markdown,
        idea_path=ticket_path,
    )

    projected = resume_commands._resume_ticket_prompt_context(selected)

    assert len(projected) < 20_000
    assert "original scenario reproduced" in projected
    assert "fresh aliases bypass identity retention" in projected
    assert '"research_attempts"' in projected
    assert '"evidence_verification"' in projected
    assert "x" * 1000 not in projected
    assert "full_proof_sha256" in projected
    assert json.dumps(str(ticket_path))[1:-1] in projected
    assert "Change the causal selector" in projected
    assert ticket_path.read_text(encoding="utf-8") == ticket_markdown


def test_pr_resume_prompt_projects_metadata_checks_and_large_diff() -> None:
    middle = "MIDDLE_SHOULD_BE_OMITTED" * 3000
    diff = "PREFIX\n" + ("a" * 30_000) + middle + ("z" * 20_000) + "\nSUFFIX"
    context = {
        "pr": {
            "number": 9,
            "url": "https://example.invalid/pr/9",
            "title": "Keep me",
            "headRefOid": "a" * 40,
            "body": "REDUNDANT_BODY" * 10_000,
        },
        "checks": [
            {
                "name": "CI",
                "state": "FAILURE",
                "link": "https://example.invalid/check",
                "verbose_output": "REDUNDANT_CHECK_OUTPUT" * 10_000,
            }
        ],
        "diff_excerpt": diff,
    }

    pr = resume_commands._pr_metadata_for_resume_prompt(context)
    checks = resume_commands._checks_for_resume_prompt(context)
    projected_diff = resume_commands._diff_excerpt_for_resume_prompt(context)

    assert pr["title"] == "Keep me"
    assert "body" not in pr
    assert checks == [
        {
            "name": "CI",
            "state": "FAILURE",
            "link": "https://example.invalid/check",
        }
    ]
    assert "verbose_output" not in checks[0]
    assert len(projected_diff) < 41_000
    assert "MIDDLE_SHOULD_BE_OMITTED" not in projected_diff
    assert "full_diff_sha256=" in projected_diff
    assert projected_diff.startswith("PREFIX")
    assert projected_diff.endswith("SUFFIX")


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
        payload["run_request"]["codex_resume_session_id"] == "019f5000-0000-7000-8000-000000000003"
    )
    assert payload["implementation_author_continuity"]["fresh_restart"] is False
    assert payload["failing_check_pointers"][0]["link"] == "https://example.invalid/runs/current"
    prompt = payload["prompt"]
    assert "Current PR metadata (refreshed immediately before this prompt)" in prompt
    assert "PR title from refreshed state" in prompt
    assert "Edge case broken" in prompt
    assert "Run artifact paths" in prompt
    assert "backlog/current-pr-branch" in prompt


def test_pr_resume_forwards_supervisor_delta_and_omits_nonblocking_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, _, _, review_run_dir = _make_pr_resume_run(tmp_path)
    summary_path = review_run_dir / "review_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["findings"].append(
        {
            "severity": "warn",
            "title": "Live proof remains bounded",
            "details": "Do not turn this context boundary into implementation work.",
        }
    )
    _write_json(summary_path, summary)
    monkeypatch.setattr(
        resume_commands,
        "_collect_pr_review_context",
        lambda **_: _pr_context(),
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
            "--supervisor-instruction",
            "FOCUSED_DELTA_ONLY",
        ]
    )

    assert resume_commands._cmd_resume(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["supervisor_instructions"] == ["FOCUSED_DELTA_ONLY"]
    assert [item["title"] for item in payload["unresolved_review_findings"]] == [
        "Edge case broken"
    ]
    assert "FOCUSED_DELTA_ONLY" in payload["prompt"]
    assert "Edge case broken" in payload["prompt"]
    assert "Live proof remains bounded" not in payload["prompt"]
    assert "Do not turn this context boundary" not in payload["prompt"]


def test_pr_resume_restarts_only_after_retained_same_author_context_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, workspace, _, _ = _make_pr_resume_run(tmp_path)
    exhausted_session = "019f5000-0000-7000-8000-000000000003"
    failed_run = tmp_path / "failed_context_resume"
    _write_json(
        failed_run / "target_ref.json",
        {
            "agent": "codex",
            "requested_codex_resume_session_id": exhausted_session,
        },
    )
    _write_json(failed_run / "workspace_ref.json", {"workspace_dir": str(workspace)})
    _write_json(
        failed_run / "agent_attempts.json",
        {
            "attempts": [
                {
                    "exit_code": 1,
                    "continued_session": True,
                    "agent_session_id": exhausted_session,
                }
            ]
        },
    )
    _write_json(
        failed_run / "verification.json",
        {"status": "skipped_agent_failed", "passed": None},
    )
    _write_json(
        failed_run / "ticket_resume_state.json",
        {"lifecycle_state": "agent_failed", "run_dir": str(failed_run)},
    )
    message = (
        "Codex ran out of room in the model's context window. "
        "Start a new thread or clear earlier history before retrying."
    )
    (failed_run / "raw_events.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": exhausted_session}),
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "error", "message": message}),
                json.dumps({"type": "turn.failed", "error": {"message": message}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    source_state_path = run_dir / "ticket_resume_state.json"
    source_state = json.loads(source_state_path.read_text(encoding="utf-8"))
    source_state["resume_attempts"] = [{"run_dir": str(failed_run)}]
    _write_json(source_state_path, source_state)
    monkeypatch.setattr(
        resume_commands,
        "_collect_pr_review_context",
        lambda **_: _pr_context(),
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

    assert payload["run_request"]["codex_resume_session_id"] is None
    continuity = payload["implementation_author_continuity"]
    assert continuity["fresh_restart"] is True
    assert continuity["restart_justified"] is True
    assert continuity["exhausted_session_id"] == exhausted_session
    assert continuity["restart_evidence"]["reason"] == (
        "codex_author_context_window_exhausted"
    )
    assert continuity["restart_evidence"]["frontier_mutation_events_observed"] is False
    assert "justified fresh-author continuation" in payload["prompt"]


def test_pr_resume_previously_merge_ready_prompt_includes_new_failing_check_pointers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, _, _, _ = _make_pr_resume_run(
        tmp_path, lifecycle_state="merge_ready", review_decision="approved"
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


def test_pr_resume_approved_pending_state_can_correct_later_terminal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir, _, _, _ = _make_pr_resume_run(
        tmp_path,
        lifecycle_state="awaiting_review",
        review_decision="approved",
    )
    monkeypatch.setattr(
        resume_commands,
        "_collect_pr_review_context",
        lambda **_: _pr_context(check_state="FAILURE"),
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
    assert payload["resume_kind"] == "pr"
    assert payload["failing_check_pointers"][0]["state"] == "FAILURE"
    assert "Lifecycle state: awaiting_review" in payload["prompt"]


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
        tmp_path,
        lifecycle_state="ci_failed",
        review_decision="approved",
        ticket_bucket="3 - in_progress",
    )
    ticket_ref = json.loads((run_dir / "ticket_ref.json").read_text(encoding="utf-8"))
    ticket_ref["ticket_provenance"] = {"target_contract": {"schema_version": 1}}
    _write_json(run_dir / "ticket_ref.json", ticket_ref)
    resumed_run = tmp_path / "resumed_pr_run"
    author_run = tmp_path / "adopted_pr_author_run"
    author_run.mkdir()
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
            {
                "branch": kwargs["branch"],
                "commit_performed": False,
                "commit_observed": True,
                "head_commit": "abc",
            },
        )
        return {
            "branch": kwargs["branch"],
            "commit_performed": False,
            "commit_observed": True,
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
    monkeypatch.setattr(
        resume_commands,
        "_resume_agent_continuity",
        lambda **_: (
            "codex",
            "019f5000-0000-7000-8000-000000000003",
            {"author_source_run_dir": str(author_run)},
        ),
    )
    monkeypatch.setattr(resume_commands, "finalize_commit", fake_commit)
    monkeypatch.setattr(resume_commands, "finalize_push", fake_push)
    monkeypatch.setattr(
        resume_commands,
        "record_existing_verified_implementation_head",
        lambda **kwargs: seen.setdefault("existing_verified_head", kwargs) or {},
    )
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
        [
            "--repo-root",
            str(tmp_path),
            "resume",
            "--run-dir",
            str(run_dir),
            "--no-docker",
            "--correction-origin",
            "system_self_correction",
        ]
    )

    assert resume_commands._cmd_resume(args) == 0
    request = seen["request"]
    assert request.ref == "backlog/current-pr-branch"
    assert request.keep_workspace is True
    assert request.agent == "codex"
    assert request.codex_resume_session_id == "019f5000-0000-7000-8000-000000000003"
    assert request.codex_resume_usage_source_run_dir == author_run.resolve()
    assert request.agent_user_prompt is not None
    assert "Current PR metadata" in request.agent_user_prompt
    assert request.agent_append_system_prompt is None
    assert request.exec_use_host_agent_login is True
    assert seen["commit"]["branch"] == "backlog/current-pr-branch"
    assert seen["push"]["branch"] == "backlog/current-pr-branch"
    assert seen["existing_verified_head"]["run_dir"] == resumed_run
    pr_ref = json.loads((resumed_run / "pr_ref.json").read_text(encoding="utf-8"))
    assert pr_ref["existing_pr"] is True
    assert pr_ref["created"] is True
    assert pr_ref["requested"] is False
    handoff = json.loads((resumed_run / "handoff_summary.json").read_text(encoding="utf-8"))
    assert handoff["pr_created"] is True
    assert handoff["review_required"] is True
    resumed_state = json.loads(
        (resumed_run / "ticket_resume_state.json").read_text(encoding="utf-8")
    )
    resumed_ticket_path = Path(resumed_state["ticket"]["path"])
    assert resumed_state["lifecycle_state"] == "awaiting_review"
    assert resumed_ticket_path.parent.name == "4 - for_review"
    assert resumed_ticket_path.is_file()
    assert not (
        workspace
        / ".agents"
        / "plans"
        / "3 - in_progress"
        / "20260719_def456def456def0_PR-resume-ticket.md"
    ).exists()
    resume_ref = json.loads((resumed_run / "resume_ref.json").read_text(encoding="utf-8"))
    assert resume_ref["correction_origin"] == "system_self_correction"
