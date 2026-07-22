from __future__ import annotations

import json
from pathlib import Path

from backlog_miner.pipeline import _write_model_invocation_manifest


def test_stage_invocation_writes_unattributable_receipt_for_unsupported_provider(
    tmp_path: Path,
) -> None:
    tag = "stage_001"
    prompt = "prompt"
    response = "{}"
    (tmp_path / f"{tag}.prompt.txt").write_text(prompt, encoding="utf-8")
    (tmp_path / f"{tag}.response.txt").write_text(response, encoding="utf-8")
    (tmp_path / f"{tag}.raw_events.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / f"{tag}.last_message.txt").write_text(response, encoding="utf-8")
    (tmp_path / f"{tag}.stderr.txt").write_text("", encoding="utf-8")
    manifest_path = _write_model_invocation_manifest(
        stage="problem_mining",
        tag=tag,
        agent="claude",
        model="claude-current",
        out_dir=tmp_path,
        prompt=prompt,
        response=response,
        error_kind=None,
        invocation_started_at="2026-07-21T12:00:00Z",
        invocation_ended_at="2026-07-21T12:00:10Z",
        elapsed_seconds=10,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 3
    rows = [
        json.loads(line)
        for line in (tmp_path / "lifecycle_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    completed = next(row for row in rows if row["event_type"] == "model.invocation.completed")
    assert completed["active_seconds"] == 10
    assert completed["attributes"]["usage_semantics"] == "unattributable"
    receipts = list((tmp_path / "model_usage_receipts").rglob("model_usage_receipt.json"))
    assert len(receipts) == 1


def test_resumed_provider_wait_is_accounted_separately_from_active_time(
    tmp_path: Path,
) -> None:
    prompt = "prompt"
    for tag in ("failed", "resumed"):
        (tmp_path / f"{tag}.prompt.txt").write_text(prompt, encoding="utf-8")
        (tmp_path / f"{tag}.raw_events.jsonl").write_text("{}\n", encoding="utf-8")
        (tmp_path / f"{tag}.last_message.txt").write_text("{}", encoding="utf-8")
        (tmp_path / f"{tag}.stderr.txt").write_text("", encoding="utf-8")
    _write_model_invocation_manifest(
        stage="problem_mining",
        tag="failed",
        agent="claude",
        model="claude-current",
        out_dir=tmp_path,
        prompt=prompt,
        response=None,
        error_kind="BacklogProviderExternalWait",
        agent_session_id="session-1",
        invocation_started_at="2026-07-21T12:00:00Z",
        invocation_ended_at="2026-07-21T12:00:10Z",
        elapsed_seconds=10,
    )
    (tmp_path / "resumed.response.txt").write_text("{}", encoding="utf-8")
    _write_model_invocation_manifest(
        stage="problem_mining",
        tag="resumed",
        agent="claude",
        model="claude-current",
        out_dir=tmp_path,
        prompt=prompt,
        response="{}",
        error_kind=None,
        agent_session_id="session-1",
        resumed_from_session_id="session-1",
        invocation_started_at="2026-07-21T12:01:00Z",
        invocation_ended_at="2026-07-21T12:01:20Z",
        elapsed_seconds=20,
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "lifecycle_events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    provider_wait = next(
        row
        for row in rows
        if row["event_type"] == "work.completed"
        and row["attributes"].get("wait_category") == "provider"
    )
    assert provider_wait["active_seconds"] is None
    assert provider_wait["external_wait_seconds"] == 50
    assert provider_wait["attributes"]["wait_seconds_by_category"] == {
        "provider": 50
    }
