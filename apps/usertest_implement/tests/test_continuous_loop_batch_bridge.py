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


def test_review_changes_requested_resumes_same_author_once(tmp_path: Path) -> None:
    mod = _load_module()
    owner_root = tmp_path / "owner"
    ticket = owner_root / ".agents" / "plans" / "4 - for_review" / "plan.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text(
        "Generated by `python -m usertest_backlog.cli reports export-tickets`.\n",
        encoding="utf-8",
    )
    fingerprint = "0123456789abcdef"
    pr_url = "https://example.invalid/pr/1"
    implementation_run = owner_root / "runs" / "implementation" / "one"
    ledger = {
        "actions": {
            fingerprint: {
                "last_pr_url": pr_url,
                "last_run_dir": str(implementation_run),
                "last_resume_lifecycle_state": "awaiting_review",
            }
        }
    }
    ctx = SimpleNamespace(
        implement_python=tmp_path / "python.exe",
        repo_root=tmp_path / "tool",
        owner_root=owner_root,
        repo_input=str(owner_root),
        implementation_agent="codex",
        implementation_model="gpt-5.6-terra",
    )
    review_calls: list[str] = []
    resume_calls: list[tuple[str, list[str]]] = []

    def _fake_review(_ctx, selected_fingerprint: str) -> bool:
        review_calls.append(selected_fingerprint)
        ledger["actions"][fingerprint].update(
            {
                "last_review_pr_url": pr_url,
                "last_review_decision": "changes_requested",
                "last_review_merge_ready": False,
                "last_resume_lifecycle_state": "review_failed_resume_ready",
            }
        )
        return True

    def _fake_run_logged(_ctx, argv, **kwargs):
        resume_calls.append((kwargs["label"], list(argv)))
        return SimpleNamespace(returncode=0)

    mod._load_ledger = lambda _ctx: ledger
    mod._find_ticket_path = lambda _ctx, _fingerprint: ticket
    mod._gh_pr_view = lambda _ctx, _url: {"state": "OPEN", "mergedAt": None}
    mod._run_review = _fake_review
    mod._run_logged = _fake_run_logged
    mod._merge_review = lambda *_args: (_ for _ in ()).throw(
        AssertionError("changes-requested review must not merge")
    )

    assert mod._reconcile_review_queue(ctx) is True
    assert review_calls == [fingerprint]
    assert len(resume_calls) == 1
    label, resume_argv = resume_calls[0]
    assert label == f"resume review changes requested {fingerprint}"
    assert resume_argv[resume_argv.index("resume") + 1 : resume_argv.index("--repo")] == [
        "--run-dir",
        str(implementation_run),
    ]
    assert resume_argv[resume_argv.index("--agent") + 1] == "codex"
    assert resume_argv[resume_argv.index("--model") + 1] == "gpt-5.6-terra"
    assert "--verify-timeout-seconds" not in resume_argv


def test_merge_ready_review_does_not_resume_implementation_author(tmp_path: Path) -> None:
    mod = _load_module()
    owner_root = tmp_path / "owner"
    ticket = owner_root / ".agents" / "plans" / "4 - for_review" / "plan.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text(
        "Generated by `python -m usertest_backlog.cli reports export-tickets`.\n",
        encoding="utf-8",
    )
    fingerprint = "0123456789abcdef"
    pr_url = "https://example.invalid/pr/1"
    head_oid = "a" * 40
    ctx = SimpleNamespace(owner_root=owner_root)
    ledger = {
        "actions": {
            fingerprint: {
                "last_pr_url": pr_url,
                "last_review_pr_url": pr_url,
                "last_review_decision": "approved",
                "last_review_causal_acceptance": True,
                "last_review_merge_ready": True,
                "last_reviewed_head_oid": head_oid,
                "last_resume_lifecycle_state": "awaiting_merge",
            }
        }
    }
    merges: list[str] = []
    resumes: list[str] = []

    mod._load_ledger = lambda _ctx: ledger
    mod._find_ticket_path = lambda _ctx, _fingerprint: ticket
    mod._gh_pr_view = lambda _ctx, _url: {
        "state": "OPEN",
        "mergedAt": None,
        "isDraft": False,
        "headRefOid": head_oid,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "SUCCESS"}
        ],
    }
    mod._run_review = lambda *_args: (_ for _ in ()).throw(
        AssertionError("merge-ready head must not be reviewed again")
    )
    mod._resume_review_changes_requested = (
        lambda _ctx, selected_fingerprint, **_kwargs: (
            resumes.append(selected_fingerprint) or True
        )
    )
    mod._merge_review = lambda _ctx, selected_fingerprint: (
        merges.append(selected_fingerprint) or True
    )

    assert mod._reconcile_review_queue(ctx) is True
    assert resumes == []
    assert merges == [fingerprint]


