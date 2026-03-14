from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace


def _load_module():
    module_path = (
        Path(__file__).resolve().parents[3] / "tools" / "continuous_implement_loop.py"
    )
    spec = importlib.util.spec_from_file_location("continuous_implement_loop", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_run_batch_pass_invokes_usertest_implement_batch_run(tmp_path: Path) -> None:
    mod = _load_module()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    ctx = mod.LoopContext(
        repo_root=repo_root,
        owner_root=repo_root,
        runs_dir=repo_root / "runs" / "usertest_implement",
        target="usertest",
        repo_input=str(repo_root),
        settings_path=repo_root / "configs" / "usertest_implement_settings.yaml",
        settings_profile="default",
        backlog_agent="codex",
        backlog_model="gpt-5.4",
        implementation_agent="codex",
        implementation_model=None,
        review_agent="claude",
        review_model=None,
        allowed_severities={"blocker", "high"},
        sleep_seconds=60.0,
        cleanup_interval_seconds=21600.0,
        log_path=repo_root / "runs" / "_continuous_loop" / "continuous_loop.log",
        state_path=repo_root / "runs" / "_continuous_loop" / "loop_state.json",
        pid_path=repo_root / "runs" / "_continuous_loop" / "loop.pid",
        batch_config_path=repo_root / "configs" / "backlog_implement_batch.yaml",
        implement_python=repo_root / "apps" / "usertest_implement" / ".venv" / "Scripts" / "python.exe",
        backlog_python=repo_root / "apps" / "usertest_backlog" / ".venv" / "Scripts" / "python.exe",
    )
    captured: dict[str, object] = {}

    def _fake_write_state(*args, **kwargs):
        captured["state"] = kwargs

    def _fake_run_logged(*args, **kwargs):
        captured["argv"] = args[1]
        captured["label"] = kwargs["label"]
        return SimpleNamespace(returncode=0)

    mod._write_state = _fake_write_state
    mod._run_logged = _fake_run_logged

    assert mod._run_batch_pass(ctx) is True
    assert captured["label"] == "batch run"
    assert captured["argv"] == [
        str(ctx.implement_python),
        "-m",
        "usertest_implement.cli",
        "--repo-root",
        str(repo_root),
        "batch",
        "run",
        "--config",
        str(ctx.batch_config_path),
    ]
