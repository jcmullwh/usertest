from __future__ import annotations

from pathlib import Path


def _prompt_texts() -> dict[str, str]:
    root = Path(__file__).resolve().parents[3]
    return {
        "guidance": (
            root / "configs" / "backlog_stage_guidance" / "repro_research.md"
        ).read_text(encoding="utf-8"),
        "mission": (
            root / "configs" / "missions" / "builtin" / "backlog_repro_research.mission.md"
        ).read_text(encoding="utf-8"),
    }


def test_live_research_prompts_teach_open_causal_proof_and_same_session_repair() -> None:
    prompts = _prompt_texts()
    combined = "\n".join(prompts.values())

    for required in (
        "environment.v1",
        "filesystem_state.v1",
        "config_repository_state.v1",
        "platform.v1",
        "structured_replay.v1",
        "origin_exact_value",
        "repository_fail_first_command",
        "authenticated_semantic_citation",
        "registered deterministic predicate",
        "implementation_touchpoints",
        "causal locator separate",
        "optioning must return the case to research",
        "same author session",
        "same workspace",
        "Confidence is telemetry",
        "Any language/runtime is valid",
        "Materiality is relative to the implementation decision",
        'reproduction_status="partial"',
        "incomplete replay coverage",
        "not in `blocking_reasons`",
        "future design parameter",
        "Stage-4/5 work",
        "do not demand post-change execution before optioning",
        "optional and non-authoritative",
        "Do not require or construct a future algorithm",
        "does not require a future solution contract",
        "future solution oracle is optional",
        "present mechanism or connected change surface",
    ):
        assert required.casefold() in combined.casefold()


def test_live_research_prompts_do_not_reintroduce_closed_benchmark_contracts() -> None:
    prompts = _prompt_texts()
    forbidden = (
        "confidence is at least `0.75`",
        "scenario_kind` is exactly",
        "platform_requirement` is exactly",
        "for a novel bug without either route, create a fail-first python harness",
        "environment, platform, filesystem-state, completion-marker, and other "
        "prose-described changes are observations",
        "do not claim those modes now",
        "two exact pytest selections",
        "python_call_chain.v1",
        "pytest_controlled_difference.v1",
    )

    for name, prompt in prompts.items():
        normalized = " ".join(prompt.casefold().split())
        assert [fragment for fragment in forbidden if fragment.casefold() in normalized] == [], name


def test_live_research_guidance_names_exact_proof_adapter_observation_sources() -> None:
    guidance = _prompt_texts()["guidance"]

    for source in (
        "exit_code",
        "stdout_text",
        "stderr_text",
        "combined_text",
        "stdout_json",
        "stderr_json",
        "event_lines",
        "event_json",
        "executed_argv",
        "platform",
    ):
        assert f"`{source}`" in guidance


def test_initial_research_guidance_discloses_nested_machine_contract() -> None:
    guidance = _prompt_texts()["guidance"]

    for required in (
        '"artifact_id"',
        '"experiment_id"',
        '"addresses_atom_ids"',
        '"observable_assertion"',
        "supports | refutes | inconclusive",
        "exit_code|stdout|stderr|combined",
        "equals|contains|not_contains",
        '"supporting_evidence"',
        '"disposition_evidence"',
        '"falsification_attempts"',
        "refuted|plausible|unresolved",
        "survived|disproved|inconclusive",
        '"kind":"equals"',
        '"kind":"event_sequence"',
        "observations={baseline:{source",
        "not the experiment assertion shape",
        'exact `scenario_kind="control"`',
        "touchpoint `causal_locator`",
        "use `symbols`, not `inspected_symbols`",
        "invented hypothesis-level",
        "never redirect temp/tmp",
        "after a pre-mechanism stall or failure",
        "rather than rerunning unchanged",
        "inconclusive command stopped by an external timeout/kill",
        "not a replay experiment",
        "self-contained faithful replay",
        "use `/` in persisted repo-relative paths",
        '"evidence_needed"',
        '"affects"',
        "evidence_boundaries` is a string list",
        "scenario/platform labels remain open",
        "unknown top-level fields fail validation",
        "directionally support the claim",
        "counterevidence as support",
    ):
        assert required.casefold() in guidance.casefold()


def test_research_prompt_payload_is_concise_enough_for_live_throughput() -> None:
    prompts = _prompt_texts()

    assert len(prompts["guidance"].splitlines()) < 260
    assert len(prompts["mission"].splitlines()) < 100
