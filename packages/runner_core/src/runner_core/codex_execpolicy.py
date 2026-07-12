from __future__ import annotations

import json
import os
import shutil
import tomllib
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from difflib import SequenceMatcher
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
CONTROLLED_CODEX_WINDOWS_SANDBOX_CONFIG_OVERRIDE = 'windows.sandbox="unelevated"'
_CONTROLLED_CODEX_WINDOWS_SANDBOX_MODE = "unelevated"


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


def _effective_windows_sandbox_is_unelevated(overrides: Sequence[str]) -> bool:
    for raw in reversed(overrides):
        key, separator, _value = raw.partition("=")
        normalized_key = key.strip().lower().replace("-", "_")
        if separator and normalized_key == "windows.sandbox":
            return raw.strip() == CONTROLLED_CODEX_WINDOWS_SANDBOX_CONFIG_OVERRIDE
    return False


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
        "canonical_subscription_route_verified": True,
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
    platform_os_name = contract.get("platform_os_name")
    if platform_os_name not in {"nt", "posix"}:
        errors.append("codex_execpolicy_config_contract_platform_os_name_invalid")
    expected_windows_sandbox_mode = (
        _CONTROLLED_CODEX_WINDOWS_SANDBOX_MODE if platform_os_name == "nt" else "not_applicable"
    )
    if contract.get("native_windows_sandbox_mode") != expected_windows_sandbox_mode:
        errors.append("codex_execpolicy_config_contract_native_windows_sandbox_mode_invalid")
    expected_rules_mode = (
        "ignored_native_windows_sandbox" if platform_os_name == "nt" else "project_execpolicy"
    )
    if contract.get("controlled_rules_enforcement_mode") != expected_rules_mode:
        errors.append("codex_execpolicy_config_contract_rules_enforcement_mode_invalid")
    if contract.get("controlled_rules_ignored") is not (platform_os_name == "nt"):
        errors.append("codex_execpolicy_config_contract_rules_ignored_invalid")
    if contract.get("controlled_rules_written") is not (platform_os_name != "nt"):
        errors.append("codex_execpolicy_config_contract_rules_written_invalid")
    if platform_os_name == "nt" and not _effective_windows_sandbox_is_unelevated(mission):
        errors.append("codex_execpolicy_config_contract_windows_sandbox_not_unelevated")
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
    schema_version = receipt.get("schema_version")
    if schema_version not in {2, 3}:
        errors.append("codex_execpolicy_schema_version_invalid")
    exact_fields = {
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
    activation_probe_required = receipt.get("activation_probe_required") is not False
    true_fields = [
        "host_user_config_ignored",
        "target_project_config_isolated",
        "chatgpt_subscription_login_status_verified",
        "chatgpt_subscription_post_login_status_verified",
        "chatgpt_subscription_auth_verified",
        "api_key_auth_environment_disabled",
        "host_auth_cache_preserved",
        "host_global_rules_unchanged",
        "canonical_subscription_route_verified",
    ]
    if activation_probe_required:
        true_fields.extend(
            [
                "chatgpt_subscription_activation_probe_verified",
                "controlled_execution_mode_verified",
            ]
        )
    if schema_version == 2:
        true_fields.append("global_config_unchanged")
    for field in true_fields:
        if receipt.get(field) is not True:
            errors.append(f"codex_execpolicy_{field}_not_verified")
    if schema_version == 3:
        if receipt.get("runner_induced_project_trust_cleanup_verified") is not True:
            errors.append("codex_execpolicy_project_trust_cleanup_not_verified")
        canonical_project_path = receipt.get("canonical_project_trust_path")
        if not isinstance(canonical_project_path, str) or not canonical_project_path:
            errors.append("codex_execpolicy_canonical_project_trust_path_invalid")
        cleanup_raw = receipt.get("runner_induced_project_trust_cleanup")
        cleanup = cleanup_raw if isinstance(cleanup_raw, dict) else {}
        cleanup_status = cleanup.get("status")
        if cleanup.get("verified") is not True or cleanup_status not in {
            "not_present",
            "preexisting_preserved",
            "removed",
            "unchanged",
        }:
            errors.append("codex_execpolicy_project_trust_cleanup_invalid")
        if cleanup.get("canonical_project_path") != canonical_project_path:
            errors.append("codex_execpolicy_project_trust_cleanup_path_mismatch")
        if not isinstance(cleanup.get("entry_preexisting"), bool) or not isinstance(
            cleanup.get("unrelated_change_preserved"), bool
        ):
            errors.append("codex_execpolicy_project_trust_cleanup_disposition_invalid")
        if cleanup.get("entry_removed") is not (cleanup_status == "removed"):
            errors.append("codex_execpolicy_project_trust_cleanup_removed_invalid")
        if not isinstance(receipt.get("global_config_unchanged"), bool):
            errors.append("codex_execpolicy_global_config_change_attestation_invalid")
    platform_os_name = receipt.get("platform_os_name")
    if platform_os_name not in {"nt", "posix"}:
        errors.append("codex_execpolicy_platform_os_name_invalid")
    expected_windows_sandbox_mode = (
        _CONTROLLED_CODEX_WINDOWS_SANDBOX_MODE if platform_os_name == "nt" else "not_applicable"
    )
    if receipt.get("native_windows_sandbox_mode") != expected_windows_sandbox_mode:
        errors.append("codex_execpolicy_native_windows_sandbox_mode_invalid")
    expected_rules_mode = (
        "ignored_native_windows_sandbox" if platform_os_name == "nt" else "project_execpolicy"
    )
    if receipt.get("controlled_rules_enforcement_mode") != expected_rules_mode:
        errors.append("codex_execpolicy_rules_enforcement_mode_invalid")
    if receipt.get("controlled_rules_ignored") is not (platform_os_name == "nt"):
        errors.append("codex_execpolicy_rules_ignored_invalid")
    if receipt.get("controlled_rules_written") is not (platform_os_name != "nt"):
        errors.append("codex_execpolicy_rules_written_invalid")
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

    if activation_probe_required:
        activation_raw = receipt.get("activation_probe")
        activation = activation_raw if isinstance(activation_raw, dict) else {}
        if (
            activation.get("ok") is not True
            or activation.get("marker_seen") is not True
            or activation.get("workspace_unchanged") is not True
            or activation.get("controlled_execution_mode_verified") is not True
        ):
            errors.append("codex_execpolicy_activation_probe_invalid")
        if activation.get("rules_ignored_observed") is not (platform_os_name == "nt"):
            errors.append("codex_execpolicy_activation_rules_mode_invalid")
        if (
            platform_os_name == "nt"
            and activation.get("sandbox_mode_observed") != "workspace-write"
        ):
            errors.append("codex_execpolicy_activation_windows_sandbox_mode_invalid")

    overrides_raw = receipt.get("controlled_config_overrides")
    overrides = overrides_raw if isinstance(overrides_raw, list) else []
    if not isinstance(overrides_raw, list) or not all(isinstance(item, str) for item in overrides):
        errors.append("codex_execpolicy_controlled_overrides_invalid")
    else:
        errors.extend(codex_subscription_config_errors(overrides))
        if platform_os_name == "nt" and not _effective_windows_sandbox_is_unelevated(overrides):
            errors.append("codex_execpolicy_windows_sandbox_not_unelevated")
        if schema_version == 3:
            canonical_path = receipt.get("canonical_project_trust_path")
            expected_trust_override = (
                f'projects.{json.dumps(canonical_path, ensure_ascii=False)}.trust_level="trusted"'
                if isinstance(canonical_path, str) and canonical_path
                else None
            )
            if expected_trust_override not in overrides:
                errors.append("codex_execpolicy_canonical_project_trust_override_missing")

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


def _strip_windows_extended_path_prefix(path: str) -> str:
    """Match ``dunce::canonicalize`` spelling for Windows extended paths."""

    if path.casefold().startswith("\\\\?\\unc\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _canonical_codex_project_trust_path(workspace_path: str | Path) -> str:
    raw_path = str(workspace_path)
    # Codex canonicalizes the active project path with native Windows path
    # semantics before looking up its trust entry.  A case-preserving override
    # therefore misses the active-project key on Windows and Codex persists a
    # second, lower-cased trust entry in the host config even though the runner
    # supplied an explicit session override.  Match that lookup key so the
    # disposable research workspace is trusted for this invocation without
    # modifying the signed-in host CODEX_HOME.
    if os.name != "nt":
        return raw_path
    resolved = _strip_windows_extended_path_prefix(str(Path(raw_path).resolve()))
    return os.path.normcase(resolved)


def codex_project_trust_override(workspace_path: str | Path) -> str:
    """Return one TOML-safe Codex config override for the exact acquired workspace."""

    trust_path = _canonical_codex_project_trust_path(workspace_path)
    encoded = json.dumps(trust_path, ensure_ascii=False)
    return f'projects.{encoded}.trust_level="trusted"'


def _optional_file_bytes(path: Path) -> bytes | None:
    if path.is_symlink():
        raise ValueError("codex_execpolicy_global_config_symlink_forbidden")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("codex_execpolicy_global_config_not_file")
    return path.read_bytes()


def _toml_document(payload: bytes | None) -> dict[str, Any]:
    if payload is None or not payload:
        return {}
    parsed = tomllib.loads(payload.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("codex_execpolicy_global_config_not_mapping")
    return parsed


def _matching_project_trust_key(document: dict[str, Any], canonical_path: str) -> str | None:
    projects_raw = document.get("projects")
    if not isinstance(projects_raw, dict):
        return None
    if canonical_path in projects_raw:
        return canonical_path
    if os.name != "nt":
        return None
    for raw_key in projects_raw:
        if not isinstance(raw_key, str):
            continue
        normalized = os.path.normcase(_strip_windows_extended_path_prefix(raw_key))
        if normalized == canonical_path:
            return raw_key
    return None


@contextmanager
def _exclusive_config_stream(path: Path, *, allow_delete: bool) -> Any:
    """Open a config file so another writer cannot race the cleanup write."""

    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        desired_access = 0x80000000 | 0x40000000
        if allow_delete:
            desired_access |= 0x00010000
        handle = create_file(
            str(path),
            desired_access,
            0,
            None,
            3,
            0x00000080,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR)
        except Exception:
            kernel32.CloseHandle(handle)
            raise
        with os.fdopen(descriptor, "r+b", buffering=0) as stream:
            yield stream
        return

    import fcntl

    descriptor = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with os.fdopen(descriptor, "r+b", buffering=0, closefd=False) as stream:
            yield stream
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _mark_windows_file_for_delete(stream: Any) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("delete_file", wintypes.BOOL)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_information.restype = wintypes.BOOL
    disposition = FileDispositionInfo(True)
    handle = msvcrt.get_osfhandle(stream.fileno())
    if not set_information(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
        raise ctypes.WinError(ctypes.get_last_error())


def _atomic_restore_optional_file(
    path: Path,
    *,
    expected_current: bytes,
    replacement: bytes | None,
) -> None:
    """Rewrite only while holding an OS-level writer-exclusive file handle."""

    if path.is_symlink() or not path.is_file():
        raise RuntimeError("codex_execpolicy_global_config_not_lockable")
    with _exclusive_config_stream(path, allow_delete=replacement is None) as stream:
        stream.seek(0)
        if stream.read() != expected_current:
            raise RuntimeError("codex_execpolicy_global_config_changed_before_restore")
        stream.seek(0)
        if replacement is None and os.name == "nt":
            _mark_windows_file_for_delete(stream)
        else:
            payload = replacement if replacement is not None else b""
            stream.write(payload)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            if stream.read() != payload:
                raise RuntimeError("codex_execpolicy_global_config_restore_mismatch")
    if replacement is None and os.name == "nt":
        if path.exists() or path.is_symlink():
            raise RuntimeError("codex_execpolicy_global_config_remove_failed")
    elif replacement is not None and path.read_bytes() != replacement:
        raise RuntimeError("codex_execpolicy_global_config_restore_mismatch")


def _plain_project_trust_line_range(payload: bytes, canonical_path: str) -> tuple[int, int]:
    """Locate the exact two Codex-authored lines for one plain trust table."""

    lines = payload.splitlines(keepends=True)
    matches: list[tuple[int, int]] = []
    for index in range(len(lines) - 1):
        try:
            header = lines[index].decode("utf-8").strip()
            trust_line = lines[index + 1].decode("utf-8").strip()
        except UnicodeDecodeError:
            continue
        if (
            not header.startswith("[projects.")
            or not header.endswith("]")
            or trust_line != 'trust_level = "trusted"'
        ):
            continue
        try:
            candidate_document = _toml_document(b"".join(lines[index : index + 2]))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError):
            continue
        candidate_key = _matching_project_trust_key(candidate_document, canonical_path)
        candidate_projects = candidate_document.get("projects")
        if (
            candidate_key is not None
            and isinstance(candidate_projects, dict)
            and candidate_projects == {candidate_key: {"trust_level": "trusted"}}
        ):
            matches.append((index, index + 2))
    if len(matches) != 1:
        raise RuntimeError("codex_execpolicy_project_trust_text_not_unique")
    return matches[0]


def _restore_runner_induced_project_trust(
    *,
    path: Path,
    original: bytes | None,
    canonical_path: str,
) -> tuple[dict[str, Any], list[str]]:
    """Remove only Codex's exact auto-persisted trust table for this workspace."""

    receipt: dict[str, Any] = {
        "status": "failed",
        "canonical_project_path": canonical_path,
        "entry_preexisting": False,
        "entry_removed": False,
        "unrelated_change_preserved": False,
        "original_size_bytes": len(original) if original is not None else None,
        "observed_size_bytes": None,
    }
    try:
        original_document = _toml_document(original)
        original_key = _matching_project_trust_key(original_document, canonical_path)
        receipt["entry_preexisting"] = original_key is not None
        current = _optional_file_bytes(path)
        receipt["observed_size_bytes"] = len(current) if current is not None else None
        if current == original:
            receipt.update(
                {
                    "status": (
                        "preexisting_preserved" if original_key is not None else "unchanged"
                    ),
                    "verified": True,
                }
            )
            return receipt, []
        if current is None:
            receipt.update(
                {
                    "status": "not_present",
                    "unrelated_change_preserved": original is not None,
                    "verified": True,
                }
            )
            return receipt, []
        current_document = _toml_document(current)
        current_key = _matching_project_trust_key(current_document, canonical_path)
        if current_key is None:
            receipt.update(
                {
                    "status": "not_present",
                    "unrelated_change_preserved": current != original,
                    "verified": True,
                }
            )
            return receipt, []
        if original_key is not None:
            receipt.update(
                {
                    "status": "preexisting_preserved",
                    "unrelated_change_preserved": current != original,
                    "verified": True,
                }
            )
            return receipt, []
        projects_raw = current_document.get("projects")
        assert isinstance(projects_raw, dict)
        if projects_raw.get(current_key) != {"trust_level": "trusted"}:
            raise RuntimeError("codex_execpolicy_runner_project_trust_not_plain")

        current_lines = current.splitlines(keepends=True)
        trust_start, trust_end = _plain_project_trust_line_range(current, canonical_path)
        replacement = b"".join(current_lines[:trust_start] + current_lines[trust_end:])
        if original is None and not replacement.strip():
            replacement = None
        elif original is not None:
            changes = [
                opcode
                for opcode in SequenceMatcher(
                    None,
                    original.splitlines(keepends=True),
                    current_lines,
                    autojunk=False,
                ).get_opcodes()
                if opcode[0] != "equal"
            ]
            if len(changes) == 1 and changes[0][0] == "insert":
                _tag, _before_start, _before_end, inserted_start, inserted_end = changes[0]
                inserted_bytes = b"".join(current_lines[inserted_start:inserted_end])
                exact_candidate = b"".join(
                    current_lines[:inserted_start] + current_lines[inserted_end:]
                )
                inserted_material_lines = [
                    line.strip()
                    for line in inserted_bytes.decode("utf-8").splitlines()
                    if line.strip()
                ]
                if (
                    exact_candidate == original
                    and len(inserted_material_lines) == 2
                    and _matching_project_trust_key(_toml_document(inserted_bytes), canonical_path)
                    is not None
                ):
                    replacement = original

        _atomic_restore_optional_file(
            path,
            expected_current=current,
            replacement=replacement,
        )
        post_cleanup = _optional_file_bytes(path)
        post_cleanup_document = _toml_document(post_cleanup)
        if _matching_project_trust_key(post_cleanup_document, canonical_path) is not None:
            raise RuntimeError("codex_execpolicy_runner_project_trust_cleanup_mismatch")
        receipt.update(
            {
                "status": "removed",
                "entry_removed": True,
                "unrelated_change_preserved": post_cleanup != original,
                "verified": True,
            }
        )
        return receipt, []
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError, RuntimeError) as exc:
        receipt.update({"verified": False, "error_kind": type(exc).__name__})
        return receipt, [str(exc)]


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
    global_config_path: Path
    global_config_bytes_before: bytes | None
    canonical_project_trust_path: str
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
            "platform_os_name": os.name,
            "user_config_ignored": True,
            "target_project_config_isolated": True,
            "canonical_route_overrides": list(CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES),
            "canonical_subscription_route_verified": True,
            "native_windows_sandbox_mode": (
                _CONTROLLED_CODEX_WINDOWS_SANDBOX_MODE if os.name == "nt" else "not_applicable"
            ),
            "controlled_rules_enforcement_mode": (
                "ignored_native_windows_sandbox" if os.name == "nt" else "project_execpolicy"
            ),
            "controlled_rules_ignored": os.name == "nt",
            "controlled_rules_written": os.name != "nt",
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
                "canonical_subscription_route_verified": True,
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

        argv_raw = probe.get("argv")
        argv = list(argv_raw) if isinstance(argv_raw, list) else []
        rules_ignored_observed = "--ignore-rules" in argv
        sandbox_mode_observed: str | None = None
        if "--sandbox" in argv:
            sandbox_index = argv.index("--sandbox")
            if sandbox_index + 1 < len(argv) and isinstance(argv[sandbox_index + 1], str):
                sandbox_mode_observed = argv[sandbox_index + 1]
        native_windows = self.receipt.get("platform_os_name") == "nt"
        controlled_execution_mode_verified = rules_ignored_observed is native_windows and (
            not native_windows or sandbox_mode_observed == "workspace-write"
        )
        raw_path_value = probe.get("raw_events_path")
        raw_path = Path(raw_path_value) if isinstance(raw_path_value, str) else None
        raw_sha = None
        if raw_path is not None and raw_path.is_file():
            raw_sha = sha256(raw_path.read_bytes()).hexdigest()
        activation_ok = probe.get("ok") is True and controlled_execution_mode_verified
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
            "rules_ignored_observed": rules_ignored_observed,
            "sandbox_mode_observed": sandbox_mode_observed,
            "controlled_execution_mode_verified": controlled_execution_mode_verified,
        }
        self.receipt["controlled_execution_mode_verified"] = controlled_execution_mode_verified
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
        activation_verified = (
            self.receipt.get("chatgpt_subscription_activation_probe_verified") is True
            or self.receipt.get("activation_probe_required") is False
        )
        verified = (
            self.receipt.get("chatgpt_subscription_login_status_verified") is True
            and activation_verified
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

        project_trust_cleanup, project_trust_cleanup_errors = _restore_runner_induced_project_trust(
            path=self.global_config_path,
            original=self.global_config_bytes_before,
            canonical_path=self.canonical_project_trust_path,
        )
        errors.extend(project_trust_cleanup_errors)
        try:
            global_config_bytes_after = _optional_file_bytes(self.global_config_path)
        except (OSError, ValueError) as exc:
            errors.append(
                f"codex_execpolicy_global_config_post_restore_unreadable:{type(exc).__name__}"
            )
            global_config_bytes_after = None
        global_config_sha_after = (
            sha256(global_config_bytes_after).hexdigest()
            if global_config_bytes_after is not None
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
                "runner_induced_project_trust_cleanup": project_trust_cleanup,
                "runner_induced_project_trust_cleanup_verified": (
                    project_trust_cleanup.get("verified") is True
                ),
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
    activation_probe_required: bool = True,
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
    global_config_bytes_before = _optional_file_bytes(global_config_path)
    global_config_sha_before = (
        sha256(global_config_bytes_before).hexdigest()
        if global_config_bytes_before is not None
        else None
    )
    host_rules_dir = host_codex_home / "rules"
    host_rules_manifest_before = _tree_manifest(host_rules_dir)
    canonical_project_trust_path = _canonical_codex_project_trust_path(agent_workspace)
    project_trust_override = codex_project_trust_override(agent_workspace)
    config_receipt_path = run_dir / "codex_execpolicy_config_overrides.json"
    _write_config_contract(
        config_receipt_path,
        {
            "schema_version": 2,
            "status": "pending",
            "platform_os_name": os.name,
            "user_config_ignored": True,
            "target_project_config_isolated": False,
            "canonical_subscription_route_verified": False,
            "native_windows_sandbox_mode": (
                _CONTROLLED_CODEX_WINDOWS_SANDBOX_MODE if os.name == "nt" else "not_applicable"
            ),
            "controlled_rules_enforcement_mode": (
                "ignored_native_windows_sandbox" if os.name == "nt" else "project_execpolicy"
            ),
            "controlled_rules_ignored": os.name == "nt",
            "controlled_rules_written": os.name != "nt",
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

        rules_text = "".join(
            "prefix_rule(pattern="
            + json.dumps(list(prefix), ensure_ascii=False)
            + ', decision="allow")\n'
            for prefix in normalized
        )
        if os.name != "nt":
            rules_install_started = True
            rules_dir.mkdir(parents=True, exist_ok=True)
            rules_path = rules_dir / _CONTROLLED_RULES_NAME
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
        "schema_version": 3,
        "mode": "runner_controlled_project_execpolicy",
        "platform_os_name": os.name,
        "native_windows_sandbox_mode": (
            _CONTROLLED_CODEX_WINDOWS_SANDBOX_MODE if os.name == "nt" else "not_applicable"
        ),
        "controlled_rules_enforcement_mode": (
            "ignored_native_windows_sandbox" if os.name == "nt" else "project_execpolicy"
        ),
        "controlled_rules_ignored": os.name == "nt",
        "workspace_dir": str(workspace),
        "agent_workspace_path": agent_workspace,
        "allow_prefixes": [list(prefix) for prefix in normalized],
        "rules_sha256": (sha256(rules_text.encode()).hexdigest() if os.name != "nt" else None),
        "controlled_rules_written": os.name != "nt",
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
        "global_rules_loaded": os.name != "nt",
        "host_global_rules_manifest_before": host_rules_manifest_before,
        "controlled_config_overrides_path": str(config_receipt_path.resolve()),
        "controlled_config_overrides": [],
        "controlled_config_contract_path": str(config_receipt_path.resolve()),
        "controlled_config_contract_sha256": sha256(config_receipt_path.read_bytes()).hexdigest(),
        "controlled_config_contract_status": "pending",
        "canonical_subscription_route_verified": False,
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
        "activation_probe_required": bool(activation_probe_required),
        "chatgpt_subscription_login_status_verified": False,
        "chatgpt_subscription_activation_probe_verified": False,
        "chatgpt_subscription_post_login_status_verified": False,
        "chatgpt_subscription_auth_verified": False,
        "auth_verification_status": "pending",
        "api_key_auth_environment_disabled": False,
        "controlled_execution_mode_verified": False,
        "controlled_auth_env_vars": list(CONTROLLED_CODEX_AUTH_ENV_VARS),
        "global_config_path": str(global_config_path),
        "global_config_sha256_before": global_config_sha_before,
        "canonical_project_trust_path": canonical_project_trust_path,
        "runner_induced_project_trust_cleanup_verified": False,
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
        global_config_path=global_config_path,
        global_config_bytes_before=global_config_bytes_before,
        canonical_project_trust_path=canonical_project_trust_path,
        project_trust_override=project_trust_override,
        codex_dir_existed=codex_dir_existed,
        expected_manifest=expected_manifest,
        receipt=receipt,
    )
