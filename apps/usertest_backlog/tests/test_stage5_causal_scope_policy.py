from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

POLICY_PATHS = (
    "configs/backlog_prompts/solution_selector.md",
    "configs/backlog_prompts_internal_maintenance/solution_selector.md",
    "configs/backlog_prompts/solution_falsifier.md",
    "configs/backlog_prompts/solution_optioner.md",
    "configs/backlog_prompts_internal_maintenance/solution_optioner.md",
    "configs/backlog_stage_guidance/solution_selection.md",
    "configs/backlog_stage_guidance_internal_maintenance/solution_selection.md",
    "configs/backlog_stage_guidance/solution_optioning.md",
    "configs/backlog_stage_guidance_internal_maintenance/solution_optioning.md",
)


def _normalized(relative_path: str) -> str:
    return " ".join(
        (REPO_ROOT / relative_path).read_text(encoding="utf-8").split()
    ).casefold()


def test_breadth_gate_does_not_confuse_one_path_with_its_change_surface() -> None:
    for path in POLICY_PATHS:
        text = _normalized(path)

        assert "broad" in text
        assert "reusable abstraction" in text
        assert "existing" in text and "helper" in text
        assert "single-path" in text or "`single_path`" in text
        assert "caller" in text
        assert "compatibility" in text


def test_stage5_requires_intervention_before_the_first_resource_failure() -> None:
    for path in POLICY_PATHS:
        text = _normalized(path)

        assert "same-resource failure boundary" in text
        assert "after" in text
        assert "cannot recover" in text


def test_partial_support_failure_policy_preserves_safe_good_throughput() -> None:
    for path in POLICY_PATHS:
        text = _normalized(path)

        assert "partial" in text and "error" in text
        assert "safe postcondition" in text
        assert "actual progress" in text
        assert "abort" in text
        assert "swallow" in text


def test_benchmark_value_cannot_silently_become_a_universal_maximum() -> None:
    for path in POLICY_PATHS:
        text = _normalized(path)

        assert "benchmark" in text
        assert "universal supported maximum" in text
        assert "repository requirement" in text