def test_causally_approved_unchanged_draft_is_marked_ready_without_rereview(
    tmp_path: Path,
) -> None:
    mod = _load_module()
    owner_root = tmp_path / "owner"
    ticket = owner_root / ".agents" / "plans" / "4 - for_review" / "plan.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text(
        "Generated by `python -m usertest_backlog.cli reports export-tickets`.\n",
        encoding="utf-8",
    )
    fingerprint = "0123456789abcdef"
    pr_url = "https://example.invalid/pr/1"
    head_oid = "a" * 40
    ctx = SimpleNamespace(owner_root=owner_root)
    ledger = {
        "actions": {
            fingerprint: {
                "last_pr_url": pr_url,
                "last_review_pr_url": pr_url,
                "last_review_decision": "approved",
                "last_review_causal_acceptance": True,
                "last_review_merge_ready": False,
                "last_reviewed_head_oid": head_oid,
            }
        }
    }
    ready_calls: list[str] = []

    mod._load_ledger = lambda _ctx: ledger
    mod._find_ticket_path = lambda _ctx, _fingerprint: ticket
    mod._gh_pr_view = lambda _ctx, _url: {
        "state": "OPEN",
        "mergedAt": None,
        "isDraft": True,
        "headRefOid": head_oid,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [],
    }
    mod._mark_pr_ready = lambda _ctx, url: ready_calls.append(url) or True
    mod._run_review = lambda *_args: (_ for _ in ()).throw(
        AssertionError("unchanged causally approved head must not be reviewed again")
    )
    mod._merge_review = lambda *_args: (_ for _ in ()).throw(
        AssertionError("draft transition must finish before merge")
    )

    assert mod._reconcile_review_queue(ctx) is True
    assert ready_calls == [pr_url]


def test_causally_approved_unchanged_head_waits_for_ci_without_rereview(
    tmp_path: Path,
) -> None:
    mod = _load_module()
    owner_root = tmp_path / "owner"
    ticket = owner_root / ".agents" / "plans" / "4 - for_review" / "plan.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text(
        "Generated by `python -m usertest_backlog.cli reports export-tickets`.\n",
        encoding="utf-8",
    )
    fingerprint = "0123456789abcdef"
    pr_url = "https://example.invalid/pr/1"
    head_oid = "a" * 40
    ctx = SimpleNamespace(owner_root=owner_root)
    ledger = {
        "actions": {
            fingerprint: {
                "last_pr_url": pr_url,
            }
        }
    }
    logs: list[str] = []
    review_calls: list[str] = []

    def _fake_review(_ctx, selected_fingerprint: str) -> bool:
        review_calls.append(selected_fingerprint)
        ledger["actions"][fingerprint].update(
            {
                "last_review_pr_url": pr_url,
                "last_review_decision": "approved",
                "last_review_causal_acceptance": True,
                "last_review_merge_ready": False,
                "last_reviewed_head_oid": head_oid,
            }
        )
        return True

    mod._load_ledger = lambda _ctx: ledger
    mod._find_ticket_path = lambda _ctx, _fingerprint: ticket
    mod._gh_pr_view = lambda _ctx, _url: {
        "state": "OPEN",
        "mergedAt": None,
        "isDraft": False,
        "headRefOid": head_oid,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": None}],
    }
    mod._append_log = lambda _ctx, message: logs.append(message)
    mod._run_review = _fake_review
    mod._merge_review = lambda *_args: (_ for _ in ()).throw(
        AssertionError("pending CI must not trigger merge")
    )

    assert mod._reconcile_review_queue(ctx) is True
    assert mod._reconcile_review_queue(ctx) is True
    assert review_calls == [fingerprint]
    assert any("state=pending" in message for message in logs)


