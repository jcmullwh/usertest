from __future__ import annotations

import json
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from agent_adapters import (
    CODEX_CHATGPT_SUBSCRIPTION_BASE_URL,
    CODEX_OPENAI_SUBSCRIPTION_BASE_URL,
    CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS,
    CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES,
    codex_subscription_config_errors,
)

_CONTROLLED_RULES_NAME = "usertest-controlled.rules"

# Backward-compatible public name.  The controlled contract now covers both
# alternate credentials and ambient provider routing.
CONTROLLED_CODEX_AUTH_ENV_VARS: tuple[str, ...] = CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS

# These command-line overrides have higher precedence than both ignored user config
# and any trusted project config. They bind controlled research to the built-in OpenAI
# provider and the host's managed ChatGPT login, while disabling unrelated integrations.
CONTROLLED_CODEX_NON_ROUTING_CONFIG_OVERRIDES: tuple[str, ...] = (
    "notify=[]",
    "sandbox_workspace_write.writable_roots=[]",
    "mcp_servers={}",
    "features.apps=false",
    "features.browser_use=false",
    "features.browser_use_external=false",
    "features.computer_use=false",
    "features.hooks=false",
    "features.plugin_sharing=false",
    "features.plugins=false",
    "features.remote_plugin=false",
    "features.skill_mcp_dependency_install=false",
    "features.tool_call_mcp_elicitation=false",
)
CONTROLLED_CODEX_CONFIG_OVERRIDES: tuple[str, ...] = (
    *CONTROLLED_CODEX_NON_ROUTING_CONFIG_OVERRIDES,
    *CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES,
)

_ACTIVATION_SAFE_CONFIG_DELTA: tuple[str, ...] = ("model_reasoning_effort=low",)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def codex_execpolicy_receipt_sha256(receipt: dict[str, Any]) -> str:
    """Hash one controlled-execpolicy receipt without its self hash."""

    payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    return _canonical_json_sha256(payload)


def _write_execpolicy_receipt(path: Path, receipt: dict[str, Any]) -> None:
    receipt["receipt_sha256"] = codex_execpolicy_receipt_sha256(receipt)
    _write_json(path, receipt)


def _config_contract_sha256(contract: dict[str, Any]) -> str:
    return _canonical_json_sha256(
        {key: value for key, value in contract.items() if key != "contract_sha256"}
    )


def _write_config_contract(path: Path, contract: dict[str, Any]) -> None:
    contract["contract_sha256"] = _config_contract_sha256(contract)
    _write_json(path, contract)


