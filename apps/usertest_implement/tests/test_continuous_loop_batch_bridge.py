from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_module():
    module_path = (
        Path(__file__).resolve().parents[3] / "tools" / "continuous_implement_loop.py"
    )
    spec = importlib.util.spec_from_file_location("continuous_implement_loop", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_run_batch_pass_invokes_usertest_implement_batch_run(tmp_path: Path) -> None:
    mod = _load_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    ctx = mod.LoopContext(
        repo_root=repo_root,
        owner_root=repo_root,
        runs_dir=repo_root / "runs" / "usertest_implement",
        target="usertest",
        repo_input=str(repo_root),
        settings_path=repo_root / "configs" / "usertest_implement_settings.yaml",
        settings_profile="default",
        backlog_agent="codex",
        backlog_model="gpt-5.5",
        implementation_agent="codex",
        implementation_model=None,
        review_agent="claude",
        review_model=None,
        allowed_severities={"blocker", "high"},
        cleanup_interval_seconds=21600.0,
        log_path=repo_root / "runs" / "_continuous_loop" / "continuous_loop.log",
        state_path=repo_root / "runs" / "_continuous_loop" / "loop_state.json",
        pid_path=repo_root / "runs" / "_continuous_loop" / "loop.pid",
        batch_config_path=repo_root / "configs" / "backlog_implement_batch.yaml",
        implement_python=(
            repo_root / "apps" / "usertest_implement" / ".venv" / "Scripts" / "python.exe"
        ),
        backlog_python=(
            repo_root / "apps" / "usertest_backlog" / ".venv" / "Scripts" / "python.exe"
        ),
    )
    captured: dict[str, object] = {}

    def _fake_write_state(*args, **kwargs):
        captured["state"] = kwargs

    def _fake_run_logged(*args, **kwargs):
        captured["argv"] = args[1]
        captured["label"] = kwargs["label"]
        return SimpleNamespace(returncode=0)

    mod._write_state = _fake_write_state
    mod._run_logged = _fake_run_logged

    assert mod._run_batch_pass(ctx) is True
    assert captured["label"] == "batch run"
    assert captured["argv"] == [
        str(ctx.implement_python),
        "-m",
        "usertest_implement.cli",
        "--repo-root",
        str(repo_root),
        "batch",
        "run",
        "--config",
        str(ctx.batch_config_path),
    ]


def test_latest_terminal_proof_is_hash_verified_before_loop_stops(tmp_path: Path) -> None:
    mod = _load_module()
    owner_root = tmp_path / "owner"
    batch = (
        owner_root
        / "runs"
        / "_batch"
        / "usertest_implement"
        / "20260710T120000Z"
    )
    batch.mkdir(parents=True)
    proof = {
        "schema_version": 1,
        "passed": True,
        "reasons": [],
        "wave_base_revision": "a" * 40,
    }
    proof["proof_sha256"] = hashlib.sha256(
        json.dumps(
            proof,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    proof_path = batch / "terminal_proof.json"
    proof_path.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    payload_sha = hashlib.sha256(proof_path.read_bytes()).hexdigest()
    (batch / "batch_state.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "terminal_proof": {
                    "path": str(proof_path),
                    "sha256": payload_sha,
                    "proof_sha256": proof["proof_sha256"],
                    "passed": True,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = SimpleNamespace(owner_root=owner_root)

    assert mod._latest_passing_terminal_proof(ctx) == proof

    proof_path.write_text(
        proof_path.read_text(encoding="utf-8").replace('"passed": true', '"passed": false'),
        encoding="utf-8",
    )
    assert mod._latest_passing_terminal_proof(ctx) is None


def test_failed_premerge_original_scenario_resumes_same_pr_instead_of_stalling(
    tmp_path: Path,
) -> None:
    mod = _load_module()
    ctx = SimpleNamespace(
        implement_python=tmp_path / "python.exe",
        repo_root=tmp_path / "tool",
        owner_root=tmp_path / "owner",
        repo_input=str(tmp_path / "owner"),
        implementation_agent="codex",
        implementation_model="gpt-5.5",
    )
    fingerprint = "0123456789abcdef"
    implementation_run = tmp_path / "owner" / "runs" / "implementation" / "one"
    mod._load_ledger = lambda _ctx: {
        "actions": {
            fingerprint: {
                "last_run_dir": str(implementation_run),
                "last_resume_lifecycle_state": "review_changes_requested",
            }
        }
    }
    calls: list[tuple[str, list[str]]] = []

    def _fake_run_logged(_ctx, argv, **kwargs):
        calls.append((kwargs["label"], list(argv)))
        return SimpleNamespace(returncode=4 if len(calls) == 1 else 0)

    mod._run_logged = _fake_run_logged

    assert mod._merge_review(ctx, fingerprint) is True
    assert [label for label, _argv in calls] == [
        f"review merge {fingerprint}",
        f"resume failed original scenario {fingerprint}",
    ]
    resume_argv = calls[1][1]
    assert resume_argv[resume_argv.index("resume") + 1 : resume_argv.index("--repo")] == [
        "--run-dir",
        str(implementation_run),
    ]
    assert "--model" in resume_argv
    assert "--verify-timeout-seconds" not in resume_argv


def test_append_log_ignores_oserror_from_stderr(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    ctx = mod.LoopContext(
        repo_root=repo_root,
        owner_root=repo_root,
        runs_dir=repo_root / "runs" / "usertest_implement",
        target="usertest",
        repo_input=str(repo_root),
        settings_path=repo_root / "configs" / "usertest_implement_settings.yaml",
        settings_profile="default",
        backlog_agent="codex",
        backlog_model="gpt-5.5",
        implementation_agent="codex",
        implementation_model=None,
        review_agent="claude",
        review_model=None,
        allowed_severities={"blocker", "high"},
        cleanup_interval_seconds=21600.0,
        log_path=repo_root / "runs" / "_continuous_loop" / "continuous_loop.log",
        state_path=repo_root / "runs" / "_continuous_loop" / "loop_state.json",
        pid_path=repo_root / "runs" / "_continuous_loop" / "loop.pid",
        batch_config_path=repo_root / "configs" / "backlog_implement_batch.yaml",
        implement_python=(
            repo_root / "apps" / "usertest_implement" / ".venv" / "Scripts" / "python.exe"
        ),
        backlog_python=(
            repo_root / "apps" / "usertest_backlog" / ".venv" / "Scripts" / "python.exe"
        ),
    )

    class BrokenStderr:
        def write(self, text: str) -> int:
            raise OSError(22, "Invalid argument")

        def flush(self) -> None:
            raise AssertionError("flush should not be called after write failure")

    monkeypatch.setattr(mod.sys, "stderr", BrokenStderr())

    mod._append_log(ctx, "background-safe log write")

    assert "background-safe log write" in ctx.log_path.read_text(encoding="utf-8")


def test_merged_nonterminal_outcome_retries_without_blocking_unrelated_refresh(
    tmp_path: Path,
) -> None:
    mod = _load_module()
    completed = tmp_path / ".agents" / "plans" / "5 - complete" / "plan.md"
    completed.parent.mkdir(parents=True)
    completed.write_text(
        "Generated by `python -m usertest_backlog.cli reports export-tickets`.\n",
        encoding="utf-8",
    )
    attempts: list[str] = []
    logs: list[str] = []
    ctx = SimpleNamespace()
    mod._load_ledger = lambda _ctx: {
        "actions": {
            "0123456789abcdef": {
                "last_pr_url": "https://example.invalid/pr/1",
                "last_review_pr_url": "https://example.invalid/pr/1",
                "last_review_merge_ready": True,
                "last_merge_pr_url": "https://example.invalid/pr/1",
                "last_merged_at": "2026-07-10T12:00:00Z",
                "last_outcome_state": "tests_verified",
            }
        }
    }
    mod._find_ticket_path = lambda _ctx, _fingerprint: completed
    mod._gh_pr_view = lambda _ctx, _url: {
        "state": "MERGED",
        "mergedAt": "2026-07-10T12:00:00Z",
    }
    mod._merge_review = lambda _ctx, fingerprint: (
        attempts.append(fingerprint) or False
    )
    mod._append_log = lambda _ctx, message: logs.append(message)

    assert mod._reconcile_review_queue(ctx) is True
    assert attempts == ["0123456789abcdef"]
    assert any("case-locally pending" in message for message in logs)


def test_resolved_merged_outcome_does_not_rerun_finalizer(tmp_path: Path) -> None:
    mod = _load_module()
    completed = tmp_path / ".agents" / "plans" / "5 - complete" / "plan.md"
    completed.parent.mkdir(parents=True)
    completed.write_text(
        "Generated by `python -m usertest_backlog.cli reports export-tickets`.\n",
        encoding="utf-8",
    )
    attempts: list[str] = []
    ctx = SimpleNamespace()
    mod._load_ledger = lambda _ctx: {
        "actions": {
            "0123456789abcdef": {
                "last_pr_url": "https://example.invalid/pr/1",
                "last_merge_pr_url": "https://example.invalid/pr/1",
                "last_merged_at": "2026-07-10T12:00:00Z",
                "last_outcome_state": "resolved",
            }
        }
    }
    mod._find_ticket_path = lambda _ctx, _fingerprint: completed
    mod._gh_pr_view = lambda *_args: (_ for _ in ()).throw(
        AssertionError("terminal outcome should skip GitHub polling")
    )
    mod._merge_review = lambda _ctx, fingerprint: attempts.append(fingerprint) or True

    assert mod._reconcile_review_queue(ctx) is True
    assert attempts == []


def test_idea_pull_request_is_ignored_by_automated_review_reconciliation(
    tmp_path: Path,
) -> None:
    mod = _load_module()
    idea = tmp_path / ".agents" / "plans" / "4 - for_review" / "idea.md"
    idea.parent.mkdir(parents=True)
    idea.write_text(
        "\n".join(
            [
                "# IDEA-005 Ticket 04",
                "",
                "Generated from tracked idea plan `.agents/plans/0 - roadmaps/IDEA-005.md`.",
                "- Export kind: `implementation`",
                "- Stage: `ready_for_ticket`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[str] = []
    ctx = SimpleNamespace()
    mod._load_ledger = lambda _ctx: {
        "actions": {
            "650b207f62033355": {
                "last_pr_url": "https://example.invalid/pr/212",
                "last_review_merge_ready": True,
            }
        }
    }
    mod._find_ticket_path = lambda _ctx, _fingerprint: idea
    mod._gh_pr_view = lambda *_args: calls.append("view") or {"state": "OPEN"}
    mod._run_review = lambda *_args: calls.append("review") or True
    mod._merge_review = lambda *_args: calls.append("merge") or True
    mod._move_ticket = lambda *_args: calls.append("move") or True

    assert mod._reconcile_review_queue(ctx) is True
    assert calls == []