def test_changed_head_requires_new_review_before_operational_gates(tmp_path: Path) -> None:
    mod = _load_module()
    owner_root = tmp_path / "owner"
    ticket = owner_root / ".agents" / "plans" / "4 - for_review" / "plan.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text(
        "Generated by `python -m usertest_backlog.cli reports export-tickets`.\n",
        encoding="utf-8",
    )
    fingerprint = "0123456789abcdef"
    pr_url = "https://example.invalid/pr/1"
    ledger = {
        "actions": {
            fingerprint: {
                "last_pr_url": pr_url,
                "last_review_pr_url": pr_url,
                "last_review_decision": "approved",
                "last_review_causal_acceptance": True,
                "last_review_merge_ready": True,
                "last_reviewed_head_oid": "a" * 40,
            }
        }
    }
    review_calls: list[str] = []
    ctx = SimpleNamespace(owner_root=owner_root)

    mod._load_ledger = lambda _ctx: ledger
    mod._find_ticket_path = lambda _ctx, _fingerprint: ticket
    mod._gh_pr_view = lambda _ctx, _url: {
        "state": "OPEN",
        "mergedAt": None,
        "isDraft": False,
        "headRefOid": "b" * 40,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "SUCCESS"}
        ],
    }
    mod._run_review = lambda _ctx, selected_fingerprint: (
        review_calls.append(selected_fingerprint) or True
    )
    mod._merge_review = lambda *_args: (_ for _ in ()).throw(
        AssertionError("changed head must be re-reviewed before merge")
    )

    assert mod._reconcile_review_queue(ctx) is True
    assert review_calls == [fingerprint]


