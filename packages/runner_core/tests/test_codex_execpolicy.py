from __future__ import annotations

import json
import os
import tomllib
from hashlib import sha256
from pathlib import Path

import pytest
from agent_adapters import (
    CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS,
    CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES,
    build_codex_subscription_config_overrides,
)

import runner_core.codex_execpolicy as execpolicy_mod
from runner_core.codex_execpolicy import (
    CONTROLLED_CODEX_NON_ROUTING_CONFIG_OVERRIDES,
    CONTROLLED_CODEX_WINDOWS_SANDBOX_CONFIG_OVERRIDE,
    ControlledCodexExecpolicyOverlay,
    capture_probe_workspace_state,
    codex_execpolicy_receipt_sha256,
    codex_project_trust_override,
    install_controlled_codex_execpolicy,
    verify_controlled_codex_execpolicy_receipt,
)


def _blank_subscription_environment() -> dict[str, bool]:
    return {name: True for name in CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS}


def _activation_probe(**overrides: object) -> dict[str, object]:
    argv = ["codex", "exec", "--sandbox", "workspace-write"]
    if os.name == "nt":
        argv.append("--ignore-rules")
    payload: dict[str, object] = {
        "ok": True,
        "marker_seen": True,
        "workspace_unchanged": True,
        "argv": argv,
    }
    payload.update(overrides)
    return payload


def _bind_default_config(
    overlay: ControlledCodexExecpolicyOverlay,
) -> tuple[list[str], list[str]]:
    mission = build_codex_subscription_config_overrides(
        ["model_reasoning_effort=high"],
        source="test_controlled",
        internal_safe_overrides=[
            *CONTROLLED_CODEX_NON_ROUTING_CONFIG_OVERRIDES,
            *([CONTROLLED_CODEX_WINDOWS_SANDBOX_CONFIG_OVERRIDE] if os.name == "nt" else []),
            overlay.project_trust_override,
        ],
    )
    return overlay.bind_effective_config(mission)


