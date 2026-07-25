from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import runner_core.runner as runner


def test_token_monitoring_hook_writes_success_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def fake_write_run_monitoring(path: Path) -> dict[str, object]:
        (path / "token_monitoring.json").write_text(
            json.dumps({"schema_version": 1}) + "\n",
            encoding="utf-8",
        )
        return {"schema_version": 1}

    monkeypatch.setitem(
        sys.modules,
        "token_monitoring",
        SimpleNamespace(write_run_monitoring=fake_write_run_monitoring),
    )

    runner._maybe_write_token_monitoring_artifacts(run_dir)

    assert (run_dir / "token_monitoring.json").exists()
    assert not (run_dir / "token_monitoring_error.json").exists()


def test_token_monitoring_hook_failure_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def fake_write_run_monitoring(_path: Path) -> dict[str, object]:
        raise RuntimeError("monitor failed")

    monkeypatch.setitem(
        sys.modules,
        "token_monitoring",
        SimpleNamespace(write_run_monitoring=fake_write_run_monitoring),
    )

    runner._maybe_write_token_monitoring_artifacts(run_dir)

    payload = json.loads((run_dir / "token_monitoring_error.json").read_text(encoding="utf-8"))
    assert payload["non_fatal"] is True
    assert payload["type"] == "RuntimeError"
