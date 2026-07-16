from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from usertest_backlog.workflows import selection_healing as healing

_SELECTOR_SESSION = "11111111-1111-4111-8111-111111111111"
_FALSIFIER_1_SESSION = "22222222-2222-4222-8222-222222222222"
_FALSIFIER_2_SESSION = "33333333-3333-4333-8333-333333333333"
_FALSIFIER_3_SESSION = "77777777-7777-4777-8777-777777777777"
_FALSIFIER_4_SESSION = "88888888-8888-4888-8888-888888888888"
_OPTIONER_SESSION = "44444444-4444-4444-8444-444444444444"
_LABELER_SESSION = "55555555-5555-4555-8555-555555555555"
_UNKNOWN_SESSION = "66666666-6666-4666-8666-666666666666"


def _option(option_id: str) -> dict[str, Any]:
    return {
        "problem_id": "problem:case",
        "option_id": option_id,
        "family_id": "most_direct",
    }


def _selection(option_id: str, *, rationale: str | None = None) -> str:
    return json.dumps(
        [
            {
                "problem_id": "problem:case",
                "selected_option_id": option_id,
                "selected_family_id": "most_direct",
                "selection_rationale": rationale or f"Select {option_id} by causal fit.",
                "repo_intent_alignment": "The option preserves repository intent.",
                "why_other_options_were_not_selected": "Their causal fit is weaker.",
                "needs_ux_review": False,
                "selection_status": "selected",
                "causal_coverage_evaluation": {
                    "mechanism_fit": "Matches the established mechanism.",
                    "accepted_unsupported_assumptions": [],
                    "accepted_residual_risks": [],
                    "class_level_evidence_sufficient": False,
                },
            }
        ]
    )


def _revision_request(
    *,
    rationale: str = "Neither existing option controls the failing boundary.",
) -> str:
    return json.dumps(
        {
            "problem_id": "problem:case",
            "selection_status": "option_revision_requested",
            "revision_rationale": rationale,
            "option_gaps": ["Add an option at the verified control point."],
        }
    )


def _review(
    verdict: str,
    *,
    critical_findings: list[dict[str, Any]] | None = None,
) -> str:
    return json.dumps(
        {
            "problem_id": "problem:case",
            "selected_option_id": "unused-by-test-projection",
            "verdict": verdict,
            "strongest_counterargument": f"The independent review returned {verdict}.",
            "critical_findings": critical_findings or [],
            "unsupported_assumptions": [],
            "residual_risks": [],
            "material_risk_dispositions": [],
            "evidence_refs": [],
        }
    )


def _label() -> str:
    return json.dumps(
        {
            "change_surface": {
                "user_visible": False,
                "kinds": ["unknown"],
                "notes": "No verified user-visible surface.",
            },
            "component": "unknown",
            "intent_risk": "med",
            "confidence": 0.5,
            "evidence_atom_ids_used": [],
        }
    )


class _ScriptedTransport:
    def __init__(self, tmp_path: Path, script: list[dict[str, Any]]) -> None:
        self.tmp_path = tmp_path
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> SimpleNamespace:
        assert self.script, f"unexpected invocation: {kwargs['stage']} {kwargs['tag']}"
        item = self.script.pop(0)
        assert kwargs["stage"] == item["stage"]
        if "resume" in item:
            assert kwargs.get("resume_session_id") == item["resume"]
        self.calls.append(dict(kwargs))
        index = len(self.calls)
        return SimpleNamespace(
            response=item["response"],
            agent_session_id=item["session"],
            resumed_from_session_id=kwargs.get("resume_session_id"),
            workspace_dir=Path(kwargs["workspace_dir"]),
            invocation_manifest_path=self.tmp_path / f"manifest-{index}.json",
            prompt_path=self.tmp_path / f"prompt-{index}.txt",
            response_path=self.tmp_path / f"response-{index}.txt",
            raw_events_path=self.tmp_path / f"events-{index}.jsonl",
            last_message_path=self.tmp_path / f"last-{index}.txt",
            stderr_path=self.tmp_path / f"stderr-{index}.txt",
            elapsed_seconds=float(item.get("elapsed", 0.0)),
        )


