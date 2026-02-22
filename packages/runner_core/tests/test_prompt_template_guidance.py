from __future__ import annotations

from pathlib import Path


def test_prompt_templates_discourage_heredocs_and_shell_output_for_reports() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    templates = [
        repo_root / "configs" / "prompt_templates" / "default_inline_report.prompt.md",
        repo_root / "configs" / "prompt_templates" / "inline_report_v1.prompt.md",
    ]

    for template_path in templates:
        text = template_path.read_text(encoding="utf-8")
        lowered = text.lower()
        assert "heredoc" in lowered, f"missing heredoc guidance in {template_path}"
        assert "<<eof" in lowered, f"missing <<EOF marker in {template_path}"
        assert "write_file" in text, f"missing write_file guidance in {template_path}"
        assert "replace" in text, f"missing replace guidance in {template_path}"
        assert "run_shell_command" in text, f"missing run_shell_command guidance in {template_path}"