def _config_contract_errors(contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if contract.get("contract_sha256") != _config_contract_sha256(contract):
        errors.append("codex_execpolicy_config_contract_hash_changed")
    exact_fields = {
        "schema_version": 2,
        "status": "bound",
        "user_config_ignored": True,
        "target_project_config_isolated": True,
        "canonical_route_overrides": list(CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES),
        "activation_safe_delta": list(_ACTIVATION_SAFE_CONFIG_DELTA),
    }
    for field, expected in exact_fields.items():
        if contract.get(field) != expected:
            errors.append(f"codex_execpolicy_config_contract_{field}_invalid")

    lists: dict[str, list[str]] = {}
    for name in ("preflight", "activation", "mission", "postcheck"):
        raw = contract.get(f"{name}_overrides")
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            errors.append(f"codex_execpolicy_config_contract_{name}_overrides_invalid")
            lists[name] = []
            continue
        lists[name] = list(raw)
        expected_sha = _canonical_json_sha256(raw)
        if contract.get(f"{name}_overrides_sha256") != expected_sha:
            errors.append(f"codex_execpolicy_config_contract_{name}_hash_changed")
        errors.extend(
            f"codex_execpolicy_config_contract_{name}:{error}"
            for error in codex_subscription_config_errors(raw)
        )

    mission = lists.get("mission", [])
    if lists.get("preflight") != mission:
        errors.append("codex_execpolicy_config_contract_preflight_mission_mismatch")
    if lists.get("postcheck") != mission:
        errors.append("codex_execpolicy_config_contract_postcheck_mission_mismatch")
    suffix_length = len(CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES)
    expected_activation = (
        [
            *mission[:-suffix_length],
            *_ACTIVATION_SAFE_CONFIG_DELTA,
            *CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES,
        ]
        if mission[-suffix_length:] == list(CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES)
        else []
    )
    if lists.get("activation") != expected_activation:
        errors.append("codex_execpolicy_config_contract_activation_delta_invalid")
    return list(dict.fromkeys(errors))


def controlled_codex_execpolicy_receipt_errors(
    receipt: dict[str, Any],
) -> list[str]:
    """Return subscription-provenance errors for a finalized controlled run.

    OAuth refreshes may legitimately change ``auth.json``.  This contract therefore
    proves that the canonical cache remained usable after execution without requiring
    byte identity before and after the run.
    """

    errors: list[str] = []
    if receipt.get("receipt_sha256") != codex_execpolicy_receipt_sha256(receipt):
        errors.append("codex_execpolicy_receipt_hash_changed")
    exact_fields = {
        "schema_version": 2,
        "mode": "runner_controlled_project_execpolicy",
        "configuration_mode": "host_codex_home_with_isolated_config",
        "forced_login_method": "chatgpt",
        "model_provider": "openai",
        "chatgpt_base_url": CODEX_CHATGPT_SUBSCRIPTION_BASE_URL,
        "openai_base_url": CODEX_OPENAI_SUBSCRIPTION_BASE_URL,
        "auth_mode": "shared_host_chatgpt_subscription_cache",
        "auth_verification_status": "verified",
        "restore_status": "restored",
    }
    for field, expected in exact_fields.items():
        if receipt.get(field) != expected:
            errors.append(f"codex_execpolicy_{field}_invalid")
    true_fields = (
        "host_user_config_ignored",
        "target_project_config_isolated",
        "chatgpt_subscription_login_status_verified",
        "chatgpt_subscription_activation_probe_verified",
        "chatgpt_subscription_post_login_status_verified",
        "chatgpt_subscription_auth_verified",
        "api_key_auth_environment_disabled",
        "host_auth_cache_preserved",
        "global_config_unchanged",
        "host_global_rules_unchanged",
    )
    for field in true_fields:
        if receipt.get(field) is not True:
            errors.append(f"codex_execpolicy_{field}_not_verified")
    false_fields = (
        "api_fallback_allowed",
        "auth_cache_copied",
        "auth_cache_deleted",
    )
    for field in false_fields:
        if receipt.get(field) is not False:
            errors.append(f"codex_execpolicy_{field}_invalid")

    controlled_vars = receipt.get("controlled_auth_env_vars")
    if controlled_vars != list(CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS):
        errors.append("codex_execpolicy_controlled_environment_incomplete")
    for status_field in ("login_status", "post_login_status"):
        status_raw = receipt.get(status_field)
        status = status_raw if isinstance(status_raw, dict) else {}
        if status.get("ok") is not True or status.get("chatgpt_status_exact") is not True:
            errors.append(f"codex_execpolicy_{status_field}_not_chatgpt")
        blank_raw = status.get("auth_env_vars_blank")
        blank = blank_raw if isinstance(blank_raw, dict) else {}
        if not all(blank.get(name) is True for name in CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS):
            errors.append(f"codex_execpolicy_{status_field}_environment_not_blank")

    activation_raw = receipt.get("activation_probe")
    activation = activation_raw if isinstance(activation_raw, dict) else {}
    if (
        activation.get("ok") is not True
        or activation.get("marker_seen") is not True
        or activation.get("workspace_unchanged") is not True
    ):
        errors.append("codex_execpolicy_activation_probe_invalid")

    overrides_raw = receipt.get("controlled_config_overrides")
    overrides = overrides_raw if isinstance(overrides_raw, list) else []
    if not isinstance(overrides_raw, list) or not all(isinstance(item, str) for item in overrides):
        errors.append("codex_execpolicy_controlled_overrides_invalid")
    else:
        errors.extend(codex_subscription_config_errors(overrides))

    config_contract_raw = receipt.get("controlled_config_contract_path")
    config_contract_path = (
        Path(config_contract_raw) if isinstance(config_contract_raw, str) else None
    )
    if config_contract_path is None or not config_contract_path.is_file():
        errors.append("codex_execpolicy_config_contract_missing")
    else:
        config_contract_bytes = config_contract_path.read_bytes()
        if (
            receipt.get("controlled_config_contract_sha256")
            != sha256(config_contract_bytes).hexdigest()
        ):
            errors.append("codex_execpolicy_config_contract_file_changed")
        try:
            config_contract = json.loads(config_contract_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            config_contract = None
        if not isinstance(config_contract, dict):
            errors.append("codex_execpolicy_config_contract_invalid")
        else:
            errors.extend(_config_contract_errors(config_contract))
            if config_contract.get("mission_overrides") != overrides:
                errors.append("codex_execpolicy_config_contract_mission_receipt_mismatch")

    host_home_raw = receipt.get("host_codex_home")
    host_auth_raw = receipt.get("host_auth_path")
    if not isinstance(host_home_raw, str) or not isinstance(host_auth_raw, str):
        errors.append("codex_execpolicy_host_cache_path_missing")
    else:
        if Path(host_auth_raw) != Path(host_home_raw) / "auth.json":
            errors.append("codex_execpolicy_host_cache_path_not_direct")

    restore_errors = receipt.get("restore_errors")
    if restore_errors != []:
        errors.append("codex_execpolicy_restore_errors_present")
    if receipt.get("target_rules_manifest_after_restore") != receipt.get(
        "target_rules_manifest_before"
    ):
        errors.append("codex_execpolicy_target_rules_not_restored")
    if receipt.get("target_config_manifest_after_restore") != receipt.get(
        "target_config_manifest_before"
    ):
        errors.append("codex_execpolicy_target_config_not_restored")
    return list(dict.fromkeys(errors))


def verify_controlled_codex_execpolicy_receipt(path: Path) -> list[str]:
    """Load and semantically verify one finalized controlled auth receipt."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"codex_execpolicy_receipt_unreadable:{type(exc).__name__}"]
    if not isinstance(raw, dict):
        return ["codex_execpolicy_receipt_not_object"]
    return controlled_codex_execpolicy_receipt_errors(raw)


def _validate_prefixes(prefixes: Sequence[Sequence[str]]) -> tuple[tuple[str, ...], ...]:
    normalized: list[tuple[str, ...]] = []
    for index, prefix in enumerate(prefixes):
        if not isinstance(prefix, (list, tuple)) or not prefix:
            raise ValueError(f"codex_execpolicy_prefix_invalid:{index}")
        tokens: list[str] = []
        for token_index, token in enumerate(prefix):
            if (
                not isinstance(token, str)
                or not token
                or token != token.strip()
                or any(ord(character) < 32 or ord(character) == 127 for character in token)
            ):
                raise ValueError(f"codex_execpolicy_prefix_token_invalid:{index}:{token_index}")
            tokens.append(token)
        normalized.append(tuple(tokens))
    return tuple(dict.fromkeys(normalized))


def _tree_manifest(path: Path) -> list[dict[str, Any]]:
    if not path.exists() and not path.is_symlink():
        return []
    if path.is_symlink():
        return [{"path": ".", "kind": "symlink", "target": os.readlink(path)}]
    if path.is_file():
        payload = path.read_bytes()
        return [
            {
                "path": ".",
                "kind": "file",
                "size_bytes": len(payload),
                "sha256": sha256(payload).hexdigest(),
            }
        ]
    rows: list[dict[str, Any]] = []
    for candidate in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
        relative = candidate.relative_to(path).as_posix()
        if candidate.is_symlink():
            rows.append({"path": relative, "kind": "symlink", "target": os.readlink(candidate)})
        elif candidate.is_file():
            payload = candidate.read_bytes()
            rows.append(
                {
                    "path": relative,
                    "kind": "file",
                    "size_bytes": len(payload),
                    "sha256": sha256(payload).hexdigest(),
                }
            )
        elif candidate.is_dir():
            rows.append({"path": relative, "kind": "directory"})
    return rows


def capture_probe_workspace_state(workspace_dir: Path) -> dict[str, Any]:
    """Return a lightweight whole-workspace state hash excluding Git internals."""

    workspace = workspace_dir.resolve()
    rows: list[dict[str, Any]] = []
    for candidate in sorted(workspace.rglob("*"), key=lambda item: item.as_posix()):
        relative = candidate.relative_to(workspace)
        if ".git" in relative.parts:
            continue
        row: dict[str, Any] = {"path": relative.as_posix()}
        try:
            stat = candidate.lstat()
        except OSError as exc:
            row.update({"kind": "unreadable", "error": type(exc).__name__})
        else:
            if candidate.is_symlink():
                row.update({"kind": "symlink", "target": os.readlink(candidate)})
            elif candidate.is_dir():
                row["kind"] = "directory"
            elif candidate.is_file():
                row.update(
                    {
                        "kind": "file",
                        "size_bytes": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                    }
                )
            else:
                row["kind"] = "other"
        rows.append(row)
    encoded = json.dumps(
        rows,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "entry_count": len(rows),
        "manifest_sha256": sha256(encoded).hexdigest(),
    }


def codex_project_trust_override(workspace_path: str | Path) -> str:
    """Return one TOML-safe Codex config override for the exact acquired workspace."""

    encoded = json.dumps(str(workspace_path), ensure_ascii=False)
    return f'projects.{encoded}.trust_level="trusted"'


def _chatgpt_auth_shape(payload: Any) -> dict[str, Any]:
    payload_dict = payload if isinstance(payload, dict) else {}
    tokens = payload_dict.get("tokens")
    token_dict = tokens if isinstance(tokens, dict) else {}
    required_tokens_present = {
        field: isinstance(token_dict.get(field), str) and bool(token_dict.get(field))
        for field in ("access_token", "refresh_token", "account_id")
    }
    shape = {
        "auth_mode": payload_dict.get("auth_mode"),
        "embedded_api_key_absent": payload_dict.get("OPENAI_API_KEY") is None,
        "required_tokens_present": required_tokens_present,
    }
    shape["valid_chatgpt_shape"] = (
        shape["auth_mode"] == "chatgpt"
        and shape["embedded_api_key_absent"] is True
        and all(required_tokens_present.values())
    )
    return shape


def _read_host_auth(path: Path) -> tuple[bytes, dict[str, Any]]:
    payload_bytes = path.read_bytes()
    payload = json.loads(payload_bytes.decode("utf-8"))
    return payload_bytes, _chatgpt_auth_shape(payload)


def _host_auth_file_attestation(path: Path) -> dict[str, Any]:
    """Describe an optional file credential cache without exposing credential values.

    Codex may store managed ChatGPT credentials in an OS keyring instead of ``auth.json``.
    Therefore absence or unreadability of this optional cache is diagnostic only; exact CLI
    login status and a model-backed probe are the authoritative checks.
    """

    if not path.exists():
        return {
            "state": "absent",
            "identity_sha256": None,
            "shape": None,
            "error_kind": None,
        }
    try:
        payload_bytes, shape = _read_host_auth(path)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "state": "unreadable",
            "identity_sha256": None,
            "shape": None,
            "error_kind": type(exc).__name__,
        }
    return {
        "state": "present",
        "identity_sha256": sha256(payload_bytes).hexdigest(),
        "shape": shape,
        "error_kind": None,
    }


@dataclass
class ControlledCodexExecpolicyOverlay:
    workspace_dir: Path
    receipt_path: Path
    rules_dir: Path
    backup_path: Path | None
    target_config_path: Path
    target_config_backup_path: Path | None
    config_contract_path: Path
    host_codex_home: Path
    host_auth_path: Path
    host_rules_dir: Path
    project_trust_override: str
    codex_dir_existed: bool
    expected_manifest: list[dict[str, Any]]
    receipt: dict[str, Any]
    restored: bool = False

    def bind_effective_config(
        self, mission_overrides: Sequence[str]
    ) -> tuple[list[str], list[str]]:
        """Bind the exact sanitized configuration used by every controlled Codex process."""

        mission = list(mission_overrides)
        route_errors = codex_subscription_config_errors(mission)
        if route_errors:
            raise ValueError("codex_execpolicy_mission_config_invalid:" + ",".join(route_errors))
        suffix_length = len(CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES)
        activation = [
            *mission[:-suffix_length],
            *_ACTIVATION_SAFE_CONFIG_DELTA,
            *CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES,
        ]
        contract: dict[str, Any] = {
            "schema_version": 2,
            "status": "bound",
            "user_config_ignored": True,
            "target_project_config_isolated": True,
            "canonical_route_overrides": list(CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES),
            "activation_safe_delta": list(_ACTIVATION_SAFE_CONFIG_DELTA),
            "preflight_overrides": mission,
            "preflight_overrides_sha256": _canonical_json_sha256(mission),
            "activation_overrides": activation,
            "activation_overrides_sha256": _canonical_json_sha256(activation),
            "mission_overrides": mission,
            "mission_overrides_sha256": _canonical_json_sha256(mission),
            "postcheck_overrides": mission,
            "postcheck_overrides_sha256": _canonical_json_sha256(mission),
        }
        _write_config_contract(self.config_contract_path, contract)
        contract_errors = _config_contract_errors(contract)
        if contract_errors:
            raise ValueError(
                "codex_execpolicy_config_contract_invalid:" + ",".join(contract_errors)
            )
        self.receipt.update(
            {
                "controlled_config_overrides": mission,
                "controlled_config_contract_path": str(self.config_contract_path.resolve()),
                "controlled_config_contract_sha256": sha256(
                    self.config_contract_path.read_bytes()
                ).hexdigest(),
                "controlled_config_contract_status": "bound",
            }
        )
        _write_execpolicy_receipt(self.receipt_path, self.receipt)
        return mission, activation

    def record_login_status(self, status: dict[str, Any]) -> None:
        """Record a redacted login-mode check without claiming token usability."""

        env_blank_raw = status.get("auth_env_vars_blank")
        env_blank = env_blank_raw if isinstance(env_blank_raw, dict) else {}
        auth_env_blank = all(env_blank.get(name) is True for name in CONTROLLED_CODEX_AUTH_ENV_VARS)
        login_verified = (
            status.get("ok") is True
            and status.get("chatgpt_status_exact") is True
            and auth_env_blank
        )
        self.receipt["login_status"] = status
        self.receipt["chatgpt_subscription_auth_declared"] = login_verified
        self.receipt["chatgpt_subscription_login_status_verified"] = login_verified
        self.receipt["api_key_auth_environment_disabled"] = auth_env_blank
        self.receipt["auth_verification_status"] = "pending" if login_verified else "failed"
        self.receipt["chatgpt_subscription_auth_verified"] = False
        _write_execpolicy_receipt(self.receipt_path, self.receipt)

    def record_activation_probe(self, probe: dict[str, Any]) -> None:
        """Bind the mission-free model-backed command-policy probe into the receipt."""

        raw_path_value = probe.get("raw_events_path")
        raw_path = Path(raw_path_value) if isinstance(raw_path_value, str) else None
        raw_sha = None
        if raw_path is not None and raw_path.is_file():
            raw_sha = sha256(raw_path.read_bytes()).hexdigest()
        activation_ok = probe.get("ok") is True
        self.receipt["activation_probe"] = {
            "ok": activation_ok,
            "marker_seen": probe.get("marker_seen") is True,
            "required_commands": list(probe.get("required_commands") or []),
            "required_commands_seen": list(probe.get("required_commands_seen") or []),
            "raw_events_path": str(raw_path.resolve()) if raw_path is not None else None,
            "raw_events_sha256": raw_sha,
            "reason": probe.get("reason"),
            "workspace_state_before": probe.get("workspace_state_before"),
            "workspace_state_after": probe.get("workspace_state_after"),
            "workspace_unchanged": probe.get("workspace_unchanged") is True,
        }
        self.receipt["chatgpt_subscription_activation_probe_verified"] = activation_ok
        ready_for_postcheck = (
            self.receipt.get("chatgpt_subscription_login_status_verified") is True and activation_ok
        )
        self.receipt["chatgpt_subscription_auth_verified"] = False
        self.receipt["auth_verification_status"] = (
            "pending_postcheck" if ready_for_postcheck else "failed"
        )
        _write_execpolicy_receipt(self.receipt_path, self.receipt)

    def record_post_login_status(self, status: dict[str, Any]) -> None:
        """Prove the shared host login remains ChatGPT-backed after agent execution."""

        env_blank_raw = status.get("auth_env_vars_blank")
        env_blank = env_blank_raw if isinstance(env_blank_raw, dict) else {}
        post_verified = (
            status.get("ok") is True
            and status.get("chatgpt_status_exact") is True
            and all(env_blank.get(name) is True for name in CONTROLLED_CODEX_AUTH_ENV_VARS)
        )
        self.receipt["post_login_status"] = status
        self.receipt["chatgpt_subscription_post_login_status_verified"] = post_verified
        verified = (
            self.receipt.get("chatgpt_subscription_login_status_verified") is True
            and self.receipt.get("chatgpt_subscription_activation_probe_verified") is True
            and post_verified
        )
        self.receipt["chatgpt_subscription_auth_verified"] = verified
        self.receipt["auth_verification_status"] = "verified" if verified else "failed"
        _write_execpolicy_receipt(self.receipt_path, self.receipt)

    def restore(self) -> list[str]:
        """Remove runner rules and restore target rules without touching host credentials."""

        if self.restored:
            return list(self.receipt.get("restore_errors") or [])
        errors: list[str] = []
        observed_manifest: list[dict[str, Any]] = []
        try:
            observed_manifest = _tree_manifest(self.rules_dir)
            if observed_manifest != self.expected_manifest:
                errors.append("codex_execpolicy_overlay_changed_during_agent_run")
        except OSError as exc:
            errors.append(f"codex_execpolicy_overlay_inspection_failed:{type(exc).__name__}:{exc}")

        observed_target_config_manifest: list[dict[str, Any]] = []
        unexpected_target_config_archive: str | None = None
        try:
            observed_target_config_manifest = _tree_manifest(self.target_config_path)
            if observed_target_config_manifest:
                errors.append("codex_execpolicy_target_config_changed_during_agent_run")
                unexpected_base_path = (
                    self.receipt_path.parent / "codex_execpolicy_unexpected_config.toml"
                )
                unexpected_path = unexpected_base_path
                collision_index = 0
                while unexpected_path.exists() or unexpected_path.is_symlink():
                    collision_index += 1
                    unexpected_path = unexpected_base_path.with_name(
                        f"{unexpected_base_path.stem}.{collision_index}"
                        f"{unexpected_base_path.suffix}"
                    )
                shutil.move(str(self.target_config_path), str(unexpected_path))
                unexpected_target_config_archive = str(unexpected_path.resolve())
        except OSError as exc:
            errors.append(
                f"codex_execpolicy_target_config_inspection_failed:{type(exc).__name__}:{exc}"
            )

        try:
            if self.rules_dir.is_symlink() or self.rules_dir.is_file():
                self.rules_dir.unlink(missing_ok=True)
            elif self.rules_dir.exists():
                shutil.rmtree(self.rules_dir)
            if self.backup_path is not None and self.backup_path.exists():
                self.rules_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(self.backup_path), str(self.rules_dir))
            if (
                not self.codex_dir_existed
                and self.rules_dir.parent.exists()
                and not any(self.rules_dir.parent.iterdir())
            ):
                self.rules_dir.parent.rmdir()
        except OSError as exc:
            errors.append(f"codex_execpolicy_overlay_restore_failed:{type(exc).__name__}:{exc}")

        try:
            if self.target_config_backup_path is not None:
                if self.target_config_path.exists() or self.target_config_path.is_symlink():
                    raise FileExistsError(str(self.target_config_path))
                self.target_config_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(
                    str(self.target_config_backup_path),
                    str(self.target_config_path),
                )
        except OSError as exc:
            errors.append(
                f"codex_execpolicy_target_config_restore_failed:{type(exc).__name__}:{exc}"
            )

        final_manifest: list[dict[str, Any]] = []
        try:
            final_manifest = _tree_manifest(self.rules_dir)
        except OSError as exc:
            errors.append(
                f"codex_execpolicy_target_rules_post_restore_unreadable:{type(exc).__name__}:{exc}"
            )
        expected_target_manifest = self.receipt.get("target_rules_manifest_before") or []
        if final_manifest != expected_target_manifest:
            errors.append("codex_execpolicy_target_rules_restore_mismatch")
        final_target_config_manifest: list[dict[str, Any]] = []
        try:
            final_target_config_manifest = _tree_manifest(self.target_config_path)
        except OSError as exc:
            errors.append(
                f"codex_execpolicy_target_config_post_restore_unreadable:{type(exc).__name__}:{exc}"
            )
        expected_target_config_manifest = self.receipt.get("target_config_manifest_before") or []
        if final_target_config_manifest != expected_target_config_manifest:
            errors.append("codex_execpolicy_target_config_restore_mismatch")

        host_auth_file_after = _host_auth_file_attestation(self.host_auth_path)

        global_config_raw = self.receipt.get("global_config_path")
        global_config_path = Path(global_config_raw) if isinstance(global_config_raw, str) else None
        global_config_sha_after = (
            sha256(global_config_path.read_bytes()).hexdigest()
            if global_config_path is not None and global_config_path.is_file()
            else None
        )
        host_rules_manifest_after: list[dict[str, Any]] = []
        try:
            host_rules_manifest_after = _tree_manifest(self.host_rules_dir)
        except OSError as exc:
            errors.append(f"codex_execpolicy_host_rules_post_run_unreadable:{type(exc).__name__}")

        host_auth_identity_before = self.receipt.get("host_auth_identity_sha256_before")
        host_auth_identity_after = host_auth_file_after.get("identity_sha256")
        host_auth_identity_unchanged = (
            host_auth_identity_after == host_auth_identity_before
            if isinstance(host_auth_identity_before, str)
            and isinstance(host_auth_identity_after, str)
            else None
        )
        self.restored = True
        self.receipt.update(
            {
                "overlay_manifest_after_agent": observed_manifest,
                "target_config_manifest_during_agent": observed_target_config_manifest,
                "unexpected_target_config_archive": unexpected_target_config_archive,
                "target_rules_manifest_after_restore": final_manifest,
                "target_config_manifest_after_restore": final_target_config_manifest,
                "host_auth_file_after": host_auth_file_after,
                "host_auth_identity_sha256_after": host_auth_file_after.get("identity_sha256"),
                "host_auth_shape_after": host_auth_file_after.get("shape"),
                "host_auth_identity_unchanged": host_auth_identity_unchanged,
                "host_auth_cache_preserved": self.receipt.get(
                    "chatgpt_subscription_post_login_status_verified"
                )
                is True,
                "global_config_sha256_after": global_config_sha_after,
                "global_config_unchanged": (
                    global_config_sha_after == self.receipt.get("global_config_sha256_before")
                ),
                "host_global_rules_manifest_after": host_rules_manifest_after,
                "host_global_rules_unchanged": (
                    host_rules_manifest_after
                    == self.receipt.get("host_global_rules_manifest_before")
                ),
                "restore_status": "restored" if not errors else "failed",
                "restore_errors": list(dict.fromkeys(errors)),
            }
        )
        _write_execpolicy_receipt(self.receipt_path, self.receipt)
        return list(dict.fromkeys(errors))


def install_controlled_codex_execpolicy(
    *,
    workspace_dir: Path,
    run_dir: Path,
    allow_prefixes: Sequence[Sequence[str]],
    agent_workspace_path: str | Path | None = None,
) -> ControlledCodexExecpolicyOverlay:
    """Install target rules while retaining the host's managed ChatGPT credential cache.

    User config is ignored by the caller, but auth intentionally continues to use the
    host ``CODEX_HOME`` so OAuth refreshes persist in the canonical cache. Existing target
        rules and target project config are restored byte-for-byte. Host global rules remain
        loaded and are attested before and after the run; they are never moved or modified here.
    """

    normalized = _validate_prefixes(allow_prefixes)
    if not normalized:
        raise ValueError("codex_execpolicy_prefixes_empty")
    workspace = workspace_dir.resolve()
    agent_workspace = str(agent_workspace_path or workspace)
    host_codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).resolve()
    codex_dir = workspace / ".codex"
    rules_dir = codex_dir / "rules"
    target_config_path = codex_dir / "config.toml"
    if codex_dir.is_symlink():
        raise ValueError("codex_execpolicy_codex_dir_symlink_forbidden")
    if rules_dir.is_symlink():
        raise ValueError("codex_execpolicy_rules_symlink_forbidden")
    if target_config_path.is_symlink():
        raise ValueError("codex_execpolicy_target_config_symlink_forbidden")
    if target_config_path.exists() and not target_config_path.is_file():
        raise ValueError("codex_execpolicy_target_config_not_file")
    if codex_dir.resolve() == host_codex_home:
        raise ValueError("codex_execpolicy_target_codex_dir_is_host_home")

    host_auth_path = host_codex_home / "auth.json"
    host_auth_file_before = _host_auth_file_attestation(host_auth_path)
    host_auth_sha_before = host_auth_file_before.get("identity_sha256")
    host_auth_shape = host_auth_file_before.get("shape")

    global_config_path = host_codex_home / "config.toml"
    global_config_sha_before = (
        sha256(global_config_path.read_bytes()).hexdigest()
        if global_config_path.is_file()
        else None
    )
    host_rules_dir = host_codex_home / "rules"
    host_rules_manifest_before = _tree_manifest(host_rules_dir)
    project_trust_override = codex_project_trust_override(agent_workspace)
    config_receipt_path = run_dir / "codex_execpolicy_config_overrides.json"
    _write_config_contract(
        config_receipt_path,
        {
            "schema_version": 2,
            "status": "pending",
            "user_config_ignored": True,
            "target_project_config_isolated": False,
        },
    )

    receipt_path = run_dir / "codex_execpolicy_overlay.json"
    backup_path: Path | None = None
    target_config_backup_path: Path | None = None
    target_manifest = _tree_manifest(rules_dir)
    target_config_manifest = _tree_manifest(target_config_path)
    codex_dir_existed = codex_dir.exists()
    rules_install_started = False
    try:
        if target_config_path.exists():
            target_config_backup_candidate = run_dir / "codex_execpolicy_target_config.toml"
            if (
                target_config_backup_candidate.exists()
                or target_config_backup_candidate.is_symlink()
            ):
                raise ValueError("codex_execpolicy_target_config_backup_already_exists")
            shutil.move(str(target_config_path), str(target_config_backup_candidate))
            target_config_backup_path = target_config_backup_candidate

        if rules_dir.exists():
            backup_candidate = run_dir / "codex_execpolicy_target_rules"
            if backup_candidate.exists() or backup_candidate.is_symlink():
                raise ValueError("codex_execpolicy_backup_already_exists")
            shutil.move(str(rules_dir), str(backup_candidate))
            backup_path = backup_candidate

        rules_install_started = True
        rules_dir.mkdir(parents=True, exist_ok=True)
        rules_path = rules_dir / _CONTROLLED_RULES_NAME
        rules_text = "".join(
            "prefix_rule(pattern="
            + json.dumps(list(prefix), ensure_ascii=False)
            + ', decision="allow")\n'
            for prefix in normalized
        )
        rules_path.write_text(rules_text, encoding="utf-8", newline="\n")
    except Exception:
        if rules_install_started:
            if rules_dir.is_symlink() or rules_dir.is_file():
                rules_dir.unlink(missing_ok=True)
            elif rules_dir.exists():
                shutil.rmtree(rules_dir, ignore_errors=True)
        if backup_path is not None and backup_path.exists():
            rules_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup_path), str(rules_dir))
        if target_config_backup_path is not None and target_config_backup_path.exists():
            target_config_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target_config_backup_path), str(target_config_path))
        raise
    expected_manifest = _tree_manifest(rules_dir)
    isolated_target_config_manifest = _tree_manifest(target_config_path)
    if isolated_target_config_manifest:
        if rules_dir.is_symlink() or rules_dir.is_file():
            rules_dir.unlink(missing_ok=True)
        elif rules_dir.exists():
            shutil.rmtree(rules_dir, ignore_errors=True)
        if backup_path is not None and backup_path.exists():
            rules_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup_path), str(rules_dir))
        if target_config_path.is_symlink() or target_config_path.is_file():
            target_config_path.unlink(missing_ok=True)
        elif target_config_path.exists():
            shutil.rmtree(target_config_path, ignore_errors=True)
        if target_config_backup_path is not None and target_config_backup_path.exists():
            target_config_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target_config_backup_path), str(target_config_path))
        raise ValueError("codex_execpolicy_target_config_isolation_failed")
    receipt: dict[str, Any] = {
        "schema_version": 2,
        "mode": "runner_controlled_project_execpolicy",
        "workspace_dir": str(workspace),
        "agent_workspace_path": agent_workspace,
        "allow_prefixes": [list(prefix) for prefix in normalized],
        "rules_sha256": sha256(rules_text.encode()).hexdigest(),
        "target_rules_manifest_before": target_manifest,
        "target_config_path": str(target_config_path.resolve()),
        "target_config_manifest_before": target_config_manifest,
        "target_config_manifest_while_isolated": isolated_target_config_manifest,
        "target_project_config_isolated": True,
        "overlay_manifest_before_agent": expected_manifest,
        "restore_status": "pending",
        "restore_errors": [],
        "configuration_mode": "host_codex_home_with_isolated_config",
        "host_codex_home": str(host_codex_home),
        "host_user_config_ignored": True,
        "global_rules_loaded": True,
        "host_global_rules_manifest_before": host_rules_manifest_before,
        "controlled_config_overrides_path": str(config_receipt_path.resolve()),
        "controlled_config_overrides": [],
        "controlled_config_contract_path": str(config_receipt_path.resolve()),
        "controlled_config_contract_sha256": sha256(config_receipt_path.read_bytes()).hexdigest(),
        "controlled_config_contract_status": "pending",
        "forced_login_method": "chatgpt",
        "model_provider": "openai",
        "chatgpt_base_url": CODEX_CHATGPT_SUBSCRIPTION_BASE_URL,
        "openai_base_url": CODEX_OPENAI_SUBSCRIPTION_BASE_URL,
        "auth_mode": "shared_host_chatgpt_subscription_cache",
        "api_fallback_allowed": False,
        "auth_cache_copied": False,
        "auth_cache_deleted": False,
        "host_auth_path": str(host_auth_path),
        "host_auth_file_before": host_auth_file_before,
        "host_auth_identity_sha256_before": host_auth_sha_before,
        "host_auth_shape_before": host_auth_shape,
        "chatgpt_subscription_auth_declared": False,
        "chatgpt_subscription_login_status_verified": False,
        "chatgpt_subscription_activation_probe_verified": False,
        "chatgpt_subscription_post_login_status_verified": False,
        "chatgpt_subscription_auth_verified": False,
        "auth_verification_status": "pending",
        "api_key_auth_environment_disabled": False,
        "controlled_auth_env_vars": list(CONTROLLED_CODEX_AUTH_ENV_VARS),
        "global_config_path": str(global_config_path),
        "global_config_sha256_before": global_config_sha_before,
    }
    _write_execpolicy_receipt(receipt_path, receipt)
    return ControlledCodexExecpolicyOverlay(
        workspace_dir=workspace,
        receipt_path=receipt_path,
        rules_dir=rules_dir,
        backup_path=backup_path,
        target_config_path=target_config_path,
        target_config_backup_path=target_config_backup_path,
        config_contract_path=config_receipt_path,
        host_codex_home=host_codex_home,
        host_auth_path=host_auth_path,
        host_rules_dir=host_rules_dir,
        project_trust_override=project_trust_override,
        codex_dir_existed=codex_dir_existed,
        expected_manifest=expected_manifest,
        receipt=receipt,
    )