def _patch_simple_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(healing, "selection_quality_errors", lambda *args, **kwargs: [])

    def falsifier_projection(
        response: str,
        **_kwargs: Any,
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        try:
            raw = json.loads(response)
        except json.JSONDecodeError as exc:
            return {"review": None}, [f"unknown_falsifier_format_error:{exc.msg}"], []
        if raw.get("format_error"):
            return {"review": raw}, [str(raw["format_error"])], []
        if raw.get("verdict") == "accept" and raw.get("critical_findings"):
            return {"review": raw}, ["falsification_accepts_critical_finding"], []
        return {"review": raw}, [], [f"falsifier_review:{raw.get('verdict')}"]

    def optioning_projection(
        response: str,
        **_kwargs: Any,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], list[str]]:
        raw = json.loads(response)
        options = [dict(item) for item in raw.get("options", [])]
        return (
            {
                "problem_id": "problem:case",
                "optioning_status": raw["optioning_status"],
                "decision_rationale": raw["decision_rationale"],
                "option_count": len(options),
                "rejected_option_count": 0,
            },
            options,
            [],
            [f"solution_option:{item['option_id']}" for item in options],
        )

    monkeypatch.setattr(healing, "_falsifier_response_projection", falsifier_projection)
    monkeypatch.setattr(healing, "_optioning_response_projection", optioning_projection)


def _stage4_doc(tmp_path: Path) -> dict[str, Any]:
    return {
        "input_meta": {
            "optioning_correction_runs": [
                {
                    "problem_id": "problem:case",
                    "attempt_history": [
                        {
                            "status": "verified",
                            "attempt_tag": "solution_optioning_001",
                            "agent_session_id": _OPTIONER_SESSION,
                            "workspace_dir": str(tmp_path.resolve()),
                            "repo_revision": "a" * 40,
                            "prompt_sha256": "b" * 64,
                            "response_sha256": "c" * 64,
                            "elapsed_seconds": 1.0,
                        }
                    ],
                }
            ]
        }
    }


def _run_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: list[dict[str, Any]],
    *,
    options: list[dict[str, Any]] | None = None,
    stage4_doc: dict[str, Any] | None = None,
    selection_validator: Any | None = None,
) -> tuple[dict[str, Any], _ScriptedTransport]:
    _patch_simple_contracts(monkeypatch)
    if selection_validator is not None:
        monkeypatch.setattr(healing, "selection_quality_errors", selection_validator)
    transport = _ScriptedTransport(tmp_path, script)
    monkeypatch.setattr(healing, "run_stage_prompt_json", transport)
    result = healing.run_stage5_live_case(
        problem_id="problem:case",
        index=1,
        selector_prompt="initial selector assignment",
        falsifier_template=(
            "repo={{REPO_CONTEXT_JSON}} problem={{PROBLEM_RECORD_JSON}} "
            "research={{RESEARCH_DOSSIER_JSON}} options={{SOLUTION_OPTIONS_JSON}} "
            "selection={{SELECTION_DECISION_JSON}}"
        ),
        labeler_template=("selected={{SELECTED_SOLUTION_JSON}} evidence={{EVIDENCE_ATOMS_JSON}}"),
        repo_context={"workspace": str(tmp_path), "head_revision": "a" * 40},
        problem_record={"problem_id": "problem:case", "title": "Case"},
        prompt_dossier={"problem_id": "problem:case"},
        research_dossier={"problem_id": "problem:case"},
        initial_options=options or [_option("option:a"), _option("option:b")],
        stage4_doc=stage4_doc,
        stage_artifacts_dir=tmp_path / "artifacts",
        target_repo_root=tmp_path,
        repo_revision="a" * 40,
        evidence_atoms_preview=[],
        evidence_atom_ids=[],
        known_family_ids={"most_direct"},
        agent="codex",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
    )
    assert transport.script == []
    return result, transport


def test_complete_selection_gate_runs_after_labeler_metadata_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations: list[tuple[bool, bool]] = []

    def selection_validator(
        decision: dict[str, Any], *, require_complete: bool = False, **_kwargs: Any
    ):
        has_surface = isinstance(decision.get("change_surface"), dict)
        if "falsification_review" in decision:
            observations.append((require_complete, has_surface))
        return [] if not require_complete or has_surface else ["selection_change_surface_missing"]

    result, _ = _run_case(
        tmp_path,
        monkeypatch,
        [
            {
                "stage": "solution_selection",
                "response": _selection("option:a"),
                "session": _SELECTOR_SESSION,
            },
            {
                "stage": "solution_falsification",
                "response": _review("accept"),
                "session": _FALSIFIER_1_SESSION,
            },
            {
                "stage": "selected_solution_labeler",
                "response": _label(),
                "session": _LABELER_SESSION,
            },
        ],
        selection_validator=selection_validator,
    )

    assert result["status"] == "selected"
    assert observations == [(False, False), (True, True)]


