"""Tests for tools/lint_prompts.py — all lint functions.

Key invariants:
- Missing prompt files return specific error messages, not silent success.
- Valid prompts pass without errors.
- Invalid prompts return descriptive error messages.
- Banned terms in {{...}} injection blocks do not trigger errors.
- Pipeline manifest version check rejects wrong versions.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------


def _load_module() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "lint_prompts.py"
    spec = importlib.util.spec_from_file_location("lint_prompts", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# lint_labeler_prompt_enums
# ---------------------------------------------------------------------------


def test_lint_labeler_prompt_enums_raises_on_missing_file(tmp_path: Path) -> None:
    mod = _load_module()
    errors = mod.lint_labeler_prompt_enums(repo_root=tmp_path)
    assert any("missing_prompt" in e for e in errors)


def test_lint_labeler_prompt_enums_passes_valid_prompt(tmp_path: Path) -> None:
    mod = _load_module()
    surface_kinds = [
        "new_command",
        "new_flag",
        "docs_change",
        "behavior_change",
        "breaking_change",
        "new_top_level_mode",
        "new_config_schema",
        "new_api",
        "unknown",
    ]
    lines = ["# Labeler", ""] + [f"- {k}" for k in surface_kinds] + ["", "Output MUST be a single valid JSON object", ""]
    _write_text(tmp_path / "configs" / "backlog_prompts" / "labeler.md", "\n".join(lines))
    errors = mod.lint_labeler_prompt_enums(repo_root=tmp_path)
    assert errors == []


def test_lint_labeler_prompt_enums_reports_missing_kinds(tmp_path: Path) -> None:
    mod = _load_module()
    # Only include a few enum values, missing most
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "labeler.md",
        "# Labeler\n- new_command\n- docs_change\nOutput MUST be a single valid JSON object\n",
    )
    errors = mod.lint_labeler_prompt_enums(repo_root=tmp_path)
    assert any("missing_kinds" in e for e in errors)
    # Verify at least one missing kind is named
    assert any("new_flag" in e for e in errors)


def test_lint_labeler_prompt_enums_reports_missing_json_rule(tmp_path: Path) -> None:
    mod = _load_module()
    surface_kinds = [
        "new_command", "new_flag", "docs_change", "behavior_change", "breaking_change",
        "new_top_level_mode", "new_config_schema", "new_api", "unknown",
    ]
    lines = ["# Labeler", ""] + [f"- {k}" for k in surface_kinds]
    # No JSON-only rule at all
    _write_text(tmp_path / "configs" / "backlog_prompts" / "labeler.md", "\n".join(lines))
    errors = mod.lint_labeler_prompt_enums(repo_root=tmp_path)
    assert any("missing_json_only_rule" in e for e in errors)


# ---------------------------------------------------------------------------
# lint_miner_prompt_mentions_labeler
# ---------------------------------------------------------------------------


def test_lint_miner_prompt_mentions_labeler_raises_on_missing_file(tmp_path: Path) -> None:
    mod = _load_module()
    errors = mod.lint_miner_prompt_mentions_labeler(repo_root=tmp_path)
    assert any("missing_prompt" in e for e in errors)


def test_lint_miner_prompt_mentions_labeler_passes_valid_prompt(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "miner_default.md",
        "# Miner\nConsult the labeler stage for change_surface classification.\n",
    )
    errors = mod.lint_miner_prompt_mentions_labeler(repo_root=tmp_path)
    assert errors == []


def test_lint_miner_prompt_mentions_labeler_reports_missing_change_surface(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "miner_default.md",
        "# Miner\nConsult the labeler for more info.\n",
    )
    errors = mod.lint_miner_prompt_mentions_labeler(repo_root=tmp_path)
    assert any("change_surface" in e for e in errors)


def test_lint_miner_prompt_mentions_labeler_reports_missing_labeler(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "miner_default.md",
        "# Miner\nTag each item with a change_surface value.\n",
    )
    errors = mod.lint_miner_prompt_mentions_labeler(repo_root=tmp_path)
    assert any("labeler" in e for e in errors)


# ---------------------------------------------------------------------------
# lint_stage_prompts_json_only
# ---------------------------------------------------------------------------


def test_lint_stage_prompts_json_only_passes_on_empty_dir(tmp_path: Path) -> None:
    mod = _load_module()
    (tmp_path / "configs" / "backlog_prompts").mkdir(parents=True)
    errors = mod.lint_stage_prompts_json_only(repo_root=tmp_path)
    assert errors == []


def test_lint_stage_prompts_json_only_passes_with_return_only_json(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "problem_miner_default.md",
        "# Miner\nReturn ONLY JSON:\n[]\n",
    )
    errors = mod.lint_stage_prompts_json_only(repo_root=tmp_path)
    assert errors == []


def test_lint_stage_prompts_json_only_fails_missing_json_rule(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "problem_miner_default.md",
        "# Miner\nIdentify problems in the evidence.\n",
    )
    errors = mod.lint_stage_prompts_json_only(repo_root=tmp_path)
    assert any("missing_json_only_rule" in e for e in errors)


# ---------------------------------------------------------------------------
# lint_problem_miner_prompts_no_solution_fields
# ---------------------------------------------------------------------------


def test_lint_problem_miner_no_solution_fields_passes_on_empty_dir(tmp_path: Path) -> None:
    mod = _load_module()
    (tmp_path / "configs" / "backlog_prompts").mkdir(parents=True)
    errors = mod.lint_problem_miner_prompts_no_solution_fields(repo_root=tmp_path)
    assert errors == []


def test_lint_problem_miner_no_solution_fields_passes_clean_prompt(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "problem_miner_default.md",
        "# Miner\nIdentify problems.\nReturn ONLY JSON:\n[]\n",
    )
    errors = mod.lint_problem_miner_prompts_no_solution_fields(repo_root=tmp_path)
    assert errors == []


def test_lint_problem_miner_no_solution_fields_rejects_proposed_fix(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "problem_miner_default.md",
        '# Miner\nInclude "proposed_fix" in your output.\nReturn ONLY JSON:\n[]\n',
    )
    errors = mod.lint_problem_miner_prompts_no_solution_fields(repo_root=tmp_path)
    assert any("proposed_fix" in e for e in errors)
    assert any("forbidden_field" in e for e in errors)


def test_lint_problem_miner_no_solution_fields_rejects_multiple_forbidden(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "problem_miner_harness.md",
        '# Harness\nAdd "family_id" and "option_id".\nReturn ONLY JSON:\n[]\n',
    )
    errors = mod.lint_problem_miner_prompts_no_solution_fields(repo_root=tmp_path)
    assert any("family_id" in e for e in errors)
    assert any("option_id" in e for e in errors)


# ---------------------------------------------------------------------------
# lint_repro_research_prompt_anti_implementation
# ---------------------------------------------------------------------------


def test_lint_repro_research_skips_missing_file(tmp_path: Path) -> None:
    mod = _load_module()
    errors = mod.lint_repro_research_prompt_anti_implementation(repo_root=tmp_path)
    assert errors == []


def test_lint_repro_research_passes_valid_prompt(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "repro_researcher.md",
        (
            "# Repro Researcher\n"
            "Your goal is reproduction of the issue.\n"
            "This is not for implementation.\n"
            "Set implementation_performed to false.\n"
        ),
    )
    errors = mod.lint_repro_research_prompt_anti_implementation(repo_root=tmp_path)
    assert errors == []


def test_lint_repro_research_fails_missing_reproduction_goal(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "repro_researcher.md",
        "# Researcher\nThis is not for implementation.\nSet implementation_performed to false.\n",
    )
    errors = mod.lint_repro_research_prompt_anti_implementation(repo_root=tmp_path)
    assert any("reproduction_goal" in e for e in errors)


def test_lint_repro_research_fails_missing_implementation_performed(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "repro_researcher.md",
        "# Researcher\nReproduce the issue. This is not for implementation.\n",
    )
    errors = mod.lint_repro_research_prompt_anti_implementation(repo_root=tmp_path)
    assert any("implementation_performed" in e for e in errors)


def test_lint_repro_research_fails_missing_anti_impl_guardrail(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "repro_researcher.md",
        "# Researcher\nReproduce the issue.\nSet implementation_performed to false.\n",
    )
    errors = mod.lint_repro_research_prompt_anti_implementation(repo_root=tmp_path)
    assert any("anti_implementation" in e for e in errors)


# ---------------------------------------------------------------------------
# lint_solution_selector_prompt_no_option_invention
# ---------------------------------------------------------------------------


def test_lint_solution_selector_skips_missing_file(tmp_path: Path) -> None:
    mod = _load_module()
    errors = mod.lint_solution_selector_prompt_no_option_invention(repo_root=tmp_path)
    assert errors == []


def test_lint_solution_selector_passes_with_choose_from(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "solution_selector.md",
        "# Selector\nChoose from the provided options.\nReturn ONLY JSON:\n[]\n",
    )
    errors = mod.lint_solution_selector_prompt_no_option_invention(repo_root=tmp_path)
    assert errors == []


def test_lint_solution_selector_passes_with_do_not_invent(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "solution_selector.md",
        "# Selector\nDo not invent new options.\nReturn ONLY JSON:\n[]\n",
    )
    errors = mod.lint_solution_selector_prompt_no_option_invention(repo_root=tmp_path)
    assert errors == []


def test_lint_solution_selector_fails_missing_constraint(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "solution_selector.md",
        "# Selector\nPick the best solution.\nReturn ONLY JSON:\n[]\n",
    )
    errors = mod.lint_solution_selector_prompt_no_option_invention(repo_root=tmp_path)
    assert any("choose_from_constraint" in e for e in errors)


# ---------------------------------------------------------------------------
# lint_stage_prompts_no_banned_optimization_terms
# ---------------------------------------------------------------------------


def test_lint_banned_terms_passes_on_empty_dir(tmp_path: Path) -> None:
    mod = _load_module()
    (tmp_path / "configs" / "backlog_prompts").mkdir(parents=True)
    errors = mod.lint_stage_prompts_no_banned_optimization_terms(repo_root=tmp_path)
    assert errors == []


def test_lint_banned_terms_passes_clean_prompt(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "problem_miner_default.md",
        "# Miner\nIdentify concrete problems.\nReturn ONLY JSON:\n[]\n",
    )
    errors = mod.lint_stage_prompts_no_banned_optimization_terms(repo_root=tmp_path)
    assert errors == []


def test_lint_banned_terms_rejects_hardcoded_banned_term(tmp_path: Path) -> None:
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "problem_miner_default.md",
        "# Miner\nChoose the simplest solution.\nReturn ONLY JSON:\n[]\n",
    )
    errors = mod.lint_stage_prompts_no_banned_optimization_terms(repo_root=tmp_path)
    assert any("banned_optimization_term" in e for e in errors)
    assert any("simplest" in e for e in errors)


def test_lint_banned_terms_allows_banned_term_inside_injection_block(tmp_path: Path) -> None:
    """A banned term inside {{...}} is taxonomy-injected text and must not trigger an error."""
    mod = _load_module()
    _write_text(
        tmp_path / "configs" / "backlog_prompts" / "problem_miner_default.md",
        "# Miner\nHere is the taxonomy: {{TAXONOMY_TEXT_simplest_fastest_easiest}}\nReturn ONLY JSON:\n[]\n",
    )
    errors = mod.lint_stage_prompts_no_banned_optimization_terms(repo_root=tmp_path)
    assert errors == []


def test_lint_banned_terms_catches_all_banned_terms(tmp_path: Path) -> None:
    """Each banned term individually should trigger an error when hard-coded."""
    mod = _load_module()
    banned = ["fastest", "quickest", "easiest", "simplest", "lowest-effort"]
    for term in banned:
        _write_text(
            tmp_path / "configs" / "backlog_prompts" / "problem_miner_default.md",
            f"# Miner\nUse the {term} approach.\nReturn ONLY JSON:\n[]\n",
        )
        errors = mod.lint_stage_prompts_no_banned_optimization_terms(repo_root=tmp_path)
        assert any(term in e for e in errors), f"Expected error for banned term '{term}'"


# ---------------------------------------------------------------------------
# lint_pipeline_manifest_references_exist
# ---------------------------------------------------------------------------


def test_lint_pipeline_manifest_skips_missing_file(tmp_path: Path) -> None:
    mod = _load_module()
    errors = mod.lint_pipeline_manifest_references_exist(repo_root=tmp_path)
    assert errors == []


def test_lint_pipeline_manifest_reports_invalid_json(tmp_path: Path) -> None:
    mod = _load_module()
    manifest_path = tmp_path / "configs" / "backlog_prompts" / "pipeline_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text("not valid json", encoding="utf-8")
    errors = mod.lint_pipeline_manifest_references_exist(repo_root=tmp_path)
    assert any("invalid_json" in e for e in errors)


def test_lint_pipeline_manifest_reports_wrong_version(tmp_path: Path) -> None:
    mod = _load_module()
    _write_json(
        tmp_path / "configs" / "backlog_prompts" / "pipeline_manifest.json",
        {"version": 1, "problem_miner_templates": ["x.md"]},
    )
    errors = mod.lint_pipeline_manifest_references_exist(repo_root=tmp_path)
    assert any("wrong_version" in e for e in errors)


def test_lint_pipeline_manifest_reports_missing_taxonomy(tmp_path: Path) -> None:
    mod = _load_module()
    _write_json(
        tmp_path / "configs" / "backlog_prompts" / "pipeline_manifest.json",
        {"version": 2},
    )
    # relation_review and stage_guidance exist but taxonomy does not
    (tmp_path / "configs" / "backlog_relation_review.yaml").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "configs" / "backlog_relation_review.yaml").write_text("version: 1\n", encoding="utf-8")
    (tmp_path / "configs" / "backlog_stage_guidance").mkdir(parents=True, exist_ok=True)
    errors = mod.lint_pipeline_manifest_references_exist(repo_root=tmp_path)
    assert any("missing_taxonomy" in e for e in errors)


def test_lint_pipeline_manifest_reports_missing_relation_review_config(tmp_path: Path) -> None:
    mod = _load_module()
    _write_json(
        tmp_path / "configs" / "backlog_prompts" / "pipeline_manifest.json",
        {"version": 2},
    )
    # taxonomy and stage_guidance exist but relation_review does not
    _write_json(tmp_path / "configs" / "backlog_taxonomy.json", {"version": 1})
    (tmp_path / "configs" / "backlog_stage_guidance").mkdir(parents=True, exist_ok=True)
    errors = mod.lint_pipeline_manifest_references_exist(repo_root=tmp_path)
    assert any("missing_relation_review_config" in e for e in errors)


def test_lint_pipeline_manifest_reports_missing_stage_guidance_dir(tmp_path: Path) -> None:
    mod = _load_module()
    _write_json(
        tmp_path / "configs" / "backlog_prompts" / "pipeline_manifest.json",
        {"version": 2},
    )
    # taxonomy and relation_review exist but stage_guidance dir does not
    _write_json(tmp_path / "configs" / "backlog_taxonomy.json", {"version": 1})
    (tmp_path / "configs" / "backlog_relation_review.yaml").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "configs" / "backlog_relation_review.yaml").write_text("version: 1\n", encoding="utf-8")
    errors = mod.lint_pipeline_manifest_references_exist(repo_root=tmp_path)
    assert any("missing_stage_guidance_dir" in e for e in errors)


def test_lint_pipeline_manifest_reports_missing_stage_template_file(tmp_path: Path) -> None:
    mod = _load_module()
    _write_json(
        tmp_path / "configs" / "backlog_prompts" / "pipeline_manifest.json",
        {
            "version": 2,
            "stage_templates": {"problem_mining": "nonexistent_template.md"},
        },
    )
    # Create the required config files so only the template check fires
    _write_json(tmp_path / "configs" / "backlog_taxonomy.json", {"version": 1})
    (tmp_path / "configs" / "backlog_relation_review.yaml").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "configs" / "backlog_relation_review.yaml").write_text("version: 1\n", encoding="utf-8")
    (tmp_path / "configs" / "backlog_stage_guidance").mkdir(parents=True, exist_ok=True)
    errors = mod.lint_pipeline_manifest_references_exist(repo_root=tmp_path)
    assert any("missing_stage_template" in e for e in errors)
    assert any("nonexistent_template.md" in e for e in errors)


def test_lint_pipeline_manifest_passes_fully_valid_manifest(tmp_path: Path) -> None:
    mod = _load_module()
    prompts_dir = tmp_path / "configs" / "backlog_prompts"
    prompts_dir.mkdir(parents=True)
    # Create a template file referenced by stage_templates
    _write_text(prompts_dir / "my_template.md", "# Template\nReturn ONLY JSON:\n[]\n")
    _write_json(
        prompts_dir / "pipeline_manifest.json",
        {
            "version": 2,
            "stage_templates": {"problem_mining": "my_template.md"},
        },
    )
    _write_json(tmp_path / "configs" / "backlog_taxonomy.json", {"version": 1})
    (tmp_path / "configs" / "backlog_relation_review.yaml").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "configs" / "backlog_relation_review.yaml").write_text("version: 1\n", encoding="utf-8")
    (tmp_path / "configs" / "backlog_stage_guidance").mkdir(parents=True, exist_ok=True)
    errors = mod.lint_pipeline_manifest_references_exist(repo_root=tmp_path)
    assert errors == []


def test_lint_pipeline_manifest_handles_list_stage_templates(tmp_path: Path) -> None:
    """stage_templates values that are lists should check each file individually."""
    mod = _load_module()
    prompts_dir = tmp_path / "configs" / "backlog_prompts"
    prompts_dir.mkdir(parents=True)
    _write_text(prompts_dir / "template_a.md", "# A\nReturn ONLY JSON:\n[]\n")
    # template_b.md does NOT exist — should trigger an error
    _write_json(
        prompts_dir / "pipeline_manifest.json",
        {
            "version": 2,
            "stage_templates": {"problem_mining": ["template_a.md", "template_b.md"]},
        },
    )
    _write_json(tmp_path / "configs" / "backlog_taxonomy.json", {"version": 1})
    (tmp_path / "configs" / "backlog_relation_review.yaml").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "configs" / "backlog_relation_review.yaml").write_text("version: 1\n", encoding="utf-8")
    (tmp_path / "configs" / "backlog_stage_guidance").mkdir(parents=True, exist_ok=True)
    errors = mod.lint_pipeline_manifest_references_exist(repo_root=tmp_path)
    assert any("template_b.md" in e for e in errors)
    assert not any("template_a.md" in e for e in errors)
