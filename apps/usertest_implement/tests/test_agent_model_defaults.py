from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_yaml(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_agent_config_defaults_use_current_model_routes() -> None:
    agents = _load_yaml(REPO_ROOT / "configs" / "agents.yaml")["agents"]

    assert agents["codex"]["default_model"] == "gpt-5.5"
    assert agents["claude"]["default_model"] == "claude-sonnet-5"
    assert agents["gemini"]["default_model"] == "auto"


def test_batch_worker_roster_keeps_codex_on_latest_frontier_model() -> None:
    batch_config = _load_yaml(REPO_ROOT / "configs" / "backlog_implement_batch.yaml")
    workers = batch_config["defaults"]["worker_roster"]

    codex_worker = next(worker for worker in workers if worker["agent"] == "codex")
    assert codex_worker["model"] == "gpt-5.5"