def test_falsifier_format_error_repairs_in_same_falsifier_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, transport = _run_case(
        tmp_path,
        monkeypatch,
        [
            {
                "stage": "solution_selection",
                "response": _selection("option:a"),
                "session": _SELECTOR_SESSION,
            },
            {
                "stage": "solution_falsification",
                "response": "not-json",
                "session": _FALSIFIER_1_SESSION,
            },
            {
                "stage": "solution_falsification",
                "response": _review("accept"),
                "session": _FALSIFIER_1_SESSION,
                "resume": _FALSIFIER_1_SESSION,
            },
            {
                "stage": "selected_solution_labeler",
                "response": _label(),
                "session": _LABELER_SESSION,
            },
        ],
    )

    assert result["status"] == "selected"
    json.dumps(result)
    falsifier_run = next(run for run in result["role_runs"] if run["role"] == "falsifier")
    assert falsifier_run["status"] == "corrected"
    assert len(falsifier_run["attempt_history"]) == 2
    assert "unknown_falsifier_format_error" in str(
        falsifier_run["attempt_history"][0]["validation_errors"]
    )
    assert transport.calls[2]["resume_session_id"] == _FALSIFIER_1_SESSION


def test_reject_returns_to_selector_then_uses_fresh_independent_falsifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, transport = _run_case(
        tmp_path,
        monkeypatch,
        [
            {
                "stage": "solution_selection",
                "response": _selection("option:a"),
                "session": _SELECTOR_SESSION,
            },
            {
                "stage": "solution_falsification",
                "response": _review("reject"),
                "session": _FALSIFIER_1_SESSION,
            },
            {
                "stage": "solution_selection",
                "response": _selection("option:b"),
                "session": _SELECTOR_SESSION,
                "resume": _SELECTOR_SESSION,
            },
            {
                "stage": "solution_falsification",
                "response": _review("accept"),
                "session": _FALSIFIER_2_SESSION,
            },
            {
                "stage": "selected_solution_labeler",
                "response": _label(),
                "session": _LABELER_SESSION,
            },
        ],
    )

    assert result["status"] == "selected"
    assert result["decision"]["selected_option_id"] == "option:b"
    falsifiers = [run for run in result["role_runs"] if run["role"] == "falsifier"]
    assert [run["session_id"] for run in falsifiers] == [
        _FALSIFIER_1_SESSION,
        _FALSIFIER_2_SESSION,
    ]
    selector_feedback = [item for item in result["feedback"] if item["to_role"] == "selector"]
    assert selector_feedback[0]["feedback"]["verdict"] == "reject"
    assert transport.calls[2]["resume_session_id"] == _SELECTOR_SESSION


def test_inadequate_options_resume_original_optioner_then_refalsify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revised_response = json.dumps(
        {
            "optioning_status": "options_produced",
            "decision_rationale": "The critique supports a control-point option.",
            "options": [_option("option:revised")],
        }
    )
    result, transport = _run_case(
        tmp_path,
        monkeypatch,
        [
            {
                "stage": "solution_selection",
                "response": _selection("option:a"),
                "session": _SELECTOR_SESSION,
            },
            {
                "stage": "solution_falsification",
                "response": _review("reject"),
                "session": _FALSIFIER_1_SESSION,
            },
            {
                "stage": "solution_selection",
                "response": _revision_request(),
                "session": _SELECTOR_SESSION,
                "resume": _SELECTOR_SESSION,
            },
            {
                "stage": "solution_optioning",
                "response": revised_response,
                "session": _OPTIONER_SESSION,
                "resume": _OPTIONER_SESSION,
            },
            {
                "stage": "solution_selection",
                "response": _selection("option:revised"),
                "session": _SELECTOR_SESSION,
                "resume": _SELECTOR_SESSION,
            },
            {
                "stage": "solution_falsification",
                "response": _review("accept"),
                "session": _FALSIFIER_2_SESSION,
            },
            {
                "stage": "selected_solution_labeler",
                "response": _label(),
                "session": _LABELER_SESSION,
            },
        ],
        stage4_doc=_stage4_doc(tmp_path),
    )

    assert result["status"] == "selected"
    assert result["decision"]["selected_option_id"] == "option:revised"
    assert [item["option_id"] for item in result["revised_options"]] == ["option:revised"]
    optioner_call = next(call for call in transport.calls if call["stage"] == "solution_optioning")
    assert optioner_call["resume_session_id"] == _OPTIONER_SESSION
    assert len([run for run in result["role_runs"] if run["role"] == "falsifier"]) == 2


