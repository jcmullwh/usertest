from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_adapters import (
    CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS,
    CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES,
    CodexLoginStatusResult,
)
from runner_core import RunnerConfig

import backlog_miner.ensemble as mod
from backlog_miner.pipeline import (
    model_invocation_manifest_path,
    run_stage_prompt_json,
    verify_model_invocation_manifest,
)

_BLOCKED_ENV_VARS = CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS


def _cfg(tmp_path: Path) -> RunnerConfig:
    return RunnerConfig(
        repo_root=tmp_path,
        runs_dir=tmp_path,
        agents={
            "codex": {
                "binary": "codex",
                "config_overrides": [
                    "model_reasoning_effort=high",
                    "features.plugins=false",
                ],
            }
        },
        policies={},
    )


def _login_status(
    *,
    status: str = "Logged in using ChatGPT",
    exit_code: int = 0,
    codex_home: Path,
) -> CodexLoginStatusResult:
    return CodexLoginStatusResult(
        argv=["codex", "-c", "model_reasoning_effort=high", "login", "status"],
        exit_code=exit_code,
        stdout=f"{status}\n",
        stderr="",
        codex_home=str(codex_home),
        auth_env_vars_blank={name: True for name in _BLOCKED_ENV_VARS},
    )


def _write_success_artifacts(
    kwargs: dict[str, object],
    *,
    response: str = "[]",
) -> SimpleNamespace:
    raw_events_path = kwargs["raw_events_path"]
    last_message_path = kwargs["last_message_path"]
    stderr_path = kwargs["stderr_path"]
    assert isinstance(raw_events_path, Path)
    assert isinstance(last_message_path, Path)
    assert isinstance(stderr_path, Path)
    raw_events_path.write_text('{"type":"item.completed"}\n', encoding="utf-8")
    last_message_path.write_text(response, encoding="utf-8", newline="\n")
    stderr_path.write_text("", encoding="utf-8")
    return SimpleNamespace(exit_code=0)


def _load_receipt(out_dir: Path, tag: str = "miner_001") -> dict[str, object]:
    return json.loads((out_dir / f"{tag}.codex_auth_receipt.json").read_text(encoding="utf-8"))


def test_codex_prompt_uses_child_only_chatgpt_subscription_controls_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "host_codex_home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    originals: dict[str, str] = {}
    for index, name in enumerate(_BLOCKED_ENV_VARS):
        originals[name] = f"parent-secret-{index}"
        monkeypatch.setenv(name, originals[name])

    probe_calls: list[dict[str, object]] = []
    exec_calls: list[dict[str, object]] = []

    def fake_probe(**kwargs: object) -> CodexLoginStatusResult:
        probe_calls.append(kwargs)
        return _login_status(codex_home=codex_home)

    def fake_exec(**kwargs: object) -> SimpleNamespace:
        exec_calls.append(kwargs)
        assert {name: os.environ[name] for name in _BLOCKED_ENV_VARS} == originals
        return _write_success_artifacts(kwargs)

    monkeypatch.setattr(mod, "probe_codex_login_status", fake_probe)
    monkeypatch.setattr(mod, "run_codex_exec", fake_exec)
    out_dir = tmp_path / "artifacts"

    response = mod.run_backlog_prompt(
        agent="codex",
        prompt="Return an empty list.",
        out_dir=out_dir,
        tag="miner_001",
        model="gpt-test",
        cfg=_cfg(tmp_path),
    )

    assert response == "[]"
    assert len(probe_calls) == 2
    assert len(exec_calls) == 1
    assert {name: os.environ[name] for name in _BLOCKED_ENV_VARS} == originals

    for call in [*probe_calls, *exec_calls]:
        env_overrides = call["env_overrides"]
        config_overrides = call["config_overrides"]
        assert isinstance(env_overrides, dict)
        assert isinstance(config_overrides, list)
        assert all(env_overrides[name] == "" for name in _BLOCKED_ENV_VARS)
        assert Path(env_overrides["CODEX_HOME"]).resolve() == codex_home.resolve()
        assert config_overrides[-len(CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES) :] == list(
            CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES
        )
        assert config_overrides[:2] == [
            "model_reasoning_effort=high",
            "features.plugins=false",
        ]

    exec_call = exec_calls[0]
    assert exec_call["ignore_user_config"] is True
    receipt = _load_receipt(out_dir)
    receipt_digest = receipt.pop("receipt_sha256")
    assert receipt_digest == mod._json_digest(receipt)
    assert receipt["auth_mode"] == "canonical_host_chatgpt_subscription_cache"
    assert receipt["api_fallback_allowed"] is False
    assert receipt["credential_cache_copied"] is False
    assert receipt["chatgpt_subscription_auth_verified"] is True
    assert receipt["preflight"]["chatgpt_status_exact"] is True
    assert receipt["postcheck"]["chatgpt_status_exact"] is True
    assert receipt["model_activation"]["succeeded"] is True
    serialized = json.dumps(receipt, sort_keys=True)
    assert not any(value in serialized for value in originals.values())


