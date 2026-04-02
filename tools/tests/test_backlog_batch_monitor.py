from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType


def _load_module() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "backlog_batch_monitor.py"
    spec = importlib.util.spec_from_file_location("backlog_batch_monitor", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_batch_stdout_tracks_success_and_failures() -> None:
    mod = _load_module()
    snapshot = mod.parse_batch_stdout(
        [
            "[2026-03-07T19:47:41Z] BEGIN phase=blocking_high_usertest_implement severities=['blocker', 'high'] workers=['worker_index=1 agent=codex model=gpt-5.4', 'worker_index=2 agent=claude model=<default>']",
            "[2026-03-07T19:47:41Z] PHASE blocking_high_usertest_implement cycle=1",
            "[2026-03-07T19:47:44Z] REFRESH source=usertest_implement reason=initial_refresh fingerprint=242f9064b005",
            "[2026-03-07T19:47:50Z] WAVE phase=blocking_high_usertest_implement cycle=1 candidates=4 parallel=2",
            "[2026-03-07T19:48:01Z] LAUNCH source=usertest_implement fingerprint=abc123 severity=blocker worker=worker_index=1 agent=codex model=gpt-5.4 ticket_path=I:\\repo\\a.md",
            "[2026-03-07T19:48:02Z] LAUNCH source=usertest_implement fingerprint=def456 severity=high worker=worker_index=2 agent=claude model=<default> ticket_path=I:\\repo\\b.md",
            "[2026-03-07T19:49:02Z] SUCCESS source=usertest_implement fingerprint=abc123 worker=worker_index=1 agent=codex model=gpt-5.4 run_dir=I:\\runs\\a branch=backlog/abc pushed=True pr_created=True pr_url=https://example/pr/1",
            "[2026-03-07T19:49:05Z] FAIL source=usertest_implement fingerprint=def456 worker=worker_index=2 agent=claude model=<default> error=Implementation run failed for b",
        ]
    )
    assert snapshot["phase"] == "blocking_high_usertest_implement"
    assert snapshot["cycle"] == 1
    assert snapshot["wave"]["candidates"] == 4
    assert snapshot["refresh_counts"]["usertest_implement"]["refresh"] == 1
    assert snapshot["workers"][0]["agent"] == "codex"
    assert snapshot["recent_successes"][0]["pr_url"] == "https://example/pr/1"
    assert snapshot["recent_failures"][0]["kind"] == "run_failed"
    assert snapshot["active_tickets"] == []


def test_parse_batch_stdout_keeps_active_ticket_and_claim_failure() -> None:
    mod = _load_module()
    snapshot = mod.parse_batch_stdout(
        [
            "[2026-03-07T19:47:41Z] BEGIN phase=medium_all severities=['medium'] workers=['worker_index=1 agent=gemini model=<default>']",
            "[2026-03-07T19:47:44Z] REUSE source=usertest_backlog fingerprint=111111111111 export=I:\\compiled\\tickets.json",
            "[2026-03-07T19:48:01Z] LAUNCH source=usertest_backlog fingerprint=xyz789 severity=medium worker=worker_index=1 agent=gemini model=<default> ticket_path=I:\\repo\\ticket.md",
            "[2026-03-07T19:48:02Z] FAIL claim fingerprint=bad111 source=usertest_backlog error=Unable to claim ticket",
        ]
    )
    assert snapshot["refresh_counts"]["usertest_backlog"]["reuse"] == 1
    assert snapshot["active_tickets"][0]["fingerprint"] == "xyz789"
    assert snapshot["recent_failures"][0]["kind"] == "claim_failed"


def test_build_snapshot_reads_authoritative_batch_state(tmp_path: Path) -> None:
    mod = _load_module()
    repo_root = tmp_path / "repo"
    log_dir = repo_root / "runs" / "_tmp_backlog_rebuild_logs"
    runs_root = repo_root / "runs" / "usertest_implement" / "usertest"
    batch_dir = repo_root / "runs" / "_batch" / "usertest_implement" / "20260308T010203Z"
    log_dir.mkdir(parents=True)
    runs_root.mkdir(parents=True)
    batch_dir.mkdir(parents=True)

    (batch_dir / "batch_state.json").write_text(
        """
{
  "schema_version": 1,
  "batch_id": "20260308T010203Z",
  "phase": "blocking_high_usertest_implement",
  "status": "blocked",
  "workers": [{"worker_index": 1, "agent": "codex", "model": "gpt-5.4"}],
  "in_flight": [{"fingerprint": "abc123", "severity": "blocker", "ticket_path": "I:/repo/a.md", "worker": {"worker_index": 1, "agent": "codex", "model": "gpt-5.4"}, "launched_utc": "2026-03-08T01:02:03Z"}],
  "completed": [],
  "failed": []
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (batch_dir / "global_blockers.json").write_text(
        """
{
  "schema_version": 1,
  "global_blockers": [{"created_utc": "2026-03-08T01:05:00Z", "class": "ticket_regression", "summary": "Produced PR went red in CI.", "fingerprint": "abc123"}]
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (batch_dir / "ticket_outcomes.jsonl").write_text(
        '{"fingerprint":"abc123","worker":{"worker_index":1,"agent":"codex","model":"gpt-5.4"},"run_dir":"I:/runs/a","handoff_summary":{"pr_url":"https://example/pr/1"},"failure":{"failure_class":"success"},"completed_utc":"2026-03-08T01:06:00Z"}\n',
        encoding="utf-8",
    )

    snapshot = mod.build_snapshot(repo_root, log_dir, runs_root)

    assert snapshot["batch_state"]["status"] == "blocked"
    assert snapshot["paths"]["batch_dir"] == str(batch_dir)
    assert snapshot["ticket_outcomes"][0]["fingerprint"] == "abc123"


def test_build_snapshot_ignores_stale_legacy_log_data_when_batch_state_exists(tmp_path: Path) -> None:
    mod = _load_module()
    repo_root = tmp_path / "repo"
    log_dir = repo_root / "runs" / "_tmp_backlog_rebuild_logs"
    runs_root = repo_root / "runs" / "usertest_implement" / "usertest"
    batch_dir = repo_root / "runs" / "_batch" / "usertest_implement" / "20260308T010203Z"
    log_dir.mkdir(parents=True)
    runs_root.mkdir(parents=True)
    batch_dir.mkdir(parents=True)

    (log_dir / "backlog_loop_20260307T214905Z.stdout.txt").write_text(
        "\n".join(
            [
                "[2026-03-08T00:28:42Z] FAIL source=usertest_implement fingerprint=oldfail worker=worker_index=2 agent=claude model=<default> error=old failure",
                "[2026-03-08T00:28:43Z] LAUNCH source=usertest_implement fingerprint=oldactive severity=high worker=worker_index=2 agent=claude model=<default> ticket_path=I:\\repo\\ticket.md",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (batch_dir / "batch_state.json").write_text(
        """
{
  "schema_version": 1,
  "batch_id": "20260308T010203Z",
  "phase": "blocking_high_usertest_implement",
  "status": "running",
  "workers": [{"worker_index": 1, "agent": "codex", "model": "gpt-5.4"}],
  "in_flight": [],
  "completed": [],
  "failed": []
}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    snapshot = mod.build_snapshot(repo_root, log_dir, runs_root)

    assert snapshot["parsed"]["recent_failures"] == []
    assert snapshot["parsed"]["active_tickets"] == []