def test_optioner_no_safe_option_preserves_case_as_explicit_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_safe_response = json.dumps(
        {
            "optioning_status": "no_safe_option",
            "decision_rationale": "Every evidenced intervention leaves a blocking causal gap.",
            "options": [],
        }
    )
    result, _ = _run_case(
        tmp_path,
        monkeypatch,
        [
            {
                "stage": "solution_selection",
                "response": _revision_request(),
                "session": _SELECTOR_SESSION,
            },
            {
                "stage": "solution_optioning",
                "response": no_safe_response,
                "session": _OPTIONER_SESSION,
                "resume": _OPTIONER_SESSION,
            },
        ],
        stage4_doc=_stage4_doc(tmp_path),
    )

    assert result["status"] == "no_safe_option"
    assert result["decision"] is None
    assert result["outcome"]["selection_status"] == "no_safe_option"
    assert result["role_runs"][-1]["role"] == "optioner"
    assert result["feedback"][-1]["to_role"] == "optioner"


def test_accept_with_critical_finding_is_corrected_not_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    critical = [{"finding": "Root cause remains", "affects": "root_cause"}]
    result, _ = _run_case(
        tmp_path,
        monkeypatch,
        [
            {
                "stage": "solution_selection",
                "response": _selection("option:a"),
                "session": _SELECTOR_SESSION,
            },
            {
                "stage": "solution_falsification",
                "response": _review("accept", critical_findings=critical),
                "session": _FALSIFIER_1_SESSION,
            },
            {
                "stage": "solution_falsification",
                "response": _review("reject", critical_findings=critical),
                "session": _FALSIFIER_1_SESSION,
                "resume": _FALSIFIER_1_SESSION,
            },
            {
                "stage": "solution_selection",
                "response": _selection("option:b"),
                "session": _SELECTOR_SESSION,
                "resume": _SELECTOR_SESSION,
            },
            {
                "stage": "solution_falsification",
                "response": _review("accept"),
                "session": _FALSIFIER_2_SESSION,
            },
            {
                "stage": "selected_solution_labeler",
                "response": _label(),
                "session": _LABELER_SESSION,
            },
        ],
    )

    assert result["status"] == "selected"
    assert result["decision"]["selected_option_id"] == "option:b"
    first_falsifier = next(run for run in result["role_runs"] if run["role"] == "falsifier")
    assert first_falsifier["status"] == "corrected"
    assert "falsification_accepts_critical_finding" in str(
        first_falsifier["attempt_history"][0]["validation_errors"]
    )


def test_stalled_labeler_uses_neutral_label_without_invalidating_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _run_case(
        tmp_path,
        monkeypatch,
        [
            {
                "stage": "solution_selection",
                "response": _selection("option:a"),
                "session": _SELECTOR_SESSION,
            },
            {
                "stage": "solution_falsification",
                "response": _review("accept"),
                "session": _FALSIFIER_1_SESSION,
            },
            {
                "stage": "selected_solution_labeler",
                "response": "not-json",
                "session": _LABELER_SESSION,
            },
            {
                "stage": "selected_solution_labeler",
                "response": "not-json",
                "session": _LABELER_SESSION,
                "resume": _LABELER_SESSION,
            },
            {
                "stage": "selected_solution_labeler",
                "response": "not-json",
                "session": _LABELER_SESSION,
                "resume": _LABELER_SESSION,
            },
        ],
    )

    assert result["status"] == "selected"
    assert result["decision"]["selected_solution_label_status"] == "neutral_fallback"
    assert result["decision"]["change_surface"]["kinds"] == ["unknown"]
    labeler_run = next(run for run in result["role_runs"] if run["role"] == "labeler")
    assert labeler_run["status"] == "stalled:same_state_repeated_after_feedback"