@pytest.fixture(autouse=True)
def _host_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": "test-access",
                    "refresh_token": "test-refresh",
                    "account_id": "test-account",
                },
            }
        ),
        encoding="utf-8",
    )
    rules_dir = codex_home / "rules"
    rules_dir.mkdir()
    (rules_dir / "default.rules").write_text(
        'prefix_rule(pattern=["host-safe"], decision="allow")\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))


def test_controlled_execpolicy_replaces_and_restores_target_rules(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    target_rules = workspace / ".codex" / "rules"
    target_rules.mkdir(parents=True)
    original = target_rules / "target.rules"
    original.write_text(
        'prefix_rule(pattern=["target"], decision="forbidden")\n',
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    overlay = install_controlled_codex_execpolicy(
        workspace_dir=workspace,
        run_dir=run_dir,
        allow_prefixes=(("git", "rev-parse"), ("python", "-m", "pytest")),
    )
    mission_overrides, activation_overrides = _bind_default_config(overlay)

    assert not original.exists()
    controlled = target_rules / "usertest-controlled.rules"
    if os.name == "nt":
        assert not controlled.exists()
    else:
        assert controlled.read_text(encoding="utf-8") == (
            'prefix_rule(pattern=["git", "rev-parse"], decision="allow")\n'
            'prefix_rule(pattern=["python", "-m", "pytest"], decision="allow")\n'
        )
    receipt = json.loads(overlay.receipt_path.read_text(encoding="utf-8"))
    assert receipt["restore_status"] == "pending"
    assert receipt["target_rules_manifest_before"][0]["path"] == "target.rules"
    assert receipt["configuration_mode"] == "host_codex_home_with_isolated_config"
    host_codex_home = Path(os.environ["CODEX_HOME"]).resolve()
    auth_path = host_codex_home / "auth.json"
    auth_before = auth_path.read_bytes()
    assert overlay.host_codex_home == host_codex_home
    assert receipt["auth_cache_copied"] is False
    assert receipt["auth_cache_deleted"] is False
    assert receipt["chatgpt_subscription_auth_verified"] is False
    assert receipt["auth_verification_status"] == "pending"
    assert receipt["global_rules_loaded"] is (os.name != "nt")
    assert receipt["controlled_rules_ignored"] is (os.name == "nt")
    assert receipt["controlled_rules_written"] is (os.name != "nt")
    assert receipt["controlled_rules_enforcement_mode"] == (
        "ignored_native_windows_sandbox" if os.name == "nt" else "project_execpolicy"
    )
    assert receipt["host_global_rules_manifest_before"][0]["path"] == "default.rules"
    config_receipt = json.loads(
        Path(receipt["controlled_config_overrides_path"]).read_text(encoding="utf-8")
    )
    assert config_receipt["status"] == "bound"
    assert config_receipt["platform_os_name"] == os.name
    assert config_receipt["canonical_subscription_route_verified"] is True
    assert config_receipt["native_windows_sandbox_mode"] == (
        "unelevated" if os.name == "nt" else "not_applicable"
    )
    assert config_receipt["controlled_rules_ignored"] is (os.name == "nt")
    assert config_receipt["controlled_rules_written"] is (os.name != "nt")
    assert config_receipt["user_config_ignored"] is True
    assert config_receipt["target_project_config_isolated"] is True
    assert config_receipt["mission_overrides"] == mission_overrides
    assert config_receipt["preflight_overrides"] == mission_overrides
    assert config_receipt["postcheck_overrides"] == mission_overrides
    assert config_receipt["activation_overrides"] == activation_overrides
    assert config_receipt["canonical_route_overrides"] == list(
        CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES
    )
    assert mission_overrides[-len(CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES) :] == list(
        CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES
    )
    assert auth_path.read_bytes() == auth_before
    assert not (host_codex_home / ".tmp").exists()

    assert overlay.restore() == []
    assert original.read_text(encoding="utf-8").endswith('decision="forbidden")\n')
    assert not (run_dir / "codex_execpolicy_target_rules").exists()
    restored = json.loads(overlay.receipt_path.read_text(encoding="utf-8"))
    assert restored["restore_status"] == "restored"
    assert (
        restored["target_rules_manifest_after_restore"] == restored["target_rules_manifest_before"]
    )
    assert restored["host_auth_file_after"]["state"] == "present"
    assert restored["host_auth_identity_unchanged"] is True
    assert restored["host_global_rules_unchanged"] is True
    assert restored["canonical_subscription_route_verified"] is True
    assert restored["native_windows_sandbox_mode"] == (
        "unelevated" if os.name == "nt" else "not_applicable"
    )
    assert auth_path.read_bytes() == auth_before


def test_controlled_execpolicy_removes_runner_only_directories(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    overlay = install_controlled_codex_execpolicy(
        workspace_dir=workspace,
        run_dir=run_dir,
        allow_prefixes=(("pytest",),),
    )

    assert (workspace / ".codex" / "rules" / "usertest-controlled.rules").is_file() is (
        os.name != "nt"
    )
    assert overlay.restore() == []
    assert not (workspace / ".codex").exists()


def test_controlled_execpolicy_detects_agent_tampering_and_still_restores(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    original_rules = workspace / ".codex" / "rules"
    original_rules.mkdir(parents=True)
    (original_rules / "original.rules").write_text("original\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    overlay = install_controlled_codex_execpolicy(
        workspace_dir=workspace,
        run_dir=run_dir,
        allow_prefixes=(("python",),),
    )
    _bind_default_config(overlay)
    overlay.rules_dir.mkdir(parents=True, exist_ok=True)
    (overlay.rules_dir / "unexpected.rules").write_text("unexpected\n", encoding="utf-8")

    assert overlay.restore() == ["codex_execpolicy_overlay_changed_during_agent_run"]
    assert (original_rules / "original.rules").read_text(encoding="utf-8") == "original\n"
    assert not (original_rules / "unexpected.rules").exists()


def test_controlled_execpolicy_isolates_and_restores_target_config_exactly(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    target_config = workspace / ".codex" / "config.toml"
    target_config.parent.mkdir(parents=True)
    original_bytes = (
        b'model_provider="alternate"\r\n'
        b'chatgpt_base_url="https://alternate.invalid/backend-api"\r\n'
    )
    target_config.write_bytes(original_bytes)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    overlay = install_controlled_codex_execpolicy(
        workspace_dir=workspace,
        run_dir=run_dir,
        allow_prefixes=(("python",),),
    )

    assert not target_config.exists()
    assert overlay.target_config_backup_path is not None
    assert overlay.target_config_backup_path.read_bytes() == original_bytes
    mission_overrides, _activation_overrides = _bind_default_config(overlay)
    assert mission_overrides[-len(CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES) :] == list(
        CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES
    )
    assert not any("alternate.invalid" in value for value in mission_overrides)

    assert overlay.restore() == []
    assert target_config.read_bytes() == original_bytes
    receipt = json.loads(overlay.receipt_path.read_text(encoding="utf-8"))
    assert receipt["target_project_config_isolated"] is True
    assert receipt["target_config_manifest_while_isolated"] == []
    assert (
        receipt["target_config_manifest_after_restore"] == receipt["target_config_manifest_before"]
    )


def test_controlled_execpolicy_archives_config_tampering_and_restores_original(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    target_config = workspace / ".codex" / "config.toml"
    target_config.parent.mkdir(parents=True)
    original_bytes = b'model_reasoning_effort="high"\r\n'
    unexpected_bytes = b'openai_base_url="https://alternate.invalid/v1"\n'
    target_config.write_bytes(original_bytes)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    overlay = install_controlled_codex_execpolicy(
        workspace_dir=workspace,
        run_dir=run_dir,
        allow_prefixes=(("python",),),
    )
    _bind_default_config(overlay)

    preexisting_archive = run_dir / "codex_execpolicy_unexpected_config.toml"
    preexisting_archive_bytes = b"preexisting archive\n"
    preexisting_archive.write_bytes(preexisting_archive_bytes)
    target_config.write_bytes(unexpected_bytes)

    assert overlay.restore() == ["codex_execpolicy_target_config_changed_during_agent_run"]
    assert target_config.read_bytes() == original_bytes
    assert preexisting_archive.read_bytes() == preexisting_archive_bytes
    archived_unexpected = run_dir / "codex_execpolicy_unexpected_config.1.toml"
    assert archived_unexpected.read_bytes() == unexpected_bytes
    receipt = json.loads(overlay.receipt_path.read_text(encoding="utf-8"))
    assert receipt["restore_status"] == "failed"
    assert Path(receipt["unexpected_target_config_archive"]) == archived_unexpected.resolve()
    assert (
        receipt["target_config_manifest_after_restore"] == receipt["target_config_manifest_before"]
    )


def test_controlled_execpolicy_backup_collisions_do_not_mutate_target(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    target_config = workspace / ".codex" / "config.toml"
    target_rules = workspace / ".codex" / "rules"
    target_rules.mkdir(parents=True)
    target_config_bytes = b'model_reasoning_effort="high"\r\n'
    target_rules_bytes = b"original rule\r\n"
    target_config.write_bytes(target_config_bytes)
    (target_rules / "target.rules").write_bytes(target_rules_bytes)

    config_collision_run = tmp_path / "config-collision-run"
    config_collision_run.mkdir()
    config_collision = config_collision_run / "codex_execpolicy_target_config.toml"
    config_collision_bytes = b"existing config backup\n"
    config_collision.write_bytes(config_collision_bytes)
    with pytest.raises(ValueError, match="target_config_backup_already_exists"):
        install_controlled_codex_execpolicy(
            workspace_dir=workspace,
            run_dir=config_collision_run,
            allow_prefixes=(("python",),),
        )
    assert target_config.read_bytes() == target_config_bytes
    assert (target_rules / "target.rules").read_bytes() == target_rules_bytes
    assert config_collision.read_bytes() == config_collision_bytes

    rules_collision_run = tmp_path / "rules-collision-run"
    rules_collision_run.mkdir()
    rules_collision = rules_collision_run / "codex_execpolicy_target_rules"
    rules_collision.mkdir()
    rules_collision_bytes = b"existing rules backup\n"
    (rules_collision / "collision.rules").write_bytes(rules_collision_bytes)
    with pytest.raises(ValueError, match="codex_execpolicy_backup_already_exists"):
        install_controlled_codex_execpolicy(
            workspace_dir=workspace,
            run_dir=rules_collision_run,
            allow_prefixes=(("python",),),
        )
    assert target_config.read_bytes() == target_config_bytes
    assert (target_rules / "target.rules").read_bytes() == target_rules_bytes
    assert (rules_collision / "collision.rules").read_bytes() == rules_collision_bytes


def test_controlled_execpolicy_rejects_ambiguous_tokens(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="codex_execpolicy_prefix_token_invalid"):
        install_controlled_codex_execpolicy(
            workspace_dir=tmp_path,
            run_dir=tmp_path,
            allow_prefixes=(("python\nwhoami",),),
        )


def test_controlled_execpolicy_does_not_preverify_api_key_file_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = Path(os.environ["CODEX_HOME"])
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "apikey",
                "OPENAI_API_KEY": "test-key",
                "tokens": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    overlay = install_controlled_codex_execpolicy(
        workspace_dir=workspace,
        run_dir=run_dir,
        allow_prefixes=(("python",),),
    )
    _bind_default_config(overlay)
    receipt = json.loads(overlay.receipt_path.read_text(encoding="utf-8"))
    assert receipt["host_auth_file_before"]["state"] == "present"
    assert receipt["host_auth_shape_before"]["valid_chatgpt_shape"] is False
    assert receipt["chatgpt_subscription_auth_verified"] is False
    assert overlay.restore() == []


def test_subscription_verification_requires_login_status_and_activation_probe(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    overlay = install_controlled_codex_execpolicy(
        workspace_dir=workspace,
        run_dir=run_dir,
        allow_prefixes=(("python",),),
    )
    _bind_default_config(overlay)

    overlay.record_login_status(
        {
            "ok": True,
            "chatgpt_status_exact": True,
            "status_kind": "chatgpt",
            "auth_env_vars_blank": _blank_subscription_environment(),
        }
    )
    after_status = json.loads(overlay.receipt_path.read_text(encoding="utf-8"))
    assert after_status["chatgpt_subscription_login_status_verified"] is True
    assert after_status["chatgpt_subscription_auth_verified"] is False
    assert after_status["auth_verification_status"] == "pending"

    overlay.record_activation_probe(
        _activation_probe(
            required_commands=["python --version"],
            required_commands_seen=["python --version"],
        )
    )
    verified = json.loads(overlay.receipt_path.read_text(encoding="utf-8"))
    assert verified["chatgpt_subscription_activation_probe_verified"] is True
    assert verified["chatgpt_subscription_auth_verified"] is False
    assert verified["auth_verification_status"] == "pending_postcheck"
    overlay.record_post_login_status(
        {
            "ok": True,
            "chatgpt_status_exact": True,
            "auth_env_vars_blank": _blank_subscription_environment(),
        }
    )
    post_verified = json.loads(overlay.receipt_path.read_text(encoding="utf-8"))
    assert post_verified["chatgpt_subscription_post_login_status_verified"] is True
    assert post_verified["chatgpt_subscription_auth_verified"] is True
    assert post_verified["auth_verification_status"] == "verified"
    assert overlay.restore() == []
    assert verify_controlled_codex_execpolicy_receipt(overlay.receipt_path) == []


def test_failed_activation_probe_never_verifies_subscription(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    overlay = install_controlled_codex_execpolicy(
        workspace_dir=workspace,
        run_dir=run_dir,
        allow_prefixes=(("python",),),
    )
    overlay.record_login_status(
        {
            "ok": True,
            "chatgpt_status_exact": True,
            "auth_env_vars_blank": _blank_subscription_environment(),
        }
    )
    overlay.record_activation_probe(
        _activation_probe(
            ok=False,
            marker_seen=False,
            required_commands=["python --version"],
            required_commands_seen=[],
            reason="probe failed",
        )
    )
    overlay.record_post_login_status(
        {
            "ok": True,
            "chatgpt_status_exact": True,
            "auth_env_vars_blank": _blank_subscription_environment(),
        }
    )

    receipt = json.loads(overlay.receipt_path.read_text(encoding="utf-8"))
    assert receipt["chatgpt_subscription_activation_probe_verified"] is False
    assert receipt["chatgpt_subscription_auth_verified"] is False
    assert receipt["auth_verification_status"] == "failed"
    assert overlay.restore() == []


def test_keyring_login_can_be_verified_without_auth_json(tmp_path: Path) -> None:
    codex_home = Path(os.environ["CODEX_HOME"])
    (codex_home / "auth.json").unlink()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    overlay = install_controlled_codex_execpolicy(
        workspace_dir=workspace,
        run_dir=run_dir,
        allow_prefixes=(("python",),),
    )
    initial = json.loads(overlay.receipt_path.read_text(encoding="utf-8"))
    assert initial["host_auth_file_before"]["state"] == "absent"

    status = {
        "ok": True,
        "chatgpt_status_exact": True,
        "auth_env_vars_blank": _blank_subscription_environment(),
    }
    overlay.record_login_status(status)
    overlay.record_activation_probe(_activation_probe())
    overlay.record_post_login_status(status)
    assert overlay.restore() == []
    final = json.loads(overlay.receipt_path.read_text(encoding="utf-8"))
    assert final["host_auth_file_after"]["state"] == "absent"
    assert final["host_auth_identity_unchanged"] is None
    assert final["chatgpt_subscription_auth_verified"] is True


def test_subscription_receipt_allows_canonical_oauth_refresh(tmp_path: Path) -> None:
    codex_home = Path(os.environ["CODEX_HOME"])
    auth_path = codex_home / "auth.json"
    before = auth_path.read_bytes()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    overlay = install_controlled_codex_execpolicy(
        workspace_dir=workspace,
        run_dir=run_dir,
        allow_prefixes=(("python",),),
    )
    _bind_default_config(overlay)
    status = {
        "ok": True,
        "chatgpt_status_exact": True,
        "status_kind": "chatgpt",
        "auth_env_vars_blank": _blank_subscription_environment(),
    }
    overlay.record_login_status(status)
    overlay.record_activation_probe(_activation_probe())
    refreshed = json.dumps(
        {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": "refreshed-access",
                "refresh_token": "refreshed-refresh",
                "account_id": "test-account",
            },
        }
    ).encode("utf-8")
    auth_path.write_bytes(refreshed)
    overlay.record_post_login_status(status)

    assert overlay.restore() == []
    assert auth_path.read_bytes() == refreshed
    assert auth_path.read_bytes() != before
    receipt = json.loads(overlay.receipt_path.read_text(encoding="utf-8"))
    assert receipt["host_auth_identity_unchanged"] is False
    assert receipt["host_auth_cache_preserved"] is True
    assert verify_controlled_codex_execpolicy_receipt(overlay.receipt_path) == []
    receipt["auth_cache_copied"] = True
    receipt["receipt_sha256"] = codex_execpolicy_receipt_sha256(receipt)
    overlay.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert "codex_execpolicy_auth_cache_copied_invalid" in (
        verify_controlled_codex_execpolicy_receipt(overlay.receipt_path)
    )


def test_subscription_receipt_rejects_semantically_mismatched_config_contract(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    overlay = install_controlled_codex_execpolicy(
        workspace_dir=workspace,
        run_dir=run_dir,
        allow_prefixes=(("python",),),
    )
    _bind_default_config(overlay)
    status = {
        "ok": True,
        "chatgpt_status_exact": True,
        "status_kind": "chatgpt",
        "auth_env_vars_blank": _blank_subscription_environment(),
    }
    overlay.record_login_status(status)
    overlay.record_activation_probe(_activation_probe())
    overlay.record_post_login_status(status)
    assert overlay.restore() == []
    assert verify_controlled_codex_execpolicy_receipt(overlay.receipt_path) == []

    contract = json.loads(overlay.config_contract_path.read_text(encoding="utf-8"))
    suffix_length = len(CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES)
    preflight = list(contract["preflight_overrides"])
    preflight.insert(len(preflight) - suffix_length, "model_reasoning_effort=medium")
    contract["preflight_overrides"] = preflight
    contract["preflight_overrides_sha256"] = execpolicy_mod._canonical_json_sha256(preflight)
    contract["contract_sha256"] = execpolicy_mod._config_contract_sha256(contract)
    overlay.config_contract_path.write_text(
        json.dumps(contract, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    receipt = json.loads(overlay.receipt_path.read_text(encoding="utf-8"))
    receipt["controlled_config_contract_sha256"] = sha256(
        overlay.config_contract_path.read_bytes()
    ).hexdigest()
    receipt["receipt_sha256"] = codex_execpolicy_receipt_sha256(receipt)
    overlay.receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    assert "codex_execpolicy_config_contract_preflight_mission_mismatch" in (
        verify_controlled_codex_execpolicy_receipt(overlay.receipt_path)
    )


def test_probe_workspace_state_detects_ignored_or_untracked_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "tracked.txt").write_text("before\n", encoding="utf-8")
    before = capture_probe_workspace_state(workspace)

    ignored_like = workspace / ".tmp" / "probe-side-effect.txt"
    ignored_like.parent.mkdir()
    ignored_like.write_text("unexpected\n", encoding="utf-8")
    after = capture_probe_workspace_state(workspace)

    assert after["entry_count"] > before["entry_count"]
    assert after["manifest_sha256"] != before["manifest_sha256"]


def test_codex_project_trust_override_is_toml_safe_for_windows_path() -> None:
    override = codex_project_trust_override(Path(r"I:\code\workspace"))
    parsed = tomllib.loads(override)

    assert parsed["projects"][r"I:\code\workspace"]["trust_level"] == "trusted"
