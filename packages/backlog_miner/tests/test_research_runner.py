from __future__ import annotations

import json
from pathlib import Path

import pytest
from runner_core import RunnerConfig, RunResult

import backlog_miner.research_runner as mod


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _cfg(tmp_path: Path) -> RunnerConfig:
    return RunnerConfig(
        repo_root=tmp_path,
        runs_dir=tmp_path / "runs",
        agents={},
        policies={},
    )


def test_run_repro_research_stage_dry_run_writes_requests_and_placeholders(tmp_path: Path) -> None:
    artifacts_dir = tmp_path / "compiled" / "target_a.backlog_artifacts"
    doc = mod.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input=None,
        target_slug="target_a",
        selected_problems=[{"problem_id": "problem:test-1", "title": "Test"}],
        artifacts_dir=artifacts_dir,
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
        dry_run=True,
    )

    assert doc.get("stage") == "repro_research"
    assert doc.get("input_meta", {}).get("dry_run") is True
    assert isinstance(doc.get("items"), list)
    assert len(doc["items"]) == 1
    dossier = doc["items"][0]
    assert dossier.get("implementation_performed") is False
    assert dossier.get("diff_classification") == "no_changes"
    assert dossier.get("writes_used") is False

    requests_path = Path(doc["artifacts"]["requests_json"])
    assert requests_path.exists()


def test_run_repro_research_stage_rejects_missing_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guidance_path = tmp_path / "configs" / "backlog_stage_guidance" / "repro_research.md"
    guidance_path.parent.mkdir(parents=True, exist_ok=True)
    guidance_path.write_text("# guidance\n", encoding="utf-8")

    def fake_run_once(*, config: RunnerConfig, request: object) -> RunResult:
        run_dir = tmp_path / "run_missing_ext"
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            run_dir / "report.json",
            {
                "schema_version": 1,
                "kind": "troubleshoot_v1",
                "status": "failure",
                "goal": "x",
                "failure_point": "y",
                "evidence": {"what_happened": "z"},
                "attempted_fixes": [],
                "recommended_fix_path": ["investigate"],
            },
        )
        _write_json(run_dir / "diff_numstat.json", [])
        return RunResult(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(mod, "run_once", fake_run_once)

    with pytest.raises(ValueError, match=r"missing required extensions\.backlog_repro_research"):
        mod.run_repro_research_stage(
            repo_root=tmp_path,
            repo_input="pip:agent-adapters",
            target_slug="target_a",
            selected_problems=[{"problem_id": "problem:test-1"}],
            artifacts_dir=tmp_path / "compiled" / "x.backlog_artifacts",
            agent="codex",
            model=None,
            cfg=_cfg(tmp_path),
            dry_run=False,
        )


def test_run_repro_research_stage_classifies_suspicious_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guidance_path = tmp_path / "configs" / "backlog_stage_guidance" / "repro_research.md"
    guidance_path.parent.mkdir(parents=True, exist_ok=True)
    guidance_path.write_text("# guidance\n", encoding="utf-8")

    ext = {
        "problem_id": "problem:test-1",
        "reproduction_status": "reproduced",
        "writes_used": True,
        "writes_purpose": ["temporary_instrumentation"],
        "implementation_performed": False,
        "root_cause_hypotheses": ["Likely missing validation"],
        "broader_class_assessment": "unknown",
        "unknowns": ["Need smaller repro"],
    }

    def fake_run_once(*, config: RunnerConfig, request: object) -> RunResult:
        run_dir = tmp_path / "run_suspicious"
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            run_dir / "report.json",
            {
                "schema_version": 1,
                "kind": "troubleshoot_v1",
                "status": "partial",
                "goal": "x",
                "failure_point": "y",
                "evidence": {"what_happened": "z"},
                "attempted_fixes": [],
                "recommended_fix_path": ["investigate"],
                "extensions": {"backlog_repro_research": ext},
            },
        )
        _write_json(
            run_dir / "diff_numstat.json",
            [
                {
                    "path": "packages/example/src/example/core.py",
                    "lines_added": 1,
                    "lines_removed": 0,
                }
            ],
        )
        return RunResult(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(mod, "run_once", fake_run_once)

    doc = mod.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input="pip:agent-adapters",
        target_slug="target_a",
        selected_problems=[{"problem_id": "problem:test-1"}],
        artifacts_dir=tmp_path / "compiled" / "x.backlog_artifacts",
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
        dry_run=False,
    )

    items = doc.get("items") or []
    assert isinstance(items, list)
    assert items
    assert items[0]["diff_classification"] == "suspicious_implementation"