def test_labeler_accepts_novel_typed_labels_without_closed_allowlist() -> None:
    payload, errors, valid_keys = healing._labeler_response_projection(
        json.dumps(
            {
                "change_surface": {
                    "user_visible": False,
                    "kinds": ["future_surface_kind"],
                    "notes": "A new typed surface emitted by a future labeler.",
                },
                "component": "future_component",
                "intent_risk": "context_dependent",
                "confidence": 0.4,
                "evidence_atom_ids_used": [],
            }
        ),
        problem_id="problem:case",
        allowed_evidence_atom_ids=set(),
    )

    assert errors == []
    assert valid_keys == ["selected_solution_label:problem:case"]
    assert payload["label"]["component"] == "future_component"


def test_exact_selector_cycle_stalls_without_fixed_cycle_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_a = _selection("option:a")
    result, _ = _run_case(
        tmp_path,
        monkeypatch,
        [
            {"stage": "solution_selection", "response": initial_a, "session": _SELECTOR_SESSION},
            {
                "stage": "solution_falsification",
                "response": _review("reject"),
                "session": _FALSIFIER_1_SESSION,
            },
            {
                "stage": "solution_selection",
                "response": _selection("option:b"),
                "session": _SELECTOR_SESSION,
                "resume": _SELECTOR_SESSION,
            },
            {
                "stage": "solution_falsification",
                "response": _review("reject"),
                "session": _FALSIFIER_2_SESSION,
            },
            {
                "stage": "solution_selection",
                "response": initial_a,
                "session": _SELECTOR_SESSION,
                "resume": _SELECTOR_SESSION,
            },
        ],
    )

    assert result["status"] == "stalled:selector_state_recurred"
    assert result["decision"] is None


def test_costly_cross_role_progress_continues_after_two_nonprogress_cycles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    findings = [
        {"finding": "Root-cause gap remains", "affects": "root_cause"},
        {"finding": "Outcome gap remains", "affects": "verification"},
    ]
    result, _ = _run_case(
        tmp_path,
        monkeypatch,
        [
            {
                "stage": "solution_selection",
                "response": _selection("option:a"),
                "session": _SELECTOR_SESSION,
                "elapsed": 2.0,
            },
            {
                "stage": "solution_falsification",
                "response": _review("reject", critical_findings=findings),
                "session": _FALSIFIER_1_SESSION,
                "elapsed": 2.0,
            },
            {
                "stage": "solution_selection",
                "response": _selection(
                    "option:a", rationale="First same-option response to bound findings."
                ),
                "session": _SELECTOR_SESSION,
                "resume": _SELECTOR_SESSION,
                "elapsed": 200.0,
            },
            {
                "stage": "solution_falsification",
                "response": _review("reject", critical_findings=findings),
                "session": _FALSIFIER_2_SESSION,
                "elapsed": 200.0,
            },
            {
                "stage": "solution_selection",
                "response": _selection(
                    "option:a", rationale="Second same-option response to bound findings."
                ),
                "session": _SELECTOR_SESSION,
                "resume": _SELECTOR_SESSION,
                "elapsed": 200.0,
            },
            {
                "stage": "solution_falsification",
                "response": _review("reject", critical_findings=findings),
                "session": _FALSIFIER_3_SESSION,
                "elapsed": 200.0,
            },
            {
                "stage": "solution_selection",
                "response": _selection(
                    "option:b", rationale="Switch mechanisms after two nonadvancing cycles."
                ),
                "session": _SELECTOR_SESSION,
                "resume": _SELECTOR_SESSION,
                "elapsed": 200.0,
            },
            {
                "stage": "solution_falsification",
                "response": _review("accept"),
                "session": _FALSIFIER_4_SESSION,
                "elapsed": 200.0,
            },
            {
                "stage": "selected_solution_labeler",
                "response": _label(),
                "session": _LABELER_SESSION,
            },
        ],
    )

    assert result["status"] == "selected"
    assert result["decision"]["selected_option_id"] == "option:b"
    assert result["outcome"]["consecutive_material_nonprogress"] == 0
    assert result["outcome"]["rework_cost_since_progress"] == 0.0
    assert result["outcome"]["total_rework_cost"] == 1200.0
    assert len(result["role_runs"]) == 9
    assert result["feedback"]


