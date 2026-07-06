from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "runner_core.artifacts",
        "runner_core.git_helpers",
        "runner_core.preflight",
        "runner_core.prompt_staging",
        "runner_core.python_capability",
        "runner_core.shell_capability",
        "runner_core.stderr_diagnostics",
        "runner_core.verification_prompts",
    ],
)
def test_planned_runner_core_module_boundaries_import(module_name: str) -> None:
    assert importlib.import_module(module_name).__name__ == module_name


@pytest.mark.parametrize(
    ("module_name", "helper_name"),
    [
        ("runner_core.artifacts", "_extract_json_object"),
        ("runner_core.artifacts", "_read_tail_text"),
        ("runner_core.artifacts", "_tail_text_for_prompt"),
        ("runner_core.artifacts", "_write_json"),
        ("runner_core.git_helpers", "_git_diff"),
        ("runner_core.git_helpers", "_git_numstat"),
        ("runner_core.git_helpers", "_git_status_porcelain"),
        ("runner_core.git_helpers", "_ensure_git_user_config"),
        ("runner_core.prompt_staging", "_agent_path_for_staged_file"),
        ("runner_core.prompt_staging", "_resolve_agent_prompt_input_path"),
        ("runner_core.prompt_staging", "_stage_agent_prompt_file"),
        ("runner_core.prompt_staging", "_stage_agent_prompt_text"),
        ("runner_core.stderr_diagnostics", "_sanitize_agent_stderr_file"),
        ("runner_core.stderr_diagnostics", "_extract_claude_quota_exhaustion"),
        ("runner_core.stderr_diagnostics", "_classify_failure_subtype"),
        ("runner_core.verification_prompts", "_build_followup_prompt"),
        ("runner_core.verification_prompts", "_build_verification_followup_prompt"),
    ],
)
def test_extracted_runner_helpers_are_importable_from_focused_modules(
    module_name: str,
    helper_name: str,
) -> None:
    module = importlib.import_module(module_name)

    assert callable(getattr(module, helper_name))
