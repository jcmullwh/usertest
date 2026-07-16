from pathlib import Path

import pytest
from runner_core import RunnerConfig

from backlog_miner import (
    PromptManifest,
    load_prompt_manifest,
    run_backlog_ensemble,
    run_backlog_prompt,
    run_labeler_jobs,
)


def test_package_surface_exports_miner_api() -> None:
    assert callable(load_prompt_manifest)
    assert callable(run_backlog_prompt)
    assert callable(run_backlog_ensemble)
    assert callable(run_labeler_jobs)


def test_legacy_ensemble_marks_output_analysis_only(tmp_path: Path) -> None:
    manifest = PromptManifest(
        coverage_templates=("coverage.md",),
        bagging_templates=("bagging.md",),
        orphan_template="orphan.md",
        merge_judge_template="merge.md",
        labeler_template="label.md",
    )

    with pytest.warns(DeprecationWarning, match="analysis-only"):
        result = run_backlog_ensemble(
            atoms=[],
            artifacts_dir=tmp_path / "artifacts",
            prompts_dir=tmp_path / "prompts",
            prompt_manifest=manifest,
            agent="codex",
            model=None,
            cfg=RunnerConfig(
                repo_root=tmp_path,
                runs_dir=tmp_path / "runs",
                agents={},
                policies={},
            ),
            miners=0,
            sample_size=0,
            coverage_miners=0,
            bagging_miners=0,
            max_tickets_per_miner=0,
            seed=0,
            resume=False,
            force=False,
            dry_run=True,
            no_merge=True,
            orphan_pass=0,
        )

    assert result["pipeline_kind"] == "legacy_one_pass_analysis"
    assert result["analysis_only"] is True
    assert result["export_eligible"] is False
    assert result["tickets"] == []
