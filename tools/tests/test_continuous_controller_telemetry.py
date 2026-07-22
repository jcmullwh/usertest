from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from run_artifacts.lifecycle_events import deserialize_lifecycle_context


def _load_tool() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "continuous_implement_loop.py"
    spec = importlib.util.spec_from_file_location("continuous_controller_metrics", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def test_continuous_controller_propagates_verified_versioned_context(tmp_path: Path) -> None:
    prompts = tmp_path / "configs" / "backlog_prompts"
    prompts.mkdir(parents=True)
    (prompts / "stage.md").write_text("prompt\n", encoding="utf-8")
    settings = tmp_path / "settings.yaml"
    settings.write_text("profile: test\n", encoding="utf-8")
    batch = tmp_path / "batch.yaml"
    batch.write_text("targets: []\n", encoding="utf-8")
    ctx = SimpleNamespace(
        repo_root=tmp_path,
        settings_path=settings,
        batch_config_path=batch,
        backlog_model="gpt-5.6-sol",
        backlog_agent="codex",
        implementation_model="gpt-5.6-sol",
        implementation_agent="codex",
        review_model="gpt-5.6-sol",
        review_agent="codex",
        controller_context=None,
    )

    context = tool._build_controller_context(ctx)
    ctx.controller_context = context
    environment = tool._controller_environment(ctx)
    decoded = deserialize_lifecycle_context(environment["USERTEST_LIFECYCLE_CONTEXT"])

    assert decoded.system_fingerprint["controller_context_verified"] == "true"
    assert decoded.system_fingerprint["score_version"] == "automation_score_v1"
    assert len(decoded.system_fingerprint["prompt_hash"]) == 64
    assert json.loads(decoded.system_fingerprint["models"])["backlog"] == "gpt-5.6-sol"
    assert json.loads(decoded.system_fingerprint["providers"])["backlog"] == "codex"


def test_continuous_pass_invokes_only_observational_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "refresh_pipeline_metrics.py").write_text(
        "# fixture\n", encoding="utf-8"
    )
    events = tmp_path / "owner" / "runs" / "case" / "lifecycle_events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text("{}\n", encoding="utf-8")
    calls: list[list[str]] = []

    def run_logged(_ctx: object, argv: list[str], **_kwargs: object) -> object:
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(tool, "_run_logged", run_logged)
    ctx = SimpleNamespace(
        owner_root=tmp_path / "owner",
        repo_root=tmp_path,
        implement_python=Path("python"),
    )

    tool._refresh_observational_metrics(ctx)

    assert len(calls) == 1
    assert calls[0][1].endswith("refresh_pipeline_metrics.py")
    assert "--stale-after-hours" in calls[0]
