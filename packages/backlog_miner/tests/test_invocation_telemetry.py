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
        for line in (tmp_path / "lifecycle_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    provider_wait = next(
        row
        for row in rows
        if row["event_type"] == "work.completed"
        and row["attributes"].get("wait_category") == "provider"
    )
    assert provider_wait["active_seconds"] is None
    assert provider_wait["external_wait_seconds"] == 50
    assert provider_wait["attributes"]["wait_seconds_by_category"] == {"provider": 50}


def test_resumed_codex_stage_invocation_uses_prior_session_high_water(
    tmp_path: Path,
) -> None:
    prompt = "prompt"
    session_id = "019f8934-cdb5-70f3-806a-1c5748f385f7"
    for tag, usage in (
        ("initial", {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20}),
        ("resumed", {"input_tokens": 145, "cached_input_tokens": 90, "output_tokens": 32}),
    ):
        (tmp_path / f"{tag}.prompt.txt").write_text(prompt, encoding="utf-8")
        (tmp_path / f"{tag}.response.txt").write_text("{}", encoding="utf-8")
        (tmp_path / f"{tag}.raw_events.jsonl").write_text(
            json.dumps({"type": "turn.completed", "usage": usage}) + "\n",
            encoding="utf-8",
        )
        (tmp_path / f"{tag}.last_message.txt").write_text("{}", encoding="utf-8")
        (tmp_path / f"{tag}.stderr.txt").write_text("", encoding="utf-8")

    _write_model_invocation_manifest(
        stage="problem_mining",
        tag="initial",
        agent="codex",
        model="gpt-5.6-sol",
        out_dir=tmp_path,
        prompt=prompt,
        response="{}",
        error_kind=None,
        agent_session_id=session_id,
        invocation_started_at="2026-07-21T12:00:00Z",
        invocation_ended_at="2026-07-21T12:00:10Z",
        elapsed_seconds=10,
    )
    _write_model_invocation_manifest(
        stage="problem_mining",
        tag="resumed",
        agent="codex",
        model="gpt-5.6-sol",
        out_dir=tmp_path,
        prompt=prompt,
        response="{}",
        error_kind=None,
        agent_session_id=session_id,
        resumed_from_session_id=session_id,
        invocation_started_at="2026-07-21T12:01:00Z",
        invocation_ended_at="2026-07-21T12:01:10Z",
        elapsed_seconds=10,
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "lifecycle_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    completed = [row for row in rows if row["event_type"] == "model.invocation.completed"]
    resumed = completed[-1]
    assert resumed["attributes"]["usage_semantics"] == "session_cumulative"
    assert resumed["attributes"]["token_usage"]["total_tokens"] == 57
    assert resumed["attributes"]["usage_unknown_reason"] is None
    assert len(resumed["evidence_paths"]) == 3


def test_resumed_codex_stage_invocation_rejects_changed_prior_usage_stream(
    tmp_path: Path,
) -> None:
    prompt = "prompt"
    session_id = "019f8934-cdb5-70f3-806a-1c5748f385f7"
    for tag, usage in (
        ("initial", {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20}),
        ("resumed", {"input_tokens": 145, "cached_input_tokens": 90, "output_tokens": 32}),
    ):
        (tmp_path / f"{tag}.prompt.txt").write_text(prompt, encoding="utf-8")
        (tmp_path / f"{tag}.response.txt").write_text("{}", encoding="utf-8")
        (tmp_path / f"{tag}.raw_events.jsonl").write_text(
            json.dumps({"type": "turn.completed", "usage": usage}) + "\n",
            encoding="utf-8",
        )
        (tmp_path / f"{tag}.last_message.txt").write_text("{}", encoding="utf-8")
        (tmp_path / f"{tag}.stderr.txt").write_text("", encoding="utf-8")

    _write_model_invocation_manifest(
        stage="problem_mining",
        tag="initial",
        agent="codex",
        model="gpt-5.6-sol",
        out_dir=tmp_path,
        prompt=prompt,
        response="{}",
        error_kind=None,
        agent_session_id=session_id,
        invocation_started_at="2026-07-21T12:00:00Z",
        invocation_ended_at="2026-07-21T12:00:10Z",
        elapsed_seconds=10,
    )
    (tmp_path / "initial.raw_events.jsonl").write_text(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_model_invocation_manifest(
        stage="problem_mining",
        tag="resumed",
        agent="codex",
        model="gpt-5.6-sol",
        out_dir=tmp_path,
        prompt=prompt,
        response="{}",
        error_kind=None,
        agent_session_id=session_id,
        resumed_from_session_id=session_id,
        invocation_started_at="2026-07-21T12:01:00Z",
        invocation_ended_at="2026-07-21T12:01:10Z",
        elapsed_seconds=10,
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "lifecycle_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    resumed = [row for row in rows if row["event_type"] == "model.invocation.completed"][-1]
    assert resumed["attributes"]["usage_semantics"] == "unattributable"
    assert resumed["attributes"]["token_usage"] is None
    assert (
        resumed["attributes"]["usage_unknown_reason"]
        == "continued_session_missing_prior_high_water"
    )
    assert len(resumed["evidence_paths"]) == 2


def test_resumed_codex_stage_invocation_rejects_unattributable_prior_usage(
    tmp_path: Path,
) -> None:
    prompt = "prompt"
    session_id = "019f8934-cdb5-70f3-806a-1c5748f385f7"
    for tag in ("initial", "resumed"):
        (tmp_path / f"{tag}.prompt.txt").write_text(prompt, encoding="utf-8")
        (tmp_path / f"{tag}.response.txt").write_text("{}", encoding="utf-8")
        (tmp_path / f"{tag}.last_message.txt").write_text("{}", encoding="utf-8")
        (tmp_path / f"{tag}.stderr.txt").write_text("", encoding="utf-8")
    (tmp_path / "initial.raw_events.jsonl").write_text(
        "".join(
            json.dumps({"type": "turn.completed", "usage": usage}) + "\n"
            for usage in (
                {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20},
                {"input_tokens": 110, "cached_input_tokens": 65, "output_tokens": 21},
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / "resumed.raw_events.jsonl").write_text(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 145, "cached_input_tokens": 90, "output_tokens": 32},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _write_model_invocation_manifest(
        stage="problem_mining",
        tag="initial",
        agent="codex",
        model="gpt-5.6-sol",
        out_dir=tmp_path,
        prompt=prompt,
        response="{}",
        error_kind=None,
        agent_session_id=session_id,
        invocation_started_at="2026-07-21T12:00:00Z",
        invocation_ended_at="2026-07-21T12:00:10Z",
        elapsed_seconds=10,
    )
    _write_model_invocation_manifest(
        stage="problem_mining",
        tag="resumed",
        agent="codex",
        model="gpt-5.6-sol",
        out_dir=tmp_path,
        prompt=prompt,
        response="{}",
        error_kind=None,
        agent_session_id=session_id,
        resumed_from_session_id=session_id,
        invocation_started_at="2026-07-21T12:01:00Z",
        invocation_ended_at="2026-07-21T12:01:10Z",
        elapsed_seconds=10,
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "lifecycle_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    resumed = [row for row in rows if row["event_type"] == "model.invocation.completed"][-1]
    assert resumed["attributes"]["usage_semantics"] == "unattributable"
    assert resumed["attributes"]["token_usage"] is None
    assert (
        resumed["attributes"]["usage_unknown_reason"]
        == "continued_session_missing_prior_high_water"
    )
