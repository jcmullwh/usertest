"""Tests for backlog_miner.pipeline prompt manifest loading.

Key invariants:
- Missing pipeline_manifest.json raises FileNotFoundError loudly.
- Wrong version raises ValueError.
- Missing referenced template files raise FileNotFoundError.
- A valid manifest loads successfully with correct attributes.
- Missing taxonomy or relation-review config files raise FileNotFoundError.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backlog_miner.pipeline import (
    PipelinePromptManifest,
    load_pipeline_prompt_manifest,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_valid_prompts_dir(base: Path) -> Path:
    """Set up a minimal valid prompts directory with all required files."""
    prompts_dir = base / "backlog_prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    # Create the four problem-miner templates.
    for name in [
        "problem_miner_default.md",
        "problem_miner_onboarding.md",
        "problem_miner_harness.md",
        "problem_miner_schema.md",
    ]:
        _write_text(prompts_dir / name, f"# {name}\nReturn ONLY JSON:\n[]\n")

    # Create the taxonomy file (repo-root relative).
    taxonomy_path = base.parent / "configs" / "backlog_taxonomy.json"
    _write_json(
        taxonomy_path,
        {
            "version": 1,
            "solution_families": [
                {"family_id": "most_direct", "label": "Most direct", "description": "..."},
                {"family_id": "most_robust", "label": "Most robust", "description": "..."},
                {
                    "family_id": "most_comprehensive",
                    "label": "Most comprehensive",
                    "description": "...",
                },
            ],
        },
    )

    # Create the relation-review config.
    rel_config_path = base.parent / "configs" / "backlog_relation_review.yaml"
    _write_text(rel_config_path, "version: 1\ndefaults:\n  top_k_by_semantic: 3\n")

    # Create the stage-guidance manifest.
    sg_manifest_path = (
        base.parent / "configs" / "backlog_stage_guidance" / "manifest.json"
    )
    _write_json(
        sg_manifest_path,
        {
            "version": 1,
            "stages": {
                "problem_mining": "problem_mining.md",
                "problem_prioritization": "problem_prioritization.md",
                "repro_research": "repro_research.md",
                "solution_optioning": "solution_optioning.md",
                "solution_selection": "solution_selection.md",
                "implementation_planning": "implementation_planning.md",
            },
        },
    )
    for stage_file in [
        "problem_mining.md",
        "problem_prioritization.md",
        "repro_research.md",
        "solution_optioning.md",
        "solution_selection.md",
        "implementation_planning.md",
    ]:
        _write_text(sg_manifest_path.parent / stage_file, f"# {stage_file}\n")

    # Write the manifest.
    _write_json(
        prompts_dir / "pipeline_manifest.json",
        {
            "version": 2,
            "problem_miner_templates": [
                "problem_miner_default.md",
                "problem_miner_onboarding.md",
                "problem_miner_harness.md",
                "problem_miner_schema.md",
            ],
        },
    )

    return prompts_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_load_pipeline_manifest_raises_on_missing_manifest(tmp_path: Path) -> None:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    with pytest.raises(FileNotFoundError, match="pipeline_manifest"):
        load_pipeline_prompt_manifest(prompts_dir)


def test_load_pipeline_manifest_raises_on_wrong_version(tmp_path: Path) -> None:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    _write_json(
        prompts_dir / "pipeline_manifest.json",
        {"version": 1, "problem_miner_templates": ["x.md"]},
    )
    with pytest.raises(ValueError, match="version"):
        load_pipeline_prompt_manifest(prompts_dir)


def test_load_pipeline_manifest_raises_on_missing_template(tmp_path: Path) -> None:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    _write_json(
        prompts_dir / "pipeline_manifest.json",
        {
            "version": 2,
            "problem_miner_templates": ["missing_template.md"],
        },
    )
    with pytest.raises(FileNotFoundError, match="missing_template"):
        load_pipeline_prompt_manifest(prompts_dir)


def test_load_pipeline_manifest_raises_on_empty_miner_templates(tmp_path: Path) -> None:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    _write_json(
        prompts_dir / "pipeline_manifest.json",
        {"version": 2, "problem_miner_templates": []},
    )
    with pytest.raises(ValueError, match="empty"):
        load_pipeline_prompt_manifest(prompts_dir)


def test_load_pipeline_manifest_raises_on_missing_taxonomy(tmp_path: Path) -> None:
    """When taxonomy file is missing, FileNotFoundError must be raised."""
    # Set up a minimal directory structure under tmp_path/configs to simulate repo root.
    repo_root = tmp_path
    prompts_dir = repo_root / "configs" / "backlog_prompts"
    prompts_dir.mkdir(parents=True)

    _write_text(prompts_dir / "problem_miner_default.md", "# test\nReturn ONLY JSON:\n[]\n")
    _write_json(
        prompts_dir / "pipeline_manifest.json",
        {
            "version": 2,
            "problem_miner_templates": ["problem_miner_default.md"],
            "taxonomy_file": "configs/backlog_taxonomy.json",
        },
    )
    # Stage guidance manifest must exist (otherwise hits that error first).
    sg_manifest = repo_root / "configs" / "backlog_stage_guidance" / "manifest.json"
    _write_json(sg_manifest, {"version": 1, "stages": {}})
    # Relation review config must exist.
    rel_config = repo_root / "configs" / "backlog_relation_review.yaml"
    _write_text(rel_config, "version: 1\n")
    # taxonomy_file does NOT exist.

    with pytest.raises(FileNotFoundError, match="taxonomy"):
        load_pipeline_prompt_manifest(prompts_dir)


def test_load_pipeline_manifest_valid_structure(tmp_path: Path) -> None:
    """A fully valid manifest loads and exposes the expected attributes."""
    # Simulate repo root under tmp_path/repo.
    repo_root = tmp_path / "repo"
    prompts_dir = _make_valid_prompts_dir(repo_root / "configs")
    # Adjust: _make_valid_prompts_dir writes configs relative to base.parent,
    # so prompts_dir.parent.parent is repo_root.

    manifest = load_pipeline_prompt_manifest(prompts_dir)

    assert isinstance(manifest, PipelinePromptManifest)
    assert len(manifest.problem_miner_templates) == 4
    assert all(p.exists() for p in manifest.problem_miner_templates)
    assert manifest.taxonomy_path.exists()
    assert manifest.relation_review_config_path.exists()
    assert manifest.stage_guidance_manifest_path.exists()


def test_load_pipeline_manifest_template_text_method(tmp_path: Path) -> None:
    """template_text() returns file contents; raises ValueError for None."""
    repo_root = tmp_path / "repo"
    prompts_dir = _make_valid_prompts_dir(repo_root / "configs")
    manifest = load_pipeline_prompt_manifest(prompts_dir)

    text = manifest.template_text(manifest.problem_miner_templates[0])
    assert "Return ONLY JSON" in text

    with pytest.raises(ValueError, match="None"):
        manifest.template_text(None)


def test_load_pipeline_manifest_load_stage_guidance(tmp_path: Path) -> None:
    """load_stage_guidance() returns the content of the stage guidance file."""
    repo_root = tmp_path / "repo"
    prompts_dir = _make_valid_prompts_dir(repo_root / "configs")
    manifest = load_pipeline_prompt_manifest(prompts_dir)

    text = manifest.load_stage_guidance("problem_mining")
    assert isinstance(text, str)
    assert len(text) > 0


def test_load_pipeline_manifest_load_stage_guidance_raises_on_unknown_stage(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    prompts_dir = _make_valid_prompts_dir(repo_root / "configs")
    manifest = load_pipeline_prompt_manifest(prompts_dir)

    with pytest.raises(KeyError, match="nonexistent_stage"):
        manifest.load_stage_guidance("nonexistent_stage")


def test_load_pipeline_manifest_no_silent_fallback_on_invalid_json(
    tmp_path: Path,
) -> None:
    """Invalid JSON in manifest must raise ValueError, not silently continue."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "pipeline_manifest.json").write_text(
        "not valid json", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="invalid JSON"):
        load_pipeline_prompt_manifest(prompts_dir)