@pytest.mark.parametrize(
    "override",
    [
        'chatgpt_base_url="https://billable.invalid"',
        'openai_base_url="https://billable.invalid/v1"',
        'model_providers.custom.base_url="https://billable.invalid/v1"',
        'profile="billable"',
        'provider_token="secret"',
    ],
)
def test_codex_prompt_rejects_unsafe_config_before_any_codex_process(
    override: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "host_codex_home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("OPENAI_API_KEY", "parent-key-remains")
    cfg = _cfg(tmp_path)
    cfg.agents["codex"]["config_overrides"] = [override]
    process_calls = 0

    def unexpected_process(**_: object) -> object:
        nonlocal process_calls
        process_calls += 1
        raise AssertionError("unsafe configuration reached a Codex process")

    monkeypatch.setattr(mod, "probe_codex_login_status", unexpected_process)
    monkeypatch.setattr(mod, "run_codex_exec", unexpected_process)

    with pytest.raises(ValueError, match="codex_subscription_config_override_forbidden"):
        mod.run_backlog_prompt(
            agent="codex",
            prompt="Do work.",
            out_dir=tmp_path / "artifacts",
            tag="miner_001",
            model=None,
            cfg=cfg,
        )

    assert process_calls == 0
    assert os.environ["OPENAI_API_KEY"] == "parent-key-remains"


def test_generic_auth_receipt_rejects_semantic_route_tampering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "host_codex_home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        mod,
        "probe_codex_login_status",
        lambda **_: _login_status(codex_home=codex_home),
    )
    monkeypatch.setattr(mod, "run_codex_exec", lambda **kwargs: _write_success_artifacts(kwargs))
    out_dir = tmp_path / "artifacts"
    prompt = "Return an empty list."
    mod.run_backlog_prompt(
        agent="codex",
        prompt=prompt,
        out_dir=out_dir,
        tag="miner_001",
        model=None,
        cfg=_cfg(tmp_path),
    )
    receipt_path = out_dir / "miner_001.codex_auth_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["effective_overrides"][-4] = 'chatgpt_base_url="https://billable.invalid"'
    receipt["effective_overrides_sha256"] = mod._json_digest(receipt["effective_overrides"])
    receipt["receipt_sha256"] = mod._json_digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    errors = mod.verify_codex_auth_receipt(
        receipt_path=receipt_path,
        prompt=prompt,
        raw_events_path=out_dir / "miner_001.raw_events.jsonl",
        last_message_path=out_dir / "miner_001.last_message.txt",
        stderr_path=out_dir / "miner_001.stderr.txt",
    )

    assert any("canonical_route_suffix_missing" in error for error in errors)


def test_codex_preflight_failure_blocks_model_activation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "host_codex_home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    statuses = iter(
        [
            _login_status(status="Logged in using an API key", codex_home=codex_home),
            _login_status(codex_home=codex_home),
        ]
    )
    model_calls = 0

    def fake_exec(**kwargs: object) -> SimpleNamespace:
        nonlocal model_calls
        model_calls += 1
        return _write_success_artifacts(kwargs)

    monkeypatch.setattr(mod, "probe_codex_login_status", lambda **_: next(statuses))
    monkeypatch.setattr(mod, "run_codex_exec", fake_exec)
    out_dir = tmp_path / "artifacts"

    with pytest.raises(RuntimeError, match="before model activation"):
        mod.run_backlog_prompt(
            agent="codex",
            prompt="Do work.",
            out_dir=out_dir,
            tag="miner_001",
            model=None,
            cfg=_cfg(tmp_path),
        )

    assert model_calls == 0
    receipt = _load_receipt(out_dir)
    assert receipt["preflight"]["chatgpt_status_exact"] is False
    assert receipt["model_activation"]["attempted"] is False
    assert receipt["chatgpt_subscription_auth_verified"] is False


