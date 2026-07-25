from __future__ import annotations

from pathlib import Path

from runner_core.prompt import CANONICAL_EXECUTION_NOTES_MD


def test_prompt_templates_discourage_heredocs_and_shell_output_for_reports() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    templates_dir = repo_root / "configs" / "prompt_templates"
    templates = sorted(templates_dir.rglob("*.prompt.md"))
    assert templates, f"no .prompt.md templates found in {templates_dir}"

    # Verify canonical guidance contains all expected safety/portability instructions
    text = CANONICAL_EXECUTION_NOTES_MD
    lowered = text.lower()
    assert "heredoc" in lowered, "missing heredoc guidance in CANONICAL_EXECUTION_NOTES_MD"
    assert "<<eof" in lowered, "missing <<EOF marker in CANONICAL_EXECUTION_NOTES_MD"
    assert "write_file" in text, "missing write_file guidance in CANONICAL_EXECUTION_NOTES_MD"
    assert "replace" in text, "missing replace guidance in CANONICAL_EXECUTION_NOTES_MD"
    assert (
        "run_shell_command" in text
    ), "missing run_shell_command guidance in CANONICAL_EXECUTION_NOTES_MD"
    assert (
        "powershell" in lowered
    ), "missing powershell guidance in CANONICAL_EXECUTION_NOTES_MD"
    assert "ripgrep" in lowered, "missing ripgrep guidance in CANONICAL_EXECUTION_NOTES_MD"

    # Verify every template uses the canonical placeholder
    for template_path in templates:
        template_text = template_path.read_text(encoding="utf-8")
        assert (
            "${execution_notes_md}" in template_text
        ), f"missing ${{execution_notes_md}} placeholder in {template_path}"


def test_implementation_and_review_missions_guide_appropriate_delegation() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    mission_paths = [
        repo_root / "configs" / "missions" / "builtin" / "implement_backlog_ticket_v1.mission.md",
        repo_root
        / "configs"
        / "missions"
        / "builtin"
        / "implement_maintenance_backlog_ticket_v1.mission.md",
        repo_root
        / "configs"
        / "missions"
        / "builtin"
        / "review_backlog_implementation_pr_v1.mission.md",
    ]

    required_phrases = (
        "## Delegation guidance",
        "Use delegation only when it helps",
        "broad read-only exploration of large files or cross-module contracts",
        "test failure triage and log summarization",
        "independent review of implementation risks",
        "narrow investigation of one module or workflow",
        "Do not delegate small, obvious",
        "require a concise summary back to the parent",
        "keep raw broad-source",
        "out of the parent context",
        "Delegation is not a scope gate",
        "rather than under-scoping",
    )

    for mission_path in mission_paths:
        text = mission_path.read_text(encoding="utf-8")
        normalized_text = " ".join(text.split())
        for phrase in required_phrases:
            normalized_phrase = " ".join(phrase.split())
            assert (
                normalized_phrase in normalized_text
            ), f"missing delegation guidance phrase {phrase!r} in {mission_path}"
