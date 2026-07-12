from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from usertest_backlog.workflows import planning_healing as planning
from usertest_backlog.workflows import selection_healing

_PLANNER_SESSION = "88888888-8888-4888-8888-888888888888"


def _plan(plan_id: str = "plan:case:1", *, errors: list[str] | None = None) -> dict[str, Any]:
    return {
        "change_plan_id": plan_id,
        "case_id": "case:case",
        "problem_id": "problem:case",
        "selected_option_id": "option:case",
        "repo_revision": "a" * 40,
        "quality_errors": list(errors or []),
    }


class _ScriptedTransport:
    def __init__(self, tmp_path: Path, script: list[dict[str, Any]]) -> None:
        self.tmp_path = tmp_path
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> SimpleNamespace:
        assert self.script, f"unexpected planner invocation: {kwargs['tag']}"
        item = self.script.pop(0)
        if "resume" in item:
            assert kwargs.get("resume_session_id") == item["resume"]
        self.calls.append(dict(kwargs))
        if "error" in item:
            raise RuntimeError(str(item["error"]))
        index = len(self.calls)
        return SimpleNamespace(
            response=item["response"],
            agent_session_id=item.get("session", _PLANNER_SESSION),
            resumed_from_session_id=kwargs.get("resume_session_id"),
            workspace_dir=Path(kwargs["workspace_dir"]),
            invocation_manifest_path=self.tmp_path / f"manifest-{index}.json",
            prompt_path=self.tmp_path / f"prompt-{index}.txt",
            response_path=self.tmp_path / f"response-{index}.txt",
            raw_events_path=self.tmp_path / f"events-{index}.jsonl",
            last_message_path=self.tmp_path / f"last-{index}.txt",
            stderr_path=self.tmp_path / f"stderr-{index}.txt",
            elapsed_seconds=float(item.get("elapsed", 1.0)),
        )


def _patch_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        planning,
        "parse_change_plan_list",
        lambda response, **_kwargs: (json.loads(response), []),
    )
    monkeypatch.setattr(
        planning,
        "build_plan_target_contract",
        lambda plan, **_kwargs: {
            "case_id": plan.get("case_id"),
            "problem_id": plan.get("problem_id"),
        },
    )

    def bind(plan: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        binding_error = plan.get("binding_error")
        if isinstance(binding_error, str):
            raise ValueError(binding_error)
        return dict(plan)

    def assign(plan: dict[str, Any]) -> dict[str, Any]:
        result = dict(plan)
        projection = {key: value for key, value in result.items() if key != "plan_revision_id"}
        result["plan_revision_id"] = "plan_revision:" + sha256(
            json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return result

    monkeypatch.setattr(planning, "bind_plan_outcome_oracle", bind)
    monkeypatch.setattr(planning, "assign_plan_revision_id", assign)
    monkeypatch.setattr(
        planning,
        "change_plan_quality_errors",
        lambda plan, **_kwargs: list(plan.get("quality_errors") or []),
    )


def _run_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: list[dict[str, Any]],
    *,
    research_dossier: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], _ScriptedTransport]:
    _patch_contracts(monkeypatch)
    transport = _ScriptedTransport(tmp_path, script)
    monkeypatch.setattr(selection_healing, "run_stage_prompt_json", transport)
    result = planning.run_stage6_live_case(
        problem_id="problem:case",
        case_id="case:case",
        selected_option_id="option:case",
        index=1,
        planner_prompt="Inspect the repository and return a complete plan.",
        stage_artifacts_dir=tmp_path / "artifacts",
        target_repo_root=tmp_path,
        repo_revision="a" * 40,
        problem_record={"problem_id": "problem:case"},
        research_dossier=research_dossier or {"problem_id": "problem:case"},
        selection_decision={"selected_option_id": "option:case"},
        agent="codex",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
    )
    assert transport.script == []
    return result, transport


