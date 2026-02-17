from __future__ import annotations

from pathlib import Path

import yaml

from backlog_repo.actions import load_backlog_actions_yaml


def test_load_backlog_actions_yaml_bootstraps_missing_file(tmp_path: Path) -> None:
    actions_path = tmp_path / "configs" / "backlog_actions.yaml"

    loaded = load_backlog_actions_yaml(actions_path)

    assert loaded == {}
    assert actions_path.exists()

    payload = yaml.safe_load(actions_path.read_text(encoding="utf-8"))
    assert payload == {"version": 1, "actions": []}