def test_codex_postcheck_failure_rejects_successful_model_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "host_codex_home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    statuses = iter(
        [
            _login_status(codex_home=codex_home),
            _login_status(status="Logged in using an API key", codex_home=codex_home),
        ]
    )
    monkeypatch.setattr(mod, "probe_codex_login_status", lambda **_: next(statuses))
    monkeypatch.setattr(
        mod,
        "run_codex_exec",
        lambda **kwargs: _write_success_artifacts(kwargs),
    )
    out_dir = tmp_path / "artifacts"

    with pytest.raises(RuntimeError, match="after model activation"):
        mod.run_backlog_prompt(
            agent="codex",
            prompt="Do work.",
            out_dir=out_dir,
            tag="miner_001",
            model=None,
            cfg=_cfg(tmp_path),
        )

    receipt = _load_receipt(out_dir)
    assert receipt["model_activation"]["succeeded"] is True
    assert receipt["postcheck"]["chatgpt_status_exact"] is False
    assert receipt["chatgpt_subscription_auth_verified"] is False
    assert not (out_dir / "miner_001.response.txt").exists()


def test_codex_model_failure_still_runs_postcheck_in_finally(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "host_codex_home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    probe_calls = 0

    def fake_probe(**_: object) -> CodexLoginStatusResult:
        nonlocal probe_calls
        probe_calls += 1
        return _login_status(codex_home=codex_home)

    def fake_exec(**_: object) -> SimpleNamespace:
        raise OSError("model launch failed")

    monkeypatch.setattr(mod, "probe_codex_login_status", fake_probe)
    monkeypatch.setattr(mod, "run_codex_exec", fake_exec)
    out_dir = tmp_path / "artifacts"

    with pytest.raises(OSError, match="model launch failed"):
        mod.run_backlog_prompt(
            agent="codex",
            prompt="Do work.",
            out_dir=out_dir,
            tag="miner_001",
            model=None,
            cfg=_cfg(tmp_path),
        )

    assert probe_calls == 2
    receipt = _load_receipt(out_dir)
    assert receipt["model_activation"]["attempted"] is True
    assert receipt["model_activation"]["succeeded"] is False
    assert receipt["postcheck"]["chatgpt_status_exact"] is True
    assert receipt["chatgpt_subscription_auth_verified"] is False


def test_new_invocation_replaces_stale_verified_receipt_before_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "host_codex_home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    out_dir = tmp_path / "artifacts"
    out_dir.mkdir()
    receipt_path = out_dir / "miner_001.codex_auth_receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "verified",
                "chatgpt_subscription_auth_verified": True,
                "stale": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        mod,
        "probe_codex_login_status",
        lambda **_: (_ for _ in ()).throw(RuntimeError("preflight exploded")),
    )

    with pytest.raises(RuntimeError, match="preflight exploded"):
        mod.run_backlog_prompt(
            agent="codex",
            prompt="new prompt",
            out_dir=out_dir,
            tag="miner_001",
            model=None,
            cfg=_cfg(tmp_path),
        )

    pending = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert pending["status"] == "pending"
    assert pending["chatgpt_subscription_auth_verified"] is False
    assert pending["prompt_sha256"] == mod._sha256_text("new prompt")
    assert "stale" not in pending


def test_stage_prompt_binds_verified_codex_subscription_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "host_codex_home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        mod,
        "probe_codex_login_status",
        lambda **_: _login_status(codex_home=codex_home),
    )
    monkeypatch.setattr(
        mod,
        "run_codex_exec",
        lambda **kwargs: _write_success_artifacts(kwargs, response="[\n]\n"),
    )
    out_dir = tmp_path / "stage"

    response = run_stage_prompt_json(
        stage="problem_prioritization",
        prompt="Return an empty list.\nUse JSON only.",
        out_dir=out_dir,
        tag="problem_prioritization_001",
        agent="codex",
        model=None,
        cfg=_cfg(tmp_path),
    )

    assert response == "[\n]\n"
    manifest_path = model_invocation_manifest_path(
        out_dir=out_dir,
        tag="problem_prioritization_001",
    )
    assert verify_model_invocation_manifest(manifest_path) == []
    receipt_path = out_dir / "problem_prioritization_001.codex_auth_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["status"] = "failed"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert "model_invocation_codex_receipt_changed" in (
        verify_model_invocation_manifest(manifest_path)
    )