def test_option_revision_pauses_only_on_third_nonprogress_cycle_and_retains_best(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unchanged_options = [_option("option:a"), _option("option:b")]
    optioner_response = json.dumps(
        {
            "optioning_status": "options_produced",
            "decision_rationale": "The current mechanisms remain the evidenced choices.",
            "options": unchanged_options,
        }
    )
    initial_revision = _revision_request()
    result, _ = _run_case(
        tmp_path,
        monkeypatch,
        [
            {
                "stage": "solution_selection",
                "response": initial_revision,
                "session": _SELECTOR_SESSION,
                "elapsed": 1.0,
            },
            {
                "stage": "solution_optioning",
                "response": optioner_response,
                "session": _OPTIONER_SESSION,
                "resume": _OPTIONER_SESSION,
                "elapsed": 100.0,
            },
            {
                "stage": "solution_selection",
                "response": _revision_request(
                    rationale="Different wording, but the same option set is still inadequate."
                ),
                "session": _SELECTOR_SESSION,
                "resume": _SELECTOR_SESSION,
                "elapsed": 100.0,
            },
            {
                "stage": "solution_optioning",
                "response": optioner_response,
                "session": _OPTIONER_SESSION,
                "resume": _OPTIONER_SESSION,
                "elapsed": 100.0,
            },
            {
                "stage": "solution_selection",
                "response": _revision_request(
                    rationale="Second distinct wording; the option mechanism is still unchanged."
                ),
                "session": _SELECTOR_SESSION,
                "resume": _SELECTOR_SESSION,
                "elapsed": 100.0,
            },
            {
                "stage": "solution_optioning",
                "response": optioner_response,
                "session": _OPTIONER_SESSION,
                "resume": _OPTIONER_SESSION,
                "elapsed": 100.0,
            },
            {
                "stage": "solution_selection",
                "response": _revision_request(
                    rationale="Third distinct wording; the option mechanism is still unchanged."
                ),
                "session": _SELECTOR_SESSION,
                "resume": _SELECTOR_SESSION,
                "elapsed": 100.0,
            },
        ],
        options=unchanged_options,
        stage4_doc=_stage4_doc(tmp_path),
    )

    assert result["status"] == (
        "repairable_paused:consecutive_nonadvancing_corrections_require_adjudication"
    )
    assert result["outcome"]["reasons"] == [
        "consecutive_nonadvancing_corrections_require_adjudication"
    ]
    assert result["outcome"]["consecutive_material_nonprogress"] == 3
    assert result["outcome"]["rework_cost_since_progress"] == 600.0
    assert result["outcome"]["total_rework_cost"] == 600.0
    assert len(result["role_runs"]) == 7
    assert result["retained_frontier"] == {
        "selector_response_sha256": sha256(initial_revision.encode("utf-8")).hexdigest(),
        "selected_option_id": None,
        "options_sha256": healing._canonical_sha256(unchanged_options),
        "falsifier_defect_count": None,
    }


def test_fewer_substantive_falsifier_defects_reset_rework_economics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_findings = [
        {"finding": "First root gap", "affects": "root_cause"},
        {"finding": "Second interface gap", "affects": "interface"},
    ]
    fewer_different_findings = [
        {"finding": "Differently named remaining gap", "affects": "change_surface"}
    ]
    result, _ = _run_case(
        tmp_path,
        monkeypatch,
        [
            {
                "stage": "solution_selection",
                "response": _selection("option:a"),
                "session": _SELECTOR_SESSION,
                "elapsed": 1.0,
            },
            {
                "stage": "solution_falsification",
                "response": _review("reject", critical_findings=first_findings),
                "session": _FALSIFIER_1_SESSION,
                "elapsed": 1.0,
            },
            {
                "stage": "solution_selection",
                "response": _selection(
                    "option:a", rationale="Substantive revision at the same option boundary."
                ),
                "session": _SELECTOR_SESSION,
                "resume": _SELECTOR_SESSION,
                "elapsed": 1.0,
            },
            {
                "stage": "solution_falsification",
                "response": _review("reject", critical_findings=fewer_different_findings),
                "session": _FALSIFIER_2_SESSION,
                "elapsed": 1.0,
            },
            {
                "stage": "solution_selection",
                "response": _selection("option:b"),
                "session": _SELECTOR_SESSION,
                "resume": _SELECTOR_SESSION,
            },
            {
                "stage": "solution_falsification",
                "response": _review("accept"),
                "session": _FALSIFIER_3_SESSION,
            },
            {
                "stage": "selected_solution_labeler",
                "response": _label(),
                "session": _LABELER_SESSION,
            },
        ],
    )

    assert result["status"] == "selected"
    assert result["decision"]["selected_option_id"] == "option:b"
    assert result["outcome"]["rework_cost_since_progress"] == 0.0


def test_dispositioned_risk_omission_is_not_substantive_progress() -> None:
    with_disclosed_accepted_risk = {
        "verdict": "reject",
        "strongest_counterargument": "A separate causal blocker remains.",
        "critical_findings": [],
        "unsupported_assumptions": ["Accepted compatibility risk"],
        "residual_risks": [],
        "material_risk_dispositions": [
            {
                "risk": "Accepted compatibility risk",
                "disposition": "accepted",
            }
        ],
    }
    omitted_disclosed_risk = {
        "verdict": "reject",
        "strongest_counterargument": "A differently worded causal blocker remains.",
        "critical_findings": [],
        "unsupported_assumptions": [],
        "residual_risks": [],
        "material_risk_dispositions": [],
    }

    assert healing._falsifier_substantive_defect_count(
        with_disclosed_accepted_risk
    ) == healing._falsifier_substantive_defect_count(omitted_disclosed_risk)


def test_role_conversation_uses_nonprogress_not_author_cost_as_pause_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _ScriptedTransport(
        tmp_path,
        [
            {
                "stage": "solution_selection",
                "response": json.dumps({"state": "a"}),
                "session": _UNKNOWN_SESSION,
                "resume": _UNKNOWN_SESSION,
                "elapsed": 1.0,
            },
            {
                "stage": "solution_selection",
                "response": json.dumps({"state": "b"}),
                "session": _UNKNOWN_SESSION,
                "resume": _UNKNOWN_SESSION,
                "elapsed": 1.0,
            },
            {
                "stage": "solution_selection",
                "response": json.dumps({"state": "c"}),
                "session": _UNKNOWN_SESSION,
                "resume": _UNKNOWN_SESSION,
                "elapsed": 1.0,
            },
            {
                "stage": "solution_selection",
                "response": json.dumps({"state": "d"}),
                "session": _UNKNOWN_SESSION,
                "resume": _UNKNOWN_SESSION,
                "elapsed": 1.0,
            },
        ],
    )
    monkeypatch.setattr(healing, "run_stage_prompt_json", transport)

    run = healing._run_role_conversation(
        role="selector",
        invocation_stage="solution_selection",
        initial_prompt="resume with missing historical cost",
        author_origin_prompt_sha256="d" * 64,
        out_dir=tmp_path,
        base_tag="resume_cost",
        workspace_dir=tmp_path,
        repo_revision="e" * 40,
        agent="codex",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
        validator=lambda response: (
            {"value": json.loads(response)},
            ["still_repairable_unknown_error"],
            [],
        ),
        initial_resume_session_id=_UNKNOWN_SESSION,
        author_cost_seconds=0.0,
    )

    assert run["status"] == (
        "repairable_paused:"
        "consecutive_nonadvancing_corrections_require_adjudication"
    )
    assert run["metrics"]["attempt_count"] == 4
    assert run["correction_cost_since_progress"] == 2.0
    assert run["total_correction_cost"] == 3.0


def test_unknown_role_error_is_repaired_without_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _ScriptedTransport(
        tmp_path,
        [
            {
                "stage": "solution_selection",
                "response": json.dumps({"ok": False}),
                "session": _UNKNOWN_SESSION,
            },
            {
                "stage": "solution_selection",
                "response": json.dumps({"ok": True}),
                "session": _UNKNOWN_SESSION,
                "resume": _UNKNOWN_SESSION,
            },
        ],
    )
    monkeypatch.setattr(healing, "run_stage_prompt_json", transport)

    def validator(response: str) -> tuple[dict[str, Any], list[str], list[str]]:
        raw = json.loads(response)
        return (
            {"value": raw},
            ([] if raw["ok"] else ["previously_unknown_validator_error"]),
            (["value:ok"] if raw["ok"] else []),
        )

    run = healing._run_role_conversation(
        role="selector",
        invocation_stage="solution_selection",
        initial_prompt="unknown error test",
        author_origin_prompt_sha256="a" * 64,
        out_dir=tmp_path,
        base_tag="selector_unknown",
        workspace_dir=tmp_path,
        repo_revision="b" * 40,
        agent="codex",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
        validator=validator,
    )

    assert run["status"] == "corrected"
    assert "previously_unknown_validator_error" in str(transport.calls[1]["prompt"])
