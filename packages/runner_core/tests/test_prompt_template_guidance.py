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
