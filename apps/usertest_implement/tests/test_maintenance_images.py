from __future__ import annotations

import argparse
import json
from pathlib import Path

import usertest_implement.commands.maintenance_images as maintenance_commands


def test_cmd_maintenance_images_list_emits_json(monkeypatch, capsys, tmp_path: Path) -> None:
    """The list command should print the maintenance-image inventory as JSON."""

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    monkeypatch.setattr(
        maintenance_commands,
        "list_local_maintenance_images",
        lambda **_kwargs: {"schema_version": 1, "entries": []},
    )

    result = maintenance_commands._cmd_maintenance_images_list(
        argparse.Namespace(repo_root=repo_root, timeout_seconds=12.0)
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {"schema_version": 1, "entries": []}


def test_cmd_maintenance_images_cleanup_passes_dry_run(monkeypatch, capsys, tmp_path: Path) -> None:
    """The cleanup command should forward explicit dry-run requests to runner_core."""

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    captured: dict[str, object] = {}

    def _fake_cleanup(**kwargs):
        captured.update(kwargs)
        return {"schema_version": 1, "deleted_tags": []}

    monkeypatch.setattr(maintenance_commands, "cleanup_local_maintenance_images", _fake_cleanup)

    result = maintenance_commands._cmd_maintenance_images_cleanup(
        argparse.Namespace(repo_root=repo_root, timeout_seconds=5.0, dry_run=True)
    )

    assert result == 0
    assert captured["repo_root"] == repo_root
    assert captured["timeout_seconds"] == 5.0
    assert captured["dry_run"] is True
    assert "protected_refs" not in captured
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 1,
        "kind": "manual_cleanup",
        "cleanup": {"schema_version": 1, "deleted_tags": []},
        "after_inventory": None,
        "errors": [],
    }
