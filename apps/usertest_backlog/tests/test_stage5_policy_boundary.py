from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _normalized(relative_path: str) -> str:
    return " ".join(
        (REPO_ROOT / relative_path).read_text(encoding="utf-8").split()
    ).casefold()


def test_stage5_does_not_treat_explicit_prospective_policy_as_missing_research() -> None:
    prompt = _normalized("configs/backlog_prompts/solution_falsifier.md")
    general = _normalized("configs/backlog_stage_guidance/solution_selection.md")
    maintenance = _normalized(
        "configs/backlog_stage_guidance_internal_maintenance/solution_selection.md"
    )

    for text in (prompt, general, maintenance):
        assert "present-state fact" in text
        assert "prospective" in text
        assert "finite threshold" in text
        assert "configurable" in text
        assert "identity/alias" in text or "identity or alias" in text
        assert "stage 3" in text

    assert "novelty alone does not make" in prompt
    assert "did not already encode it" in general
    assert "without proof that the old implementation already intended it" in maintenance


def test_stage5_keeps_concrete_maintenance_gaps_blocking() -> None:
    prompt = _normalized("configs/backlog_prompts/solution_falsifier.md")
    maintenance = _normalized(
        "configs/backlog_stage_guidance_internal_maintenance/solution_selection.md"
    )

    for text in (prompt, maintenance):
        assert "pre-build recovery" in text
        assert "manual/shared consumer" in text or "manual/shared" in text
        assert "active" in text
        assert "protected" in text
        assert "external" in text
        assert "verification" in text
        assert "indispensable" in text
        assert "block" in text


def test_stage4_owns_policy_choice_but_not_unknown_present_state() -> None:
    paths = (
        "configs/backlog_prompts/solution_optioner.md",
        "configs/backlog_prompts_internal_maintenance/solution_optioner.md",
        "configs/backlog_stage_guidance/solution_optioning.md",
        "configs/backlog_stage_guidance_internal_maintenance/solution_optioning.md",
    )

    for path in paths:
        text = _normalized(path)
        assert "prospective" in text
        assert "finite threshold" in text
        assert "configurable" in text
        assert "identity/alias" in text
        assert "indispensable" in text
        assert "protection signal" in text
        assert "verification" in text