def _operational_blocker_case(
    tmp_path: Path,
    *,
    lifecycle: str = "merge_ready",
) -> tuple[object, SimpleNamespace, str, str, Path]:
    mod = _load_module()
    owner_root = tmp_path / "owner"
    fingerprint = "0123456789abcdef"
    pr_url = "https://example.invalid/pr/1"
    head_oid = "a" * 40
    ticket = owner_root / ".agents" / "plans" / "4 - for_review" / f"{fingerprint}.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text(
        "Generated by `python -m usertest_backlog.cli reports export-tickets`.\n",
        encoding="utf-8",
    )
    implementation_run = owner_root / "runs" / "implementation" / "one"
    implementation_run.mkdir(parents=True)
    ledger_path = owner_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": None,
                "actions": {
                    fingerprint: {
                        "last_pr_url": pr_url,
                        "last_run_dir": str(implementation_run),
                        "last_review_pr_url": pr_url,
                        "last_review_decision": "approved",
                        "last_review_causal_acceptance": True,
                        "last_review_merge_ready": lifecycle == "merge_ready",
                        "last_reviewed_head_oid": head_oid,
                        "last_resume_lifecycle_state": lifecycle,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        implement_python=tmp_path / "python.exe",
        repo_root=tmp_path / "tool",
        owner_root=owner_root,
        repo_input=str(owner_root),
        implementation_agent="codex",
        implementation_model="gpt-5.6-terra",
        log_path=tmp_path / "continuous.log",
    )
    return mod, ctx, fingerprint, pr_url, ledger_path


def test_terminal_ci_failure_routes_one_same_author_correction_then_blocks_repeat(
    tmp_path: Path,
) -> None:
    mod, ctx, fingerprint, pr_url, ledger_path = _operational_blocker_case(tmp_path)
    resume_calls: list[tuple[str, list[str]]] = []
    logs: list[str] = []
    gh_calls = 0

    def _failing_pr(_ctx, _url):
        nonlocal gh_calls
        gh_calls += 1
        return {
            "url": pr_url,
            "state": "OPEN",
            "mergedAt": None,
            "isDraft": False,
            "headRefOid": "a" * 40,
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [
                {
                    "name": "tests",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                    "detailsUrl": (
                        "https://example.invalid/runs/failed"
                        if gh_calls == 1
                        else "https://example.invalid/runs/failed-rerun"
                    ),
                }
            ],
        }

    mod._gh_pr_view = _failing_pr
    mod._run_review = lambda *_args: (_ for _ in ()).throw(
        AssertionError("unchanged causally accepted head must not be re-reviewed first")
    )
    mod._merge_review = lambda *_args: (_ for _ in ()).throw(
        AssertionError("terminal-failing CI must not merge")
    )
    mod._run_logged = lambda _ctx, argv, **kwargs: (
        resume_calls.append((kwargs["label"], list(argv)))
        or SimpleNamespace(returncode=0)
    )
    mod._append_log = lambda _ctx, message: logs.append(message)

    assert mod._reconcile_review_queue(ctx) is True
    assert len(resume_calls) == 1
    label, argv = resume_calls[0]
    assert label == f"resume terminal_ci_failure {fingerprint}"
    assert argv[argv.index("--correction-origin") + 1] == "system_self_correction"
    instruction = argv[argv.index("--supervisor-instruction") + 1]
    assert "terminal CI failure" in instruction
    assert "https://example.invalid/runs/failed" in instruction

    # The live head/evidence did not change after the bounded successful resume.
    # A second pass records nonprogress for supervision instead of spinning.
    assert mod._reconcile_review_queue(ctx) is True
    assert len(resume_calls) == 1
    ledger = mod.yaml.safe_load(ledger_path.read_text(encoding="utf-8"))
    action = ledger["actions"][fingerprint]
    assert action["last_operational_correction"]["status"] == "blocked_nonprogress"
    assert action["last_operational_correction"]["checks"][0]["detailsUrl"].endswith(
        "failed-rerun"
    )
    assert action["last_review_causal_acceptance"] is True
    assert (
        action["last_review_approval_invalidation"]["prior_causal_acceptance"] is True
    )
    assert any("state=blocked_nonprogress" in message for message in logs)


def test_operational_blocker_identity_ignores_urls_but_tracks_failure_membership(
    tmp_path: Path,
) -> None:
    mod = _load_module()

    def evidence(checks: list[dict[str, object]]) -> dict[str, object]:
        return mod._operational_blocker_evidence(
            pr_doc={
                "headRefOid": "a" * 40,
                "mergeable": "MERGEABLE",
                "statusCheckRollup": checks,
            },
            pr_url="https://example.invalid/pr/1",
            classification="terminal_ci_failure",
        )

    original = evidence(
        [
            {
                "name": "tests",
                "conclusion": "FAILURE",
                "detailsUrl": "https://example.invalid/runs/1",
            },
            {
                "context": "lint",
                "state": "ERROR",
                "targetUrl": "https://example.invalid/status/1",
            },
        ]
    )
    new_urls = evidence(
        [
            {
                "name": "tests",
                "conclusion": "FAILURE",
                "detailsUrl": "https://example.invalid/runs/2",
            },
            {
                "context": "lint",
                "state": "ERROR",
                "targetUrl": "https://example.invalid/status/2",
            },
        ]
    )
    fewer_failures = evidence(
        [
            {
                "name": "tests",
                "conclusion": "FAILURE",
                "detailsUrl": "https://example.invalid/runs/3",
            }
        ]
    )
    different_failure = evidence(
        [
            {
                "name": "integration",
                "conclusion": "FAILURE",
                "detailsUrl": "https://example.invalid/runs/4",
            },
            {
                "context": "lint",
                "state": "ERROR",
                "targetUrl": "https://example.invalid/status/4",
            },
        ]
    )

    assert original["evidence_id"] == new_urls["evidence_id"]
    assert original["checks"] != new_urls["checks"]
    assert original["evidence_id"] != fewer_failures["evidence_id"]
    assert original["evidence_id"] != different_failure["evidence_id"]


def test_approved_pending_review_routes_if_same_head_later_fails_ci(
    tmp_path: Path,
) -> None:
    mod, ctx, fingerprint, pr_url, _ledger_path = _operational_blocker_case(
        tmp_path,
        lifecycle="awaiting_review",
    )
    check_state = "pending"
    resume_calls: list[list[str]] = []

    def _pr_doc(_ctx, _url):
        if check_state == "pending":
            check = {"name": "tests", "status": "IN_PROGRESS", "conclusion": None}
        else:
            check = {"name": "tests", "status": "COMPLETED", "conclusion": "FAILURE"}
        return {
            "url": pr_url,
            "state": "OPEN",
            "mergedAt": None,
            "isDraft": False,
            "headRefOid": "a" * 40,
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [check],
        }

    mod._gh_pr_view = _pr_doc
    mod._run_review = lambda *_args: (_ for _ in ()).throw(
        AssertionError("causally approved unchanged head must not be reviewed again")
    )
    mod._merge_review = lambda *_args: (_ for _ in ()).throw(
        AssertionError("pending/failing CI must not merge")
    )
    mod._run_logged = lambda _ctx, argv, **_kwargs: (
        resume_calls.append(list(argv)) or SimpleNamespace(returncode=0)
    )
    mod._append_log = lambda *_args: None

    assert mod._reconcile_review_queue(ctx) is True
    assert resume_calls == []

    check_state = "failure"
    assert mod._reconcile_review_queue(ctx) is True
    assert len(resume_calls) == 1
    assert resume_calls[0][resume_calls[0].index("--correction-origin") + 1] == (
        "system_self_correction"
    )
    action = mod._load_ledger(ctx)["actions"][fingerprint]
    assert action["last_operational_correction"]["classification"] == (
        "terminal_ci_failure"
    )


def test_real_merge_conflict_routes_same_author_correction(tmp_path: Path) -> None:
    mod, ctx, fingerprint, pr_url, _ledger_path = _operational_blocker_case(tmp_path)
    resume_calls: list[list[str]] = []
    mod._gh_pr_view = lambda _ctx, _url: {
        "url": pr_url,
        "state": "OPEN",
        "mergedAt": None,
        "isDraft": False,
        "headRefOid": "a" * 40,
        "mergeable": "CONFLICTING",
        "statusCheckRollup": [
            {"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ],
    }
    mod._run_review = lambda *_args: (_ for _ in ()).throw(
        AssertionError("unchanged causally accepted head must not be re-reviewed first")
    )
    mod._merge_review = lambda *_args: (_ for _ in ()).throw(
        AssertionError("conflicting PR must not merge")
    )
    mod._run_logged = lambda _ctx, argv, **_kwargs: (
        resume_calls.append(list(argv)) or SimpleNamespace(returncode=0)
    )

    assert mod._reconcile_review_queue(ctx) is True
    assert len(resume_calls) == 1
    instruction = resume_calls[0][resume_calls[0].index("--supervisor-instruction") + 1]
    assert "definitively CONFLICTING" in instruction
    assert '"classification": "merge_conflict"' in instruction


def test_transient_unknown_mergeability_waits_without_correction(tmp_path: Path) -> None:
    mod, ctx, _fingerprint, pr_url, _ledger_path = _operational_blocker_case(tmp_path)
    logs: list[str] = []
    mod._gh_pr_view = lambda _ctx, _url: {
        "url": pr_url,
        "state": "OPEN",
        "mergedAt": None,
        "isDraft": False,
        "headRefOid": "a" * 40,
        "mergeable": "UNKNOWN",
        "statusCheckRollup": [
            {"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ],
    }
    mod._run_review = lambda *_args: (_ for _ in ()).throw(
        AssertionError("unchanged causally accepted head must not be re-reviewed")
    )
    mod._resume_review_changes_requested = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("transient UNKNOWN must not trigger correction")
    )
    mod._merge_review = lambda *_args: (_ for _ in ()).throw(
        AssertionError("transient UNKNOWN must not merge")
    )
    mod._append_log = lambda _ctx, message: logs.append(message)

    assert mod._reconcile_review_queue(ctx) is True
    assert any("state=UNKNOWN" in message for message in logs)


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


def test_historical_completed_merge_without_merge_ledger_fields_is_case_local(
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
            "8a51325c9752d31d": {
                "last_pr_url": "https://example.invalid/pr/170",
                "last_review_pr_url": "https://example.invalid/pr/170",
                "last_review_merge_ready": True,
                # Historical entries can be complete even though the merge finalizer
                # never persisted last_merge_pr_url/last_merged_at/outcome state.
            }
        }
    }
    mod._find_ticket_path = lambda _ctx, _fingerprint: completed
    mod._gh_pr_view = lambda _ctx, _url: {
        "state": "MERGED",
        "mergedAt": "2026-07-05T23:44:19Z",
    }
    mod._merge_review = lambda _ctx, fingerprint: (
        attempts.append(fingerprint) or False
    )
    mod._append_log = lambda _ctx, message: logs.append(message)

    assert mod._reconcile_review_queue(ctx) is True
    assert attempts == ["8a51325c9752d31d"]
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
