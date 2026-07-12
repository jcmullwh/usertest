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
        "semantic_review_required",
        "registered deterministic predicate",
        "implementation_touchpoints",
        "causal locator separate",
        "optioning must return the case to research",
        "same author session",
        "same workspace",
        "Confidence is telemetry",
        "Any language/runtime is valid",
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
    )

    for name, prompt in prompts.items():
        normalized = " ".join(prompt.casefold().split())
        assert [fragment for fragment in forbidden if fragment.casefold() in normalized] == [], name


def test_research_prompt_payload_is_concise_enough_for_live_throughput() -> None:
    prompts = _prompt_texts()

    assert len(prompts["guidance"].splitlines()) < 220
    assert len(prompts["mission"].splitlines()) < 100
