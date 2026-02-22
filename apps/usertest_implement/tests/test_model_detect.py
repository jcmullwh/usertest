from __future__ import annotations

import json
from pathlib import Path

from usertest_implement.model_detect import infer_observed_model


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_infer_observed_model_prefers_target_ref_model(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "target_ref.json", {"model": "gpt-5.2"})
    (run_dir / "agent_stderr.txt").write_text("model=gpt-4.1\n", encoding="utf-8")
    assert infer_observed_model(run_dir=run_dir) == "gpt-5.2"


def test_infer_observed_model_from_agent_attempts_warning(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    _write_json(run_dir / "agent_attempts.json", {"attempts": [{"warnings": ["model=gpt-5.2"]}]})
    assert infer_observed_model(run_dir=run_dir) == "gpt-5.2"


def test_infer_observed_model_from_agent_stderr(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "agent_stderr.txt").write_text("... model=gpt-5.2 ...\n", encoding="utf-8")
    assert infer_observed_model(run_dir=run_dir) == "gpt-5.2"


def test_infer_observed_model_none_when_unavailable(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    assert infer_observed_model(run_dir=run_dir) is None