def test_one_pass_valid_plan_has_no_extra_planning_ritual(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, transport = _run_case(
        tmp_path,
        monkeypatch,
        [{"response": json.dumps([_plan()]), "elapsed": 2.0}],
    )

    assert result["status"] == "planned"
    assert len(result["plans"]) == 1
    assert result["partial_valid_plans"] == []
    assert len(transport.calls) == 1
    assert result["metrics"]["attempt_count"] == 1
    assert result["metrics"]["correction_turn_count"] == 0


def test_unknown_planner_error_returns_to_same_exact_author_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, transport = _run_case(
        tmp_path,
        monkeypatch,
        [
            {
                "response": json.dumps([_plan(errors=["novel_planner_validator_error:x42"])]),
                "elapsed": 1.0,
            },
            {
                "response": json.dumps([_plan()]),
                "resume": _PLANNER_SESSION,
                "elapsed": 0.25,
            },
        ],
    )

    assert result["status"] == "planned"
    assert len(transport.calls) == 2
    assert transport.calls[1]["resume_session_id"] == _PLANNER_SESSION
    correction_prompt = str(transport.calls[1]["prompt"])
    assert "novel_planner_validator_error:x42" in correction_prompt
    assert "SAME-AUTHOR PLANNER RESPONSE CORRECTION" in correction_prompt


def test_transient_pre_session_failure_retries_fresh_then_uses_acquired_author(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, transport = _run_case(
        tmp_path,
        monkeypatch,
        [
            {"error": "transient transport launch failure"},
            {"response": json.dumps([_plan()]), "session": _PLANNER_SESSION},
        ],
    )

    assert result["status"] == "planned"
    assert len(transport.calls) == 2
    assert all(call.get("resume_session_id") is None for call in transport.calls)
    assert result["metrics"]["session_acquisition_retry_count"] == 1
    assert result["metrics"]["correction_turn_count"] == 0
    assert len(result["role_run"]["attempt_history"]) == 2


def test_repeated_equivalent_pre_session_failure_pauses_without_fixed_retry_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = json.dumps([_plan()])
    result, transport = _run_case(
        tmp_path,
        monkeypatch,
        [
            {"response": response, "session": None, "elapsed": 0.1},
            {"response": response, "session": None, "elapsed": 0.1},
        ],
    )

    assert result["status"] == (
        "repairable_paused:author_session_acquisition_repeated"
    )
    assert result["plans"] == []
    assert len(transport.calls) == 2
    assert result["metrics"]["session_acquisition_retry_count"] == 1
    assert len(result["role_run"]["attempt_history"]) == 2


def test_fewer_errors_is_progress_even_when_every_error_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, transport = _run_case(
        tmp_path,
        monkeypatch,
        [
            {
                "response": json.dumps([_plan(errors=["old:error:a", "old:error:b"])]),
                "elapsed": 1.0,
            },
            {
                "response": json.dumps([_plan(errors=["new:error:c"])]),
                "resume": _PLANNER_SESSION,
                # This exceeds the initial author cost. Objective progress must reset
                # correction economics before the pause policy is evaluated.
                "elapsed": 2.0,
            },
            {
                "response": json.dumps([_plan()]),
                "resume": _PLANNER_SESSION,
                "elapsed": 0.5,
            },
        ],
    )

    assert result["status"] == "planned"
    assert len(transport.calls) == 3
    progress = result["role_run"]["attempt_history"][1]["correction_progress"]
    assert progress["before_error_count"] == 2
    assert progress["after_error_count"] == 1
    assert progress["reason"] == "error_count_decreased"
    assert progress["reset_progress_clock"] is True


def test_transient_exact_session_planner_exception_retries_same_author(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, transport = _run_case(
        tmp_path,
        monkeypatch,
        [
            {
                "response": json.dumps([_plan(errors=["plan:error"])]),
                "session": _PLANNER_SESSION,
            },
            {
                "error": "transient resumed transport loss",
                "resume": _PLANNER_SESSION,
            },
            {
                "response": json.dumps([_plan()]),
                "resume": _PLANNER_SESSION,
            },
        ],
    )

    assert result["status"] == "planned"
    assert len(transport.calls) == 3
    assert transport.calls[1]["resume_session_id"] == _PLANNER_SESSION
    assert transport.calls[2]["resume_session_id"] == _PLANNER_SESSION
    assert result["metrics"]["correction_invocation_failure_count"] == 1
    history = result["role_run"]["attempt_history"]
    assert [attempt["status"] for attempt in history] == [
        "invalid",
        "invocation_failed",
        "verified",
    ]
    assert history[1]["agent_session_id"] == _PLANNER_SESSION


def test_incomplete_partial_plan_is_retained_but_never_exported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = json.dumps(
        [
            _plan("plan:case:valid"),
            _plan("plan:case:invalid", errors=["unknown_late_plan_error"]),
        ]
    )
    result, transport = _run_case(
        tmp_path,
        monkeypatch,
        [
            {"response": response, "elapsed": 1.0},
            {"response": response, "resume": _PLANNER_SESSION, "elapsed": 0.1},
            {"response": response, "resume": _PLANNER_SESSION, "elapsed": 0.1},
        ],
    )

    assert result["status"] == "stalled:same_state_repeated_after_feedback"
    assert result["plans"] == []
    assert [plan["change_plan_id"] for plan in result["partial_valid_plans"]] == [
        "plan:case:valid"
    ]
    assert result["metrics"]["retained_partial_plan_count"] == 1
    assert result["metrics"]["emitted_plan_count"] == 0
    assert len(transport.calls) == 3


def test_server_bound_outcome_gap_returns_to_research_without_planner_churn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _plan()
    candidate["binding_error"] = "research_positive_outcome_contract_missing"
    result, transport = _run_case(
        tmp_path,
        monkeypatch,
        [{"response": json.dumps([candidate]), "elapsed": 1.0}],
    )

    assert result["status"] == "research_required"
    assert result["plans"] == []
    assert result["partial_valid_plans"] == []
    assert len(result["research_required"]) == 1
    assert result["research_required"][0]["source"] == "server_evidence_binding"
    assert "research_positive_outcome_contract_missing" in result["research_required"][0][
        "reasons"
    ][0]
    assert len(transport.calls) == 1


def test_grounded_research_return_accepts_unforeseen_open_block_description(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = json.dumps(
        [
            {
                "planning_status": "research_required",
                "case_id": "case:case",
                "problem_id": "problem:case",
                "selected_option_id": "option:case",
                "return_to_stage": "repro_research",
                "evidence_gaps": [
                    {
                        "gap": "The cross-version fallback contract is not observed.",
                        "blocks": [
                            "cross-version compatibility behavior at the provider boundary"
                        ],
                        "evidence_needed": "Replay both supported provider protocol versions.",
                        "evidence_refs": ["boundary:provider-version-unobserved"],
                    }
                ],
                "rationale": "Guessing the fallback could break an existing provider version.",
            }
        ]
    )
    result, transport = _run_case(
        tmp_path,
        monkeypatch,
        [{"response": response}],
        research_dossier={
            "problem_id": "problem:case",
            "evidence_boundaries": ["boundary:provider-version-unobserved"],
        },
    )

    assert result["status"] == "research_required"
    assert result["research_required"][0]["evidence_gaps"][0]["blocks"] == [
        "cross-version compatibility behavior at the provider boundary"
    ]
    assert len(transport.calls) == 1


def test_change_planner_prompt_defines_grounded_research_return_contract() -> None:
    prompt_path = (
        Path(__file__).resolve().parents[3]
        / "configs"
        / "backlog_prompts"
        / "change_planner.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")

    assert '"planning_status": "research_required"' in prompt
    assert "Never mix ready plans and a" in prompt
    assert "open language" in prompt
    assert "same planner session" in prompt
