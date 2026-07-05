from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
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
        backlog_model="gpt-5.5",
        implementation_agent="codex",
        implementation_model=None,
        review_agent="claude",
        review_model=None,
        allowed_severities={"blocker", "high"},
        cleanup_interval_seconds=21600.0,
        log_path=repo_root / "runs" / "_continuous_loop" / "continuous_loop.log",
        state_path=repo_root / "runs" / "_continuous_loop" / "loop_state.json",
        pid_path=repo_root / "runs" / "_continuous_loop" / "loop.pid",
        batch_config_path=repo_root / "configs" / "backlog_implement_batch.yaml",
        implement_python=(
            repo_root / "apps" / "usertest_implement" / ".venv" / "Scripts" / "python.exe"
        ),
        backlog_python=(
            repo_root / "apps" / "usertest_backlog" / ".venv" / "Scripts" / "python.exe"
        ),
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


def test_append_log_ignores_oserror_from_stderr(tmp_path: Path, monkeypatch) -> None:
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
        backlog_model="gpt-5.5",
        implementation_agent="codex",
        implementation_model=None,
        review_agent="claude",
        review_model=None,
        allowed_severities={"blocker", "high"},
        cleanup_interval_seconds=21600.0,
        log_path=repo_root / "runs" / "_continuous_loop" / "continuous_loop.log",
        state_path=repo_root / "runs" / "_continuous_loop" / "loop_state.json",
        pid_path=repo_root / "runs" / "_continuous_loop" / "loop.pid",
        batch_config_path=repo_root / "configs" / "backlog_implement_batch.yaml",
        implement_python=(
            repo_root / "apps" / "usertest_implement" / ".venv" / "Scripts" / "python.exe"
        ),
        backlog_python=(
            repo_root / "apps" / "usertest_backlog" / ".venv" / "Scripts" / "python.exe"
        ),
    )

    class BrokenStderr:
        def write(self, text: str) -> int:
            raise OSError(22, "Invalid argument")

        def flush(self) -> None:
            raise AssertionError("flush should not be called after write failure")

    monkeypatch.setattr(mod.sys, "stderr", BrokenStderr())

    mod._append_log(ctx, "background-safe log write")

    assert "background-safe log write" in ctx.log_path.read_text(encoding="utf-8")
