from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from backlog_miner.pipeline import (
    _write_model_invocation_manifest,
    load_pipeline_prompt_manifest,
)

from usertest_backlog.workflows.prioritization import (
    _run_problem_prioritization_stage,
)
from usertest_backlog.workflows.solution_options import (
    _run_optioning_prompt_with_correction,
)

_SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _write_fake_invocation(
    *,
    kwargs: dict[str, Any],
    response: str,
    session_id: str | None,
    elapsed_seconds: float,
) -> SimpleNamespace:
    out_dir = Path(kwargs["out_dir"])
    tag = str(kwargs["tag"])
    prompt = str(kwargs["prompt"])
    workspace = Path(kwargs["workspace_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = out_dir / f"{tag}.prompt.txt"
    response_path = out_dir / f"{tag}.response.txt"
    raw_events_path = out_dir / f"{tag}.raw_events.jsonl"
    last_message_path = out_dir / f"{tag}.last_message.txt"
    stderr_path = out_dir / f"{tag}.stderr.txt"
    prompt_path.write_text(prompt, encoding="utf-8", newline="\n")
    response_path.write_text(response, encoding="utf-8", newline="\n")
    raw_events_path.write_text("{}\n", encoding="utf-8", newline="\n")
    last_message_path.write_text(response, encoding="utf-8", newline="\n")
    stderr_path.write_text("", encoding="utf-8", newline="\n")
    invocation_path = _write_model_invocation_manifest(
        stage=str(kwargs["stage"]),
        tag=tag,
        agent=str(kwargs["agent"]),
        out_dir=out_dir,
        prompt=prompt,
        response=response,
        error_kind=None if session_id is not None else "NoAuthorSession",
        agent_session_id=session_id,
        resumed_from_session_id=kwargs.get("resume_session_id"),
        workspace_dir=workspace,
    )
    return SimpleNamespace(
        response=response,
        agent_session_id=session_id,
        resumed_from_session_id=kwargs.get("resume_session_id"),
        workspace_dir=workspace,
        invocation_manifest_path=invocation_path,
        prompt_path=prompt_path,
        response_path=response_path,
        raw_events_path=raw_events_path,
        last_message_path=last_message_path,
        stderr_path=stderr_path,
        elapsed_seconds=elapsed_seconds,
    )


def _priority_decision(problem_id: str, atom_id: str) -> dict[str, Any]:
    return {
        "problem_id": problem_id,
        "priority_bucket": "p1",
        "selected_for_research": True,
        "priority_rationale": f"Evidence {atom_id} shows material impact.",
        "evidence_atom_ids_used": [atom_id],
        "priority_status": "prioritized",
    }


def test_stage2_repairs_missing_decision_in_exact_author_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    manifest = load_pipeline_prompt_manifest(repo_root / "configs" / "backlog_prompts")
    records = [
        {
            "problem_id": "problem:one",
            "title": "Problem One unique marker",
            "problem": "One fails",
            "user_impact": "One is blocked",
            "severity": "high",
            "confidence": 0.9,
            "evidence_atom_ids": ["atom:one"],
            "evidence_summary": "One observed failure",
        },
        {
            "problem_id": "problem:two",
            "title": "Problem Two",
            "problem": "Two fails",
            "user_impact": "Two is blocked",
            "severity": "medium",
            "confidence": 0.8,
            "evidence_atom_ids": ["atom:two"],
            "evidence_summary": "Two observed failure",
        },
    ]
    responses = [
        json.dumps([_priority_decision("problem:one", "atom:one")]),
        json.dumps(
            [
                _priority_decision("problem:one", "atom:one"),
                _priority_decision("problem:two", "atom:two"),
            ]
        ),
    ]
    calls: list[dict[str, Any]] = []

    def fake_run_stage_prompt_json(**kwargs: Any) -> SimpleNamespace:
        calls.append(dict(kwargs))
        return _write_fake_invocation(
            kwargs=kwargs,
            response=responses[len(calls) - 1],
            session_id=_SESSION_ID,
            elapsed_seconds=1.0 if len(calls) == 1 else 0.5,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.prioritization.run_stage_prompt_json",
        fake_run_stage_prompt_json,
    )
    out_json = tmp_path / "prioritized.json"
    doc = _run_problem_prioritization_stage(
        atoms=[
            {"atom_id": "atom:one", "severity_hint": "high"},
            {"atom_id": "atom:two", "severity_hint": "medium"},
        ],
        problem_records=records,
        pipeline_manifest=manifest,
        artifacts_dir=tmp_path / "artifacts",
        out_json=out_json,
        out_md=tmp_path / "prioritized.md",
        agent="claude",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
        dry_run=False,
        stage_guidance_text="Prioritize every canonical problem.",
    )

    assert len(calls) == 2
    assert calls[0]["resume_session_id"] is None
    assert calls[1]["resume_session_id"] == _SESSION_ID
    assert Path(calls[0]["workspace_dir"]).resolve() == Path(
        calls[1]["workspace_dir"]
    ).resolve()
    correction_prompt = str(calls[1]["prompt"])
    assert "priority_decision:problem:one" in correction_prompt
    assert "prioritizer_missing_problem_id:problem:two" in correction_prompt
    assert "Problem One unique marker" not in correction_prompt

    meta = doc["input_meta"]
    assert meta["prioritizer_status"] == "corrected"
    assert meta["prioritizer_correction_status"] == "corrected"
    assert meta["prioritizer_correction_metrics"]["attempt_count"] == 2
    assert meta["prioritizer_correction_metrics"]["repaired"] is True
    assert len(meta["prioritizer_attempt_history"]) == 2
    assert meta["prioritizer_fallback_decision_count"] == 0
    assert {item["problem_id"] for item in doc["items"]} == {
        "problem:one",
        "problem:two",
    }
    assert all(item["model_priority_accepted"] is True for item in doc["items"])


def test_stage2_retries_fresh_until_codex_author_session_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    manifest = load_pipeline_prompt_manifest(repo_root / "configs" / "backlog_prompts")
    records = [
        {
            "problem_id": "problem:one",
            "title": "Problem One",
            "problem": "One fails",
            "user_impact": "One is blocked",
            "severity": "high",
            "confidence": 0.9,
            "evidence_atom_ids": ["atom:one"],
            "evidence_summary": "One observed failure",
        }
    ]
    response = json.dumps([_priority_decision("problem:one", "atom:one")])
    calls: list[dict[str, Any]] = []

    def fake_run_stage_prompt_json(**kwargs: Any) -> SimpleNamespace:
        calls.append(dict(kwargs))
        return _write_fake_invocation(
            kwargs=kwargs,
            response=response,
            session_id=None if len(calls) == 1 else _SESSION_ID,
            elapsed_seconds=20.0 if len(calls) == 1 else 1.0,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.prioritization.run_stage_prompt_json",
        fake_run_stage_prompt_json,
    )
    doc = _run_problem_prioritization_stage(
        atoms=[{"atom_id": "atom:one", "severity_hint": "high"}],
        problem_records=records,
        pipeline_manifest=manifest,
        artifacts_dir=tmp_path / "artifacts",
        out_json=tmp_path / "prioritized.json",
        out_md=tmp_path / "prioritized.md",
        agent="codex",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
        dry_run=False,
        stage_guidance_text="Prioritize every canonical problem.",
    )

    assert len(calls) == 2
    assert calls[0]["resume_session_id"] is None
    assert calls[1]["resume_session_id"] is None
    assert calls[1]["prompt"] == calls[0]["prompt"]
    meta = doc["input_meta"]
    assert meta["prioritizer_status"] == "ok"
    assert meta["prioritizer_correction_status"] == "accepted"
    assert meta["prioritizer_correction_metrics"]["attempt_count"] == 2
    assert (
        meta["prioritizer_correction_metrics"]["session_acquisition_retry_count"]
        == 1
    )
    assert len(meta["prioritizer_attempt_history"]) == 2
    assert meta["prioritizer_attempt_history"][0]["agent_session_id"] is None
    assert meta["prioritizer_attempt_history"][1]["agent_session_id"] == _SESSION_ID


def test_stage2_transient_exact_session_exception_retries_same_author(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    manifest = load_pipeline_prompt_manifest(repo_root / "configs" / "backlog_prompts")
    records = [
        {
            "problem_id": "problem:one",
            "title": "One",
            "problem": "One fails",
            "user_impact": "One is blocked",
            "severity": "high",
            "confidence": 0.9,
            "evidence_atom_ids": ["atom:one"],
            "evidence_summary": "One observed failure",
        },
        {
            "problem_id": "problem:two",
            "title": "Two",
            "problem": "Two fails",
            "user_impact": "Two is blocked",
            "severity": "medium",
            "confidence": 0.8,
            "evidence_atom_ids": ["atom:two"],
            "evidence_summary": "Two observed failure",
        },
    ]
    partial = json.dumps([_priority_decision("problem:one", "atom:one")])
    valid = json.dumps(
        [
            _priority_decision("problem:one", "atom:one"),
            _priority_decision("problem:two", "atom:two"),
        ]
    )
    calls: list[dict[str, Any]] = []

    def fake_run_stage_prompt_json(**kwargs: Any) -> SimpleNamespace:
        calls.append(dict(kwargs))
        if len(calls) == 2:
            raise RuntimeError("transient resumed transport loss")
        return _write_fake_invocation(
            kwargs=kwargs,
            response=partial if len(calls) == 1 else valid,
            session_id=_SESSION_ID,
            elapsed_seconds=1.0,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.prioritization.run_stage_prompt_json",
        fake_run_stage_prompt_json,
    )
    doc = _run_problem_prioritization_stage(
        atoms=[
            {"atom_id": "atom:one", "severity_hint": "high"},
            {"atom_id": "atom:two", "severity_hint": "medium"},
        ],
        problem_records=records,
        pipeline_manifest=manifest,
        artifacts_dir=tmp_path / "artifacts",
        out_json=tmp_path / "prioritized.json",
        out_md=tmp_path / "prioritized.md",
        agent="codex",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
        dry_run=False,
        stage_guidance_text="Prioritize every canonical problem.",
    )

    assert len(calls) == 3
    assert calls[1]["resume_session_id"] == _SESSION_ID
    assert calls[2]["resume_session_id"] == _SESSION_ID
    meta = doc["input_meta"]
    assert meta["prioritizer_correction_status"] == "corrected"
    assert (
        meta["prioritizer_correction_metrics"][
            "correction_invocation_failure_count"
        ]
        == 1
    )
    assert [
        attempt["status"] for attempt in meta["prioritizer_attempt_history"]
    ] == ["invalid", "invocation_failed", "verified"]


def test_stage2_improved_frontier_is_not_paused_by_elapsed_author_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    manifest = load_pipeline_prompt_manifest(repo_root / "configs" / "backlog_prompts")
    record = {
        "problem_id": "problem:one",
        "title": "One",
        "problem": "One fails",
        "user_impact": "One is blocked",
        "severity": "high",
        "confidence": 0.9,
        "evidence_atom_ids": ["atom:one"],
        "evidence_summary": "One observed failure",
    }
    responses = ["initial", "reduced", "best", "replacement", "regression", "valid"]
    errors_by_response = {
        "initial": [f"initial:error:{index}" for index in range(37)],
        "reduced": [f"reduced:error:{index}" for index in range(8)],
        "best": ["near_ready:error:a", "near_ready:error:b"],
        "replacement": [
            "replacement:error:a",
            "replacement:error:b",
            "replacement:error:c",
        ],
        "regression": [
            "replacement:error:a",
            "replacement:error:b",
            "replacement:error:c",
            "regression:error:d",
        ],
        "valid": [],
    }
    elapsed = [10.0, 20.0, 20.0, 5.0, 30.0, 5.0]
    calls: list[dict[str, Any]] = []

    def projection(response: str, **_kwargs: Any):
        errors = errors_by_response[response]
        return (
            [_priority_decision("problem:one", "atom:one")] if not errors else [],
            list(errors),
            ["priority_decision:problem:one"] if not errors else [],
        )

    def transport(**kwargs: Any) -> SimpleNamespace:
        index = len(calls)
        calls.append(dict(kwargs))
        return _write_fake_invocation(
            kwargs=kwargs,
            response=responses[index],
            session_id=_SESSION_ID,
            elapsed_seconds=elapsed[index],
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.prioritization._priority_response_projection",
        projection,
    )
    monkeypatch.setattr(
        "usertest_backlog.workflows.prioritization.run_stage_prompt_json",
        transport,
    )
    doc = _run_problem_prioritization_stage(
        atoms=[{"atom_id": "atom:one", "severity_hint": "high"}],
        problem_records=[record],
        pipeline_manifest=manifest,
        artifacts_dir=tmp_path / "artifacts",
        out_json=tmp_path / "prioritized.json",
        out_md=tmp_path / "prioritized.md",
        agent="codex",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
        dry_run=False,
        stage_guidance_text="Prioritize every canonical problem.",
    )

    assert len(calls) == 6
    assert doc["input_meta"]["prioritizer_correction_status"] == "corrected"
    assert doc["input_meta"]["prioritizer_correction_metrics"]["attempt_count"] == 6
    assert doc["items"][0]["model_priority_accepted"] is True


def test_stage2_nonblocking_fallback_preserves_valid_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    manifest = load_pipeline_prompt_manifest(repo_root / "configs" / "backlog_prompts")
    records = [
        {
            "problem_id": "problem:one",
            "title": "One",
            "problem": "One fails",
            "user_impact": "One is blocked",
            "severity": "high",
            "confidence": 0.9,
            "evidence_atom_ids": ["atom:one"],
            "evidence_summary": "One observed failure",
        },
        {
            "problem_id": "problem:two",
            "title": "Two",
            "problem": "Two fails",
            "user_impact": "Two is blocked",
            "severity": "medium",
            "confidence": 0.8,
            "evidence_atom_ids": ["atom:two"],
            "evidence_summary": "Two observed failure",
        },
    ]
    response = json.dumps([_priority_decision("problem:one", "atom:one")])

    def fake_run_stage_prompt_json(**kwargs: Any) -> SimpleNamespace:
        return _write_fake_invocation(
            kwargs=kwargs,
            response=response,
            session_id=None,
            elapsed_seconds=1.0,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.prioritization.run_stage_prompt_json",
        fake_run_stage_prompt_json,
    )
    doc = _run_problem_prioritization_stage(
        atoms=[
            {"atom_id": "atom:one", "severity_hint": "high"},
            {"atom_id": "atom:two", "severity_hint": "medium"},
        ],
        problem_records=records,
        pipeline_manifest=manifest,
        artifacts_dir=tmp_path / "artifacts",
        out_json=tmp_path / "prioritized.json",
        out_md=tmp_path / "prioritized.md",
        agent="claude",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
        dry_run=False,
        stage_guidance_text="Prioritize every canonical problem.",
    )

    by_id = {item["problem_id"]: item for item in doc["items"]}
    assert doc["input_meta"]["prioritizer_status"] == "nonblocking_fallback"
    assert (
        doc["input_meta"]["prioritizer_correction_status"]
        == "repairable_paused:continuation_unavailable"
    )
    assert by_id["problem:one"]["model_priority_accepted"] is True
    assert by_id["problem:two"]["model_priority_accepted"] is False
    assert by_id["problem:two"]["selected_for_research"] is True
    assert doc["input_meta"]["prioritizer_fallback_decision_count"] == 1


def test_stage4_accepts_honest_zero_option_correction_in_exact_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        json.dumps(
            {
                "problem_id": "problem:case",
                "optioning_status": "unsupported_status",
                "decision_rationale": "The envelope is wrong.",
                "options": [],
            }
        ),
        json.dumps(
            {
                "problem_id": "problem:case",
                "optioning_status": "insufficient_evidence",
                "decision_rationale": "The verified proof does not establish a safe control point.",
                "options": [],
            }
        ),
    ]
    calls: list[dict[str, Any]] = []

    def fake_run_stage_prompt_json(**kwargs: Any) -> SimpleNamespace:
        calls.append(dict(kwargs))
        return SimpleNamespace(
            response=responses[len(calls) - 1],
            agent_session_id=_SESSION_ID,
            resumed_from_session_id=kwargs.get("resume_session_id"),
            workspace_dir=Path(kwargs["workspace_dir"]),
            invocation_manifest_path=tmp_path / f"manifest-{len(calls)}.json",
            prompt_path=tmp_path / f"prompt-{len(calls)}.txt",
            response_path=tmp_path / f"response-{len(calls)}.txt",
            raw_events_path=tmp_path / f"events-{len(calls)}.jsonl",
            last_message_path=tmp_path / f"last-{len(calls)}.txt",
            stderr_path=tmp_path / f"stderr-{len(calls)}.txt",
            elapsed_seconds=1.0 if len(calls) == 1 else 0.5,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_options.run_stage_prompt_json",
        fake_run_stage_prompt_json,
    )
    result = _run_optioning_prompt_with_correction(
        stage="solution_optioning",
        prompt="ORIGINAL UNIQUE OPTIONING EVIDENCE",
        out_dir=tmp_path,
        tag="solution_optioning_001",
        agent="codex",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
        workspace_dir=tmp_path,
        expected_problem_id="problem:case",
        repo_revision="a" * 40,
        known_family_ids={"most_direct"},
        research_dossier={},
    )

    assert len(calls) == 2
    assert calls[1]["resume_session_id"] == _SESSION_ID
    assert Path(calls[0]["workspace_dir"]).resolve() == Path(
        calls[1]["workspace_dir"]
    ).resolve()
    assert "ORIGINAL UNIQUE OPTIONING EVIDENCE" not in str(calls[1]["prompt"])
    assert "solution_optioner_invalid_status" in str(calls[1]["prompt"])
    assert result["correction_status"] == "corrected"
    assert result["options"] == []
    assert result["outcome"]["optioning_status"] == "insufficient_evidence"
    assert result["correction_metrics"]["attempt_count"] == 2
    assert result["correction_metrics"]["repaired"] is True


def test_stage4_improved_frontier_is_not_paused_by_elapsed_author_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = ["initial", "reduced", "best", "replacement", "regression", "valid"]
    errors_by_response = {
        "initial": [f"initial:error:{index}" for index in range(37)],
        "reduced": [f"reduced:error:{index}" for index in range(8)],
        "best": ["near_ready:error:a", "near_ready:error:b"],
        "replacement": [
            "replacement:error:a",
            "replacement:error:b",
            "replacement:error:c",
        ],
        "regression": [
            "replacement:error:a",
            "replacement:error:b",
            "replacement:error:c",
            "regression:error:d",
        ],
        "valid": [],
    }
    elapsed = [10.0, 20.0, 20.0, 5.0, 30.0, 5.0]
    calls: list[dict[str, Any]] = []

    def projection(response: str, **_kwargs: Any):
        errors = errors_by_response[response]
        return (
            {
                "problem_id": "problem:case",
                "optioning_status": (
                    "insufficient_evidence" if not errors else "invalid_output"
                ),
                "decision_rationale": "No safe option is established.",
                "option_count": 0,
                "rejected_option_count": 0,
            },
            [],
            list(errors),
            [],
        )

    def transport(**kwargs: Any) -> SimpleNamespace:
        index = len(calls)
        calls.append(dict(kwargs))
        return _write_fake_invocation(
            kwargs=kwargs,
            response=responses[index],
            session_id=_SESSION_ID,
            elapsed_seconds=elapsed[index],
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_options._optioning_response_projection",
        projection,
    )
    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_options.run_stage_prompt_json",
        transport,
    )
    result = _run_optioning_prompt_with_correction(
        stage="solution_optioning",
        prompt="ORIGINAL OPTIONING EVIDENCE",
        out_dir=tmp_path,
        tag="solution_optioning_001",
        agent="codex",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
        workspace_dir=tmp_path,
        expected_problem_id="problem:case",
        repo_revision="f" * 40,
        known_family_ids=set(),
        research_dossier={},
    )

    assert len(calls) == 6
    assert result["correction_status"] == "corrected"
    assert result["correction_metrics"]["attempt_count"] == 6
    assert result["outcome"]["optioning_status"] == "insufficient_evidence"


def test_stage4_retries_fresh_until_codex_author_session_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = json.dumps(
        {
            "problem_id": "problem:case",
            "optioning_status": "insufficient_evidence",
            "decision_rationale": "The proof does not establish a safe control point.",
            "options": [],
        }
    )
    calls: list[dict[str, Any]] = []

    def fake_run_stage_prompt_json(**kwargs: Any) -> SimpleNamespace:
        calls.append(dict(kwargs))
        return SimpleNamespace(
            response=response,
            agent_session_id=None if len(calls) == 1 else _SESSION_ID,
            resumed_from_session_id=kwargs.get("resume_session_id"),
            workspace_dir=Path(kwargs["workspace_dir"]),
            invocation_manifest_path=tmp_path / f"manifest-{len(calls)}.json",
            prompt_path=tmp_path / f"prompt-{len(calls)}.txt",
            response_path=tmp_path / f"response-{len(calls)}.txt",
            raw_events_path=tmp_path / f"events-{len(calls)}.jsonl",
            last_message_path=tmp_path / f"last-{len(calls)}.txt",
            stderr_path=tmp_path / f"stderr-{len(calls)}.txt",
            elapsed_seconds=30.0 if len(calls) == 1 else 1.0,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_options.run_stage_prompt_json",
        fake_run_stage_prompt_json,
    )
    result = _run_optioning_prompt_with_correction(
        stage="solution_optioning",
        prompt="ORIGINAL OPTIONING EVIDENCE",
        out_dir=tmp_path,
        tag="solution_optioning_001",
        agent="codex",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
        workspace_dir=tmp_path,
        expected_problem_id="problem:case",
        repo_revision="d" * 40,
        known_family_ids={"most_direct"},
        research_dossier={},
    )

    assert len(calls) == 2
    assert calls[0]["resume_session_id"] is None
    assert calls[1]["resume_session_id"] is None
    assert calls[1]["prompt"] == calls[0]["prompt"]
    assert result["correction_status"] == "accepted"
    assert result["correction_metrics"]["attempt_count"] == 2
    assert result["correction_metrics"]["session_acquisition_retry_count"] == 1
    assert len(result["attempt_history"]) == 2
    assert result["attempt_history"][0]["agent_session_id"] is None
    assert result["attempt_history"][1]["agent_session_id"] == _SESSION_ID


def test_stage4_transient_exact_session_exception_retries_same_author(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = json.dumps(
        {
            "problem_id": "problem:case",
            "optioning_status": "unsupported_status",
            "decision_rationale": "The envelope is wrong.",
            "options": [],
        }
    )
    valid = json.dumps(
        {
            "problem_id": "problem:case",
            "optioning_status": "insufficient_evidence",
            "decision_rationale": "The evidence does not establish a safe mechanism.",
            "options": [],
        }
    )
    calls: list[dict[str, Any]] = []

    def fake_run_stage_prompt_json(**kwargs: Any) -> SimpleNamespace:
        calls.append(dict(kwargs))
        if len(calls) == 2:
            raise RuntimeError("transient resumed transport loss")
        return SimpleNamespace(
            response=invalid if len(calls) == 1 else valid,
            agent_session_id=_SESSION_ID,
            resumed_from_session_id=kwargs.get("resume_session_id"),
            workspace_dir=Path(kwargs["workspace_dir"]),
            invocation_manifest_path=tmp_path / f"manifest-{len(calls)}.json",
            prompt_path=tmp_path / f"prompt-{len(calls)}.txt",
            response_path=tmp_path / f"response-{len(calls)}.txt",
            raw_events_path=tmp_path / f"events-{len(calls)}.jsonl",
            last_message_path=tmp_path / f"last-{len(calls)}.txt",
            stderr_path=tmp_path / f"stderr-{len(calls)}.txt",
            elapsed_seconds=1.0,
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_options.run_stage_prompt_json",
        fake_run_stage_prompt_json,
    )
    result = _run_optioning_prompt_with_correction(
        stage="solution_optioning",
        prompt="ORIGINAL OPTIONING EVIDENCE",
        out_dir=tmp_path,
        tag="solution_optioning_001",
        agent="codex",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
        workspace_dir=tmp_path,
        expected_problem_id="problem:case",
        repo_revision="e" * 40,
        known_family_ids=set(),
        research_dossier={},
    )

    assert len(calls) == 3
    assert calls[1]["resume_session_id"] == _SESSION_ID
    assert calls[2]["resume_session_id"] == _SESSION_ID
    assert result["correction_status"] == "corrected"
    assert result["correction_metrics"]["correction_invocation_failure_count"] == 1
    assert [attempt["status"] for attempt in result["attempt_history"]] == [
        "invalid",
        "invocation_failed",
        "verified",
    ]


def test_stage4_incomplete_correction_retains_independently_valid_option(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    option = {"option_id": "option:case:valid", "problem_id": "problem:case"}

    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_options._optioning_response_projection",
        lambda *_args, **_kwargs: (
            {
                "problem_id": "problem:case",
                "optioning_status": "options_produced",
                "decision_rationale": "One option is valid and one sibling option needs repair.",
                "option_count": 1,
                "rejected_option_count": 1,
            },
            [option],
            ["previously_unknown_quality_error"],
            ["solution_option:option:case:valid"],
        ),
    )
    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_options.run_stage_prompt_json",
        lambda **kwargs: SimpleNamespace(
            response="invalid envelope with one retained option",
            agent_session_id=None,
            resumed_from_session_id=kwargs.get("resume_session_id"),
            workspace_dir=Path(kwargs["workspace_dir"]),
            invocation_manifest_path=tmp_path / "manifest.json",
            prompt_path=tmp_path / "prompt.txt",
            response_path=tmp_path / "response.txt",
            raw_events_path=tmp_path / "events.jsonl",
            last_message_path=tmp_path / "last.txt",
            stderr_path=tmp_path / "stderr.txt",
            elapsed_seconds=1.0,
        ),
    )
    result = _run_optioning_prompt_with_correction(
        stage="solution_optioning",
        prompt="optioning prompt",
        out_dir=tmp_path,
        tag="solution_optioning_001",
        agent="claude",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
        workspace_dir=tmp_path,
        expected_problem_id="problem:case",
        repo_revision="b" * 40,
        known_family_ids={"most_direct"},
        research_dossier={},
    )

    assert result["correction_status"] == "repairable_paused:continuation_unavailable"
    assert result["options"] == [option]
    assert result["outcome"]["optioning_status"] == "options_produced"
    assert result["outcome"]["option_count"] == 1
    assert result["correction_metrics"]["repairable_paused"] is True


def test_stage4_malformed_envelope_does_not_advance_salvaged_option(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    salvaged = {"option_id": "option:case:salvaged", "problem_id": "problem:case"}
    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_options._optioning_response_projection",
        lambda *_args, **_kwargs: (
            {
                "problem_id": "problem:case",
                "optioning_status": "invalid_output",
                "decision_rationale": "The envelope contradicts its options.",
                "option_count": 1,
                "rejected_option_count": 0,
            },
            [salvaged],
            ["solution_optioner_invalid_status:'contradictory'"],
            ["solution_option:option:case:salvaged"],
        ),
    )
    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_options.run_stage_prompt_json",
        lambda **kwargs: SimpleNamespace(
            response="contradictory envelope",
            agent_session_id=None,
            resumed_from_session_id=kwargs.get("resume_session_id"),
            workspace_dir=Path(kwargs["workspace_dir"]),
            invocation_manifest_path=tmp_path / "manifest.json",
            prompt_path=tmp_path / "prompt.txt",
            response_path=tmp_path / "response.txt",
            raw_events_path=tmp_path / "events.jsonl",
            last_message_path=tmp_path / "last.txt",
            stderr_path=tmp_path / "stderr.txt",
            elapsed_seconds=1.0,
        ),
    )
    result = _run_optioning_prompt_with_correction(
        stage="solution_optioning",
        prompt="optioning prompt",
        out_dir=tmp_path,
        tag="solution_optioning_001",
        agent="claude",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
        workspace_dir=tmp_path,
        expected_problem_id="problem:case",
        repo_revision="c" * 40,
        known_family_ids={"most_direct"},
        research_dossier={},
    )

    assert result["options"] == []
    assert result["outcome"]["optioning_status"] == "insufficient_evidence"
    assert result["outcome"]["retained_valid_option_count"] == 1
    assert result["outcome"]["valid_item_keys"] == [
        "solution_option:option:case:salvaged"
    ]
