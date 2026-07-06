#!/usr/bin/env python
# ruff: noqa: E501
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_WINDOWS_OFFLINE_FIRST_SUCCESS_CMD = (
    r"powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline_first_success.ps1"
)
_POSIX_OFFLINE_FIRST_SUCCESS_CMD = "bash ./scripts/offline_first_success.sh"
_SOURCE_RELATIVE_PATHS = (
    "apps/usertest/src",
    "apps/usertest_backlog/src",
    "apps/usertest_implement/src",
    "packages/runner_core/src",
    "packages/agent_adapters/src",
    "packages/normalized_events/src",
    "packages/reporter/src",
    "packages/sandbox_runner/src",
    "packages/triage_engine/src",
    "packages/backlog_core/src",
    "packages/backlog_miner/src",
    "packages/backlog_repo/src",
    "packages/token_monitoring/src",
    "packages/run_artifacts/src",
)


def _prefer_checkout_sources() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    if not (repo_root / "tools" / "scaffold" / "monorepo.toml").exists():
        return
    for rel_path in reversed(_SOURCE_RELATIVE_PATHS):
        source_path = (repo_root / rel_path).resolve()
        if not source_path.exists():
            continue
        source_path_s = str(source_path)
        if source_path_s in sys.path:
            sys.path.remove(source_path_s)
        sys.path.insert(0, source_path_s)


_prefer_checkout_sources()


def _one_command_first_success_remediation() -> str:
    return (
        "Quick fix (recommended): from repo root, run ONE of:\n"
        f"  - Windows PowerShell: `{_WINDOWS_OFFLINE_FIRST_SUCCESS_CMD}`\n"
        f"  - macOS/Linux: `{_POSIX_OFFLINE_FIRST_SUCCESS_CMD}`"
    )


def _missing_dependency_remediation(*, dependency: str, import_name: str) -> str:
    return (
        f"Missing dependency `{dependency}` (import name: `{import_name}`).\n"
        f"{_one_command_first_success_remediation()}\n"
        "Manual fix: `python -m pip install -r requirements-dev.txt`."
    )


try:
    import yaml
except ModuleNotFoundError as exc:
    raise SystemExit(
        _missing_dependency_remediation(dependency="pyyaml", import_name="yaml")
    ) from exc


def _from_source_import_remediation(*, missing_module: str) -> str:
    return (
        f"Missing import `{missing_module}`.\n"
        f"{_one_command_first_success_remediation()}\n"
        "Manual fix (from repo root): install deps + configure PYTHONPATH:\n"
        "  - macOS/Linux: `python -m pip install -r requirements-dev.txt && source scripts/set_pythonpath.sh`\n"
        "  - PowerShell: `python -m pip install -r requirements-dev.txt; . .\\scripts\\set_pythonpath.ps1`"
    )


def _is_missing_module(exc: ModuleNotFoundError, module: str) -> bool:
    name = getattr(exc, "name", None)
    if not name:
        return False
    return name == module or name.startswith(f"{module}.")


try:
    from runner_core import RunnerConfig, RunRequest, find_repo_root, run_once
    from runner_core.execution_backend import (
        _load_maintenance_docker_config,
        cleanup_local_maintenance_images,
        list_local_maintenance_images,
    )
except ModuleNotFoundError as exc:
    if _is_missing_module(exc, "runner_core"):
        raise SystemExit(_from_source_import_remediation(missing_module="runner_core")) from exc
    raise

try:
    from backlog_repo import (
        DISCARDED_PLAN_BUCKET,
        load_atom_actions_yaml,
        load_backlog_actions_yaml,
        reconcile_atom_actions_from_plan_folders,
        write_atom_actions_yaml,
        write_backlog_actions_yaml,
    )
except ModuleNotFoundError as exc:
    if _is_missing_module(exc, "backlog_repo"):
        raise SystemExit(_from_source_import_remediation(missing_module="backlog_repo")) from exc
    raise

try:
    from usertest_implement.batch_runner import add_batch_subcommands
    from usertest_implement.finalize import finalize_commit, finalize_push
    from usertest_implement.ledger import load_ledger, update_ledger_file
    from usertest_implement.model_detect import infer_observed_model
    from usertest_implement.summarize import iter_implementation_rows, write_jsonl
    from usertest_implement.tickets import (
        build_ticket_index,
        move_ticket_file,
        parse_ticket_markdown_metadata,
        select_next_ticket,
        select_next_ticket_path,
        strip_legacy_source_ticket_lines,
    )
except ModuleNotFoundError as exc:
    if _is_missing_module(exc, "usertest_implement"):
        raise SystemExit(_from_source_import_remediation(missing_module="usertest_implement")) from exc
    raise


@dataclass(frozen=True)
class SelectedTicket:
    fingerprint: str
    title: str | None
    export_kind: str | None
    stage: str | None
    owner_root: Path | None
    idea_path: Path | None
    ticket_markdown: str
    tickets_export_path: Path | None
    export_index: int | None


@dataclass(frozen=True)
class _SettingsValueSpec:
    kind: str
    choices: tuple[str, ...] = ()
    allow_none: bool = False


_SETTINGS_FILENAME = "usertest_implement_settings.yaml"
_DEFAULT_PERSONA_ID = "thoughtful_maintainer"
_DEFAULT_MISSION_ID = "implement_maintenance_backlog_ticket_v1"
_DEFAULT_REVIEW_PERSONA_ID = "compliance_sentinel"
_DEFAULT_REVIEW_MISSION_ID = "review_backlog_implementation_pr_v1"
_DEFAULT_LEDGER_PATH = Path(".agents/state/backlog_implement_actions.yaml")
_MAX_REVIEW_DIFF_CHARS = 120_000
_SETTINGS_SECTION_RUN_COMMON = "run_common"
_SETTINGS_SECTION_RUN = "run"
_SETTINGS_SECTION_TICKETS_RUN_NEXT = "tickets_run_next"
_SETTINGS_ALLOWED_SECTIONS = {
    _SETTINGS_SECTION_RUN_COMMON,
    _SETTINGS_SECTION_RUN,
    _SETTINGS_SECTION_TICKETS_RUN_NEXT,
}
_SETTINGS_COMMON_SPECS: dict[str, _SettingsValueSpec] = {
    "repo": _SettingsValueSpec("str", allow_none=True),
    "ref": _SettingsValueSpec("str", allow_none=True),
    "agent": _SettingsValueSpec("choice", choices=("claude", "codex", "gemini")),
    "model": _SettingsValueSpec("str", allow_none=True),
    "policy": _SettingsValueSpec("str"),
    "persona_id": _SettingsValueSpec("str", allow_none=True),
    "mission_id": _SettingsValueSpec("str"),
    "seed": _SettingsValueSpec("int"),
    "agent_config_override": _SettingsValueSpec("str_list"),
    "keep_workspace": _SettingsValueSpec("bool"),
    "exec_backend": _SettingsValueSpec("choice", choices=("docker", "local")),
    "exec_use_host_agent_login": _SettingsValueSpec("bool"),
    "exec_use_target_sandbox_cli_install": _SettingsValueSpec("bool"),
    "exec_docker_profile": _SettingsValueSpec(
        "choice",
        choices=("standard", "maintenance"),
        allow_none=True,
    ),
    "exec_keep_container": _SettingsValueSpec("bool"),
    "exec_cache": _SettingsValueSpec("choice", choices=("cold", "warm")),
    "exec_cache_dir": _SettingsValueSpec("path", allow_none=True),
    "maintenance_venv_cache": _SettingsValueSpec("bool"),
    "dry_run": _SettingsValueSpec("bool"),
    "verification_profile": _SettingsValueSpec(
        "choice",
        choices=("default_handoff", "none"),
    ),
    "verification_commands": _SettingsValueSpec("str_list"),
    "verification_timeout_seconds": _SettingsValueSpec("float", allow_none=True),
    "skip_verify": _SettingsValueSpec("bool"),
    "verify_reuse": _SettingsValueSpec("choice", choices=("auto", "off")),
    "implementation_review_agent": _SettingsValueSpec(
        "choice",
        choices=("claude", "codex", "gemini"),
        allow_none=True,
    ),
    "implementation_review_model": _SettingsValueSpec("str", allow_none=True),
    "ci_timeout_seconds": _SettingsValueSpec("float", allow_none=True),
    "skip_ci_wait": _SettingsValueSpec("bool"),
    "draft_pr_on_ci_failure": _SettingsValueSpec("bool"),
    "commit": _SettingsValueSpec("bool"),
    "branch": _SettingsValueSpec("str", allow_none=True),
    "commit_message": _SettingsValueSpec("str", allow_none=True),
    "git_user_name": _SettingsValueSpec("str", allow_none=True),
    "git_user_email": _SettingsValueSpec("str", allow_none=True),
    "push": _SettingsValueSpec("bool"),
    "remote_name": _SettingsValueSpec("str"),
    "remote_url": _SettingsValueSpec("str", allow_none=True),
    "force_push": _SettingsValueSpec("bool"),
    "base_branch": _SettingsValueSpec("str"),
    "pr": _SettingsValueSpec("bool"),
    "move_on_start": _SettingsValueSpec("bool"),
    "move_on_commit": _SettingsValueSpec("bool"),
    "ledger": _SettingsValueSpec("path", allow_none=True),
}
_SETTINGS_TICKETS_RUN_NEXT_SPECS: dict[str, _SettingsValueSpec] = {
    "owner_root": _SettingsValueSpec("path"),
    "bucket_priority": _SettingsValueSpec("str_list"),
    "kind_priority": _SettingsValueSpec("str_list"),
    "refresh_backlog": _SettingsValueSpec("bool"),
    "backlog_target": _SettingsValueSpec("str", allow_none=True),
    "backlog_runs_dir": _SettingsValueSpec("path", allow_none=True),
    "backlog_agent": _SettingsValueSpec(
        "choice",
        choices=("claude", "codex", "gemini"),
        allow_none=True,
    ),
    "backlog_model": _SettingsValueSpec("str", allow_none=True),
    "review_agent": _SettingsValueSpec(
        "choice",
        choices=("claude", "codex", "gemini"),
        allow_none=True,
    ),
    "review_model": _SettingsValueSpec("str", allow_none=True),
}
_SETTINGS_RUN_SPECS: dict[str, _SettingsValueSpec] = {}
_BOOLEAN_TRUE_VALUES = {"1", "true", "yes", "on"}
_BOOLEAN_FALSE_VALUES = {"0", "false", "no", "off"}


def _enable_console_backslashreplace(stream: Any) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        if str(getattr(stream, "errors", "")).lower() == "backslashreplace":
            return
        reconfigure(errors="backslashreplace")
    except Exception:
        return


def _configure_console_output() -> None:
    _enable_console_backslashreplace(sys.stdout)
    _enable_console_backslashreplace(sys.stderr)


_configure_console_output()


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_atom_actions_path(repo_root: Path) -> Path:
    return repo_root / "configs" / "backlog_atom_actions.yaml"


def _default_backlog_actions_path(repo_root: Path) -> Path:
    return repo_root / "configs" / "backlog_actions.yaml"


def _sync_ticket_atom_actions(
    *,
    repo_root: Path,
    owner_root: Path,
    atom_actions_path: Path | None = None,
    discard_fingerprint: str | None = None,
    discard_reason: str | None = None,
    discard_note: str | None = None,
    discarded_path: Path | None = None,
    discarded_at: str | None = None,
) -> dict[str, Any]:
    resolved_atom_actions_path = atom_actions_path or _default_atom_actions_path(repo_root)
    atom_actions = load_atom_actions_yaml(resolved_atom_actions_path)
    sync_at = discarded_at or _utc_now_z()
    meta = reconcile_atom_actions_from_plan_folders(
        atom_actions=atom_actions,
        owner_roots=[owner_root.resolve()],
        generated_at=sync_at,
    )
    if discard_fingerprint is not None:
        for entry in atom_actions.values():
            discarded_fingerprints = [
                item for item in entry.get("discarded_fingerprints", []) if isinstance(item, str)
            ]
            if discard_fingerprint not in discarded_fingerprints:
                continue
            entry["last_discard_reason"] = discard_reason
            entry["last_discarded_at"] = sync_at
            if discard_note:
                entry["last_discard_note"] = discard_note
            if discarded_path is not None:
                entry["last_discarded_path"] = str(discarded_path)
    write_atom_actions_yaml(resolved_atom_actions_path, atom_actions)
    return meta


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {path}, got {type(data).__name__}")
    return data


def _resolve_repo_root(repo_root: Path | None) -> Path:
    if repo_root is None:
        return find_repo_root()
    return repo_root.resolve()


def _load_runner_config(repo_root: Path) -> RunnerConfig:
    agents_cfg = _load_yaml(repo_root / "configs" / "agents.yaml").get("agents", {})
    policies_cfg = _load_yaml(repo_root / "configs" / "policies.yaml").get("policies", {})
    if not isinstance(agents_cfg, dict) or not isinstance(policies_cfg, dict):
        raise ValueError("Invalid configs under configs/.")
    return RunnerConfig(
        repo_root=repo_root,
        runs_dir=repo_root / "runs" / "usertest_implement",
        agents=agents_cfg,
        policies=policies_cfg,
    )


def _default_settings_path(repo_root: Path) -> Path:
    return repo_root / "configs" / _SETTINGS_FILENAME


def _resolve_settings_path(*, repo_root: Path, value: Path | None) -> Path | None:
    if value is None:
        candidate = _default_settings_path(repo_root)
        return candidate if candidate.exists() else None
    if value.is_absolute():
        return value.resolve()
    return (repo_root / value).resolve()


def _load_cli_settings_doc(path: Path) -> dict[str, Any]:
    doc = _load_yaml(path)
    version = doc.get("version", 1)
    if version != 1:
        raise ValueError(f"Unsupported settings version in {path}: {version!r}")
    profiles = doc.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError(f"Expected non-empty profiles mapping in {path}")
    for profile_name, profile_raw in profiles.items():
        if not isinstance(profile_name, str) or not profile_name.strip():
            raise ValueError(f"Settings profile names must be non-empty strings in {path}")
        if not isinstance(profile_raw, dict):
            raise ValueError(f"Profile {profile_name!r} must be a mapping in {path}")
        unknown_sections = set(profile_raw.keys()) - _SETTINGS_ALLOWED_SECTIONS
        if unknown_sections:
            raise ValueError(
                f"Profile {profile_name!r} has unknown sections {sorted(unknown_sections)!r} in {path}"
            )
        for section_name, section_raw in profile_raw.items():
            if not isinstance(section_raw, dict):
                raise ValueError(
                    f"Profile {profile_name!r} section {section_name!r} must be a mapping in {path}"
                )
    default_profile = doc.get("default_profile")
    if default_profile is not None:
        if not isinstance(default_profile, str) or not default_profile.strip():
            raise ValueError(f"default_profile must be a non-empty string in {path}")
        if default_profile not in profiles:
            raise ValueError(
                f"default_profile {default_profile!r} was not found in profiles for {path}"
            )
    return doc


def _coerce_settings_bool(*, key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _BOOLEAN_TRUE_VALUES:
            return True
        if lowered in _BOOLEAN_FALSE_VALUES:
            return False
    raise ValueError(f"Setting {key!r} must be a boolean value")


def _coerce_settings_value(
    *,
    key: str,
    value: Any,
    spec: _SettingsValueSpec,
    settings_path: Path,
    repo_root: Path,
) -> Any:
    if value is None:
        if spec.allow_none:
            return None
        raise ValueError(f"Setting {key!r} may not be null")

    if spec.kind == "str":
        if not isinstance(value, str):
            raise ValueError(f"Setting {key!r} must be a string")
        return value

    if spec.kind == "choice":
        if not isinstance(value, str):
            raise ValueError(f"Setting {key!r} must be a string")
        normalized = value.strip()
        if normalized not in spec.choices:
            raise ValueError(
                f"Setting {key!r} must be one of {sorted(spec.choices)!r}; got {normalized!r}"
            )
        return normalized

    if spec.kind == "bool":
        return _coerce_settings_bool(key=key, value=value)

    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"Setting {key!r} must be an integer")
        return value

    if spec.kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Setting {key!r} must be a number")
        return float(value)

    if spec.kind == "path":
        if not isinstance(value, str):
            raise ValueError(f"Setting {key!r} must be a filesystem path string")
        path = Path(value)
        if not path.is_absolute():
            path = (repo_root / path).resolve()
        else:
            path = path.resolve()
        return path

    if spec.kind == "str_list":
        if not isinstance(value, list):
            raise ValueError(f"Setting {key!r} must be a list of strings")
        out: list[str] = []
        for idx, item in enumerate(value):
            if not isinstance(item, str):
                raise ValueError(f"Setting {key!r}[{idx}] must be a string")
            out.append(item)
        return out

    raise ValueError(f"Unsupported settings spec kind for {key!r}: {spec.kind!r}")


def _settings_specs_for_args(args: argparse.Namespace) -> dict[str, _SettingsValueSpec]:
    specs = dict(_SETTINGS_COMMON_SPECS)
    if args.cmd == "run":
        specs.update(_SETTINGS_RUN_SPECS)
    elif args.cmd == "tickets" and getattr(args, "tickets_cmd", None) == "run-next":
        specs.update(_SETTINGS_TICKETS_RUN_NEXT_SPECS)
    return specs


def _settings_sections_for_args(args: argparse.Namespace) -> list[str]:
    if args.cmd == "run":
        return [_SETTINGS_SECTION_RUN_COMMON, _SETTINGS_SECTION_RUN]
    if args.cmd == "tickets" and getattr(args, "tickets_cmd", None) == "run-next":
        return [_SETTINGS_SECTION_RUN_COMMON, _SETTINGS_SECTION_TICKETS_RUN_NEXT]
    return []


def _collect_explicit_option_dests(
    parser: argparse.ArgumentParser,
    argv: list[str],
) -> set[str]:
    option_to_dest: dict[str, str] = {}

    def _walk(current: argparse.ArgumentParser) -> None:
        for action in current._actions:
            for opt in action.option_strings:
                option_to_dest[opt] = action.dest
            if isinstance(action, argparse._SubParsersAction):
                for subparser in action.choices.values():
                    _walk(subparser)

    _walk(parser)
    explicit: set[str] = set()
    for token in argv:
        if token == "--":
            break
        if not token.startswith("-"):
            continue
        option = token.split("=", 1)[0]
        dest = option_to_dest.get(option)
        if dest:
            explicit.add(dest)
    return explicit


def _normalize_settings_for_json(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [_normalize_settings_for_json(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _normalize_settings_for_json(item)
            for key, item in value.items()
        }
    return value


def _apply_cli_settings(
    *,
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    argv: list[str],
) -> dict[str, Any] | None:
    sections = _settings_sections_for_args(args)
    if not sections:
        return None

    repo_root = _resolve_repo_root(getattr(args, "repo_root", None))
    settings_path = _resolve_settings_path(repo_root=repo_root, value=getattr(args, "settings", None))
    settings_profile = getattr(args, "settings_profile", None)

    if settings_path is None:
        if settings_profile:
            raise SystemExit(
                f"--settings-profile requires a settings file; default path not found under {repo_root}."
            )
        info = {"config_path": None, "profile": None, "applied": {}, "auto_loaded": False}
        args._settings_info = info
        return info

    if not settings_path.exists():
        raise SystemExit(f"Settings file not found: {settings_path}")

    try:
        settings_doc = _load_cli_settings_doc(settings_path)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    profiles_raw = settings_doc["profiles"]
    profile_name = (
        str(settings_profile).strip()
        if isinstance(settings_profile, str) and settings_profile.strip()
        else str(settings_doc.get("default_profile") or "default").strip()
    )
    if profile_name not in profiles_raw:
        raise SystemExit(
            f"Settings profile {profile_name!r} not found in {settings_path}"
        )

    profile = profiles_raw[profile_name]
    merged: dict[str, Any] = {}
    for section_name in sections:
        section = profile.get(section_name, {})
        if not isinstance(section, dict):
            raise SystemExit(
                f"Settings profile {profile_name!r} section {section_name!r} must be a mapping"
            )
        merged.update(section)

    specs = _settings_specs_for_args(args)
    unknown_keys = set(merged.keys()) - set(specs.keys())
    if unknown_keys:
        raise SystemExit(
            f"Settings profile {profile_name!r} contains unsupported keys for this command: "
            f"{sorted(unknown_keys)!r}"
        )

    explicit_dests = _collect_explicit_option_dests(parser, argv)
    applied: dict[str, Any] = {}
    for key, raw_value in merged.items():
        if key in explicit_dests:
            continue
        coerced = _coerce_settings_value(
            key=key,
            value=raw_value,
            spec=specs[key],
            settings_path=settings_path,
            repo_root=repo_root,
        )
        setattr(args, key, coerced)
        applied[key] = _normalize_settings_for_json(coerced)

    info = {
        "config_path": str(settings_path),
        "profile": profile_name,
        "applied": applied,
        "auto_loaded": getattr(args, "settings", None) is None,
    }
    args._settings_info = info
    return info


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _optional_timeout_seconds(value: Any) -> float | None:
    if value is None:
        return None
    timeout_seconds = float(value)
    if timeout_seconds <= 0:
        return None
    return timeout_seconds


def _ci_timeout_seconds_arg(value: Any) -> float | None:
    return _optional_timeout_seconds(value)


def _resolve_ledger_path(*, repo_root: Path, raw: Path | None) -> Path:
    ledger_path = raw if raw is not None else _DEFAULT_LEDGER_PATH
    if ledger_path.is_absolute():
        return ledger_path.resolve()
    return (repo_root / ledger_path).resolve()


def _git_head_sha(workspace_dir: Path) -> str | None:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(workspace_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha if sha else None


def _wait_for_ci_success(
    *,
    run_dir: Path,
    workspace_dir: Path,
    branch: str,
    head_sha: str,
    workflow: str,
    timeout_seconds: float | None,
) -> dict[str, Any]:
    """
    Wait for GitHub Actions CI to pass for the current branch HEAD before opening a PR.

    This relies on CI being triggered for `push` events on the branch.
    """

    started_utc = _utc_now_z()
    started_monotonic = time.monotonic()
    summary: dict[str, Any] = {
        "schema_version": 1,
        "workflow": workflow,
        "branch": branch,
        "head_sha": head_sha,
        "run_id": None,
        "run_url": None,
        "status": None,
        "conclusion": None,
        "passed": False,
        "error": None,
        "started_at_utc": started_utc,
        "finished_at_utc": None,
        "timeout_seconds": timeout_seconds,
    }

    def _gh_json(argv: list[str]) -> Any:
        return _run_gh_json(cwd=workspace_dir, argv=argv)

    def _pick_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
        matches = [
            r
            for r in runs
            if isinstance(r, dict) and r.get("headSha") == head_sha and r.get("event") == "push"
        ]
        if not matches:
            matches = [
                r for r in runs if isinstance(r, dict) and r.get("headSha") == head_sha
            ]
        if not matches:
            return None
        matches.sort(key=lambda r: str(r.get("createdAt") or ""), reverse=True)
        return matches[0]

    run_id: int | None = None
    poll_interval_seconds = 5.0
    limit = 50
    while True:
        elapsed = time.monotonic() - started_monotonic
        if timeout_seconds is not None and elapsed > timeout_seconds:
            summary["error"] = (
                f"Timed out waiting to find a GitHub Actions run for {workflow} "
                f"(branch={branch}, head_sha={head_sha})."
            )
            summary["finished_at_utc"] = _utc_now_z()
            _write_json(run_dir / "ci_gate.json", summary)
            return summary

        try:
            runs_raw = _gh_json(
                [
                    "gh",
                    "run",
                    "list",
                    "--workflow",
                    workflow,
                    "--branch",
                    branch,
                    "--limit",
                    str(limit),
                    "--json",
                    "databaseId,headSha,event,status,conclusion,createdAt,url",
                ]
            )
        except Exception as e:  # noqa: BLE001
            summary["error"] = f"Failed to list GitHub Actions runs: {e}"
            summary["finished_at_utc"] = _utc_now_z()
            _write_json(run_dir / "ci_gate.json", summary)
            return summary

        runs_list = runs_raw if isinstance(runs_raw, list) else []
        picked = _pick_run([r for r in runs_list if isinstance(r, dict)])
        if picked is not None:
            run_id_raw = picked.get("databaseId")
            run_id_parsed: int | None = None
            if isinstance(run_id_raw, int):
                run_id_parsed = run_id_raw
            elif isinstance(run_id_raw, str) and run_id_raw.strip().isdigit():
                run_id_parsed = int(run_id_raw.strip())

            if run_id_parsed is not None:
                run_id = run_id_parsed
                summary["run_id"] = run_id
                summary["run_url"] = picked.get("url")
                summary["status"] = picked.get("status")
                summary["conclusion"] = picked.get("conclusion")
                _write_json(run_dir / "ci_gate.json", summary)
                break

        time.sleep(poll_interval_seconds)

    assert run_id is not None

    poll_interval_seconds = 10.0
    while True:
        elapsed = time.monotonic() - started_monotonic
        if timeout_seconds is not None and elapsed > timeout_seconds:
            summary["error"] = f"Timed out waiting for GitHub Actions run {run_id} to complete."
            summary["finished_at_utc"] = _utc_now_z()
            _write_json(run_dir / "ci_gate.json", summary)
            return summary

        try:
            view_raw = _gh_json(
                [
                    "gh",
                    "run",
                    "view",
                    str(run_id),
                    "--json",
                    "status,conclusion,url,headSha,event,createdAt,updatedAt",
                ]
            )
        except Exception as e:  # noqa: BLE001
            summary["error"] = f"Failed to inspect GitHub Actions run {run_id}: {e}"
            summary["finished_at_utc"] = _utc_now_z()
            _write_json(run_dir / "ci_gate.json", summary)
            return summary

        if isinstance(view_raw, dict):
            summary["status"] = view_raw.get("status")
            summary["conclusion"] = view_raw.get("conclusion")
            summary["run_url"] = view_raw.get("url") or summary.get("run_url")
            _write_json(run_dir / "ci_gate.json", summary)

        status = str(summary.get("status") or "").strip().lower()
        conclusion = str(summary.get("conclusion") or "").strip().lower()
        if status == "completed":
            passed = conclusion == "success"
            summary["passed"] = passed
            if not passed:
                summary["error"] = (
                    f"GitHub Actions CI did not pass (run_id={run_id}, "
                    f"conclusion={summary.get('conclusion')!r})."
                )
            summary["finished_at_utc"] = _utc_now_z()
            _write_json(run_dir / "ci_gate.json", summary)
            return summary

        time.sleep(poll_interval_seconds)


def _looks_like_local_path(value: str) -> bool:
    raw = value.strip()
    if not raw:
        return False
    if raw.startswith(("http://", "https://", "git@")):
        return False
    if raw.startswith(("pip:", "pdm:")):
        return False
    if re.match(r"^[A-Za-z]:[\\/]", raw):
        return True
    if raw.startswith(("\\\\", "/", "./", "../", ".\\", "..\\", "~")):
        return True
    return ("\\" in raw) or ("/" in raw)


def _infer_git_root(path: Path) -> Path | None:
    cur = path.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _git_remote_url(*, repo_dir: Path, remote_name: str) -> str | None:
    remote = remote_name.strip() or "origin"
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "remote", "get-url", remote],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return out if out else None


def _normalize_repo_identity(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    cleaned = cleaned.rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    return cleaned.lower()


def _maintenance_profile_is_eligible(*, repo_root: Path, repo_input: str) -> bool:
    current_git_root = _infer_git_root(repo_root) or repo_root.resolve()
    if _looks_like_local_path(repo_input):
        try:
            candidate = Path(repo_input).expanduser()
        except OSError:
            candidate = None
        if candidate is not None:
            target_git_root = _infer_git_root(candidate)
            if target_git_root is not None and target_git_root.resolve() == current_git_root.resolve():
                return True
    current_origin = _normalize_repo_identity(
        _git_remote_url(repo_dir=current_git_root, remote_name="origin")
    )
    requested_origin = _normalize_repo_identity(repo_input)
    return bool(current_origin is not None and requested_origin == current_origin)


def _resolve_exec_docker_profile(
    *,
    exec_backend: str,
    requested_profile: str | None,
    maintenance_eligible: bool,
) -> str:
    backend = exec_backend.strip().lower()
    if requested_profile is not None and requested_profile.strip():
        profile = requested_profile.strip().lower()
        if profile not in {"standard", "maintenance"}:
            raise SystemExit(f"Unsupported --exec-docker-profile: {requested_profile!r}")
        if backend != "docker":
            raise SystemExit("--exec-docker-profile requires --exec-backend docker.")
        if profile == "maintenance" and not maintenance_eligible:
            raise SystemExit(
                "exec_docker_profile='maintenance' is only valid for same-repo maintenance targets."
            )
        return profile
    if backend != "docker":
        return "standard"
    return "maintenance" if maintenance_eligible else "standard"


def _default_backlog_runs_dir(repo_root: Path) -> Path:
    return repo_root / "runs" / "usertest"


def _list_backlog_targets(runs_dir: Path) -> list[str]:
    if not runs_dir.exists():
        return []
    if not runs_dir.is_dir():
        return []
    slugs: list[str] = []
    for child in runs_dir.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if not name or name.startswith("_"):
            continue
        slugs.append(name)
    slugs.sort()
    return slugs


def _resolve_backlog_target(*, runs_dir: Path, target: str | None) -> str:
    if isinstance(target, str) and target.strip():
        return target.strip()
    candidates = _list_backlog_targets(runs_dir)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise SystemExit(
            "Unable to infer --backlog-target because there are no target directories under "
            f"{runs_dir}. Provide --backlog-target or --no-refresh-backlog."
        )
    raise SystemExit(
        "Unable to infer --backlog-target because multiple targets exist under "
        f"{runs_dir}: {', '.join(candidates)}. Provide --backlog-target or --no-refresh-backlog."
    )


def _run_workflow_step(argv: list[str], *, cwd: Path, label: str) -> None:
    cmd = " ".join(argv)
    print(f"[workflow] {label}: {cmd}", file=sys.stderr)
    proc = subprocess.run(argv, cwd=str(cwd), check=False)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def _refresh_backlog_for_ticket_implementation(
    *,
    args: argparse.Namespace,
    repo_root: Path,
) -> None:
    runs_dir = (
        args.backlog_runs_dir.resolve()
        if args.backlog_runs_dir is not None
        else _default_backlog_runs_dir(repo_root)
    )
    target = _resolve_backlog_target(runs_dir=runs_dir, target=args.backlog_target)

    backlog_agent = str(args.backlog_agent) if args.backlog_agent else "claude"
    backlog_model = (
        str(args.backlog_model).strip()
        if isinstance(args.backlog_model, str) and args.backlog_model.strip()
        else None
    )
    review_agent = (
        str(args.review_agent).strip()
        if isinstance(args.review_agent, str) and args.review_agent.strip()
        else backlog_agent
    )
    review_model = (
        str(args.review_model).strip()
        if isinstance(args.review_model, str) and args.review_model.strip()
        else None
    )

    base = [sys.executable, "-m", "usertest_backlog.cli"]
    common = ["--repo-root", str(repo_root), "--runs-dir", str(runs_dir), "--target", target]

    backlog_cmd = base + ["reports", "backlog", *common, "--agent", backlog_agent]
    if backlog_model is not None:
        backlog_cmd.extend(["--model", backlog_model])
    _run_workflow_step(backlog_cmd, cwd=repo_root, label="reports backlog")

    intent_cmd = base + ["reports", "intent-snapshot", *common]
    _run_workflow_step(intent_cmd, cwd=repo_root, label="reports intent-snapshot")

    review_cmd = base + ["reports", "review-ux", *common, "--agent", review_agent]
    if review_model is not None:
        review_cmd.extend(["--model", review_model])
    _run_workflow_step(review_cmd, cwd=repo_root, label="reports review-ux")

    export_cmd = base + ["reports", "export-tickets", *common]
    export_cmd.extend(["--stage", "ready_for_ticket"])
    _run_workflow_step(export_cmd, cwd=repo_root, label="reports export-tickets")


def _fingerprint_from_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _select_ticket_from_export(
    *,
    tickets_export_path: Path,
    fingerprint: str,
) -> SelectedTicket:
    doc = json.loads(tickets_export_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("tickets export must be a JSON object")
    exports_raw = doc.get("exports")
    exports = [e for e in exports_raw if isinstance(e, dict)] if isinstance(exports_raw, list) else []
    if not exports:
        raise ValueError("tickets export has no exports")

    matches: list[tuple[int, dict[str, Any]]] = []
    for idx, export in enumerate(exports):
        export_fp = export.get("fingerprint")
        export_fp_s = export_fp if isinstance(export_fp, str) else None
        if export_fp_s == fingerprint:
            matches.append((idx, export))

    if not matches:
        raise ValueError("No matching export found for the provided selector")
    if len(matches) > 1:
        raise ValueError(f"Selector matched multiple exports: {len(matches)}")

    export_index, export = matches[0]
    export_fp = export.get("fingerprint")
    if not isinstance(export_fp, str) or not export_fp.strip():
        raise ValueError("Export missing fingerprint")

    title = export.get("title")
    title_s = title.strip() if isinstance(title, str) and title.strip() else None
    export_kind = export.get("export_kind")
    export_kind_s = export_kind.strip() if isinstance(export_kind, str) and export_kind.strip() else None
    source_ticket_raw = export.get("source_ticket")
    source_ticket = source_ticket_raw if isinstance(source_ticket_raw, dict) else {}
    stage_raw = source_ticket.get("stage")
    stage_s = stage_raw.strip() if isinstance(stage_raw, str) and stage_raw.strip() else None

    owner_repo = export.get("owner_repo")
    owner_root: Path | None = None
    idea_path: Path | None = None
    if isinstance(owner_repo, dict):
        root_raw = owner_repo.get("root")
        if isinstance(root_raw, str) and root_raw.strip():
            owner_root = Path(root_raw)
        idea_raw = owner_repo.get("idea_path")
        if isinstance(idea_raw, str) and idea_raw.strip():
            idea_path = Path(idea_raw)

    body = export.get("body_markdown")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Export missing body_markdown")
    body = strip_legacy_source_ticket_lines(body)

    return SelectedTicket(
        fingerprint=export_fp.strip(),
        title=title_s,
        export_kind=export_kind_s,
        stage=stage_s,
        owner_root=owner_root,
        idea_path=idea_path,
        ticket_markdown=body,
        tickets_export_path=tickets_export_path,
        export_index=export_index,
    )


def _select_ticket_from_path(ticket_path: Path) -> SelectedTicket:
    text = ticket_path.read_text(encoding="utf-8", errors="replace")
    text = strip_legacy_source_ticket_lines(text)
    meta = parse_ticket_markdown_metadata(text)
    fingerprint = meta.get("fingerprint") or _fingerprint_from_text(text)
    title = meta.get("title")
    export_kind = meta.get("export_kind")
    stage = meta.get("stage")

    owner_root: Path | None = None
    try:
        resolved = ticket_path.resolve()
        parts_lower = [p.lower() for p in resolved.parts]
        if ".agents" in parts_lower:
            idx = parts_lower.index(".agents")
            owner_root = Path(*resolved.parts[:idx])
    except Exception:
        owner_root = None

    return SelectedTicket(
        fingerprint=fingerprint,
        title=title,
        export_kind=export_kind,
        stage=stage,
        owner_root=owner_root,
        idea_path=ticket_path,
        ticket_markdown=text,
        tickets_export_path=None,
        export_index=None,
    )


def _select_ticket_from_owner_root(
    *,
    owner_root: Path,
    fingerprint: str,
) -> SelectedTicket:
    index = build_ticket_index(owner_root=owner_root)
    entry = index.get(fingerprint)
    if entry is None or not entry.paths:
        raise ValueError(f"Unknown fingerprint under {owner_root}: {fingerprint}")
    path = sorted(entry.paths, key=lambda item: str(item))[0]
    return _select_ticket_from_path(path)


def _select_review_ticket(
    *,
    owner_root: Path,
    ticket_path: Path | None,
    fingerprint: str | None,
) -> SelectedTicket:
    if ticket_path is not None:
        return _select_ticket_from_path(ticket_path)
    if isinstance(fingerprint, str) and fingerprint.strip():
        return _select_ticket_from_owner_root(owner_root=owner_root, fingerprint=fingerprint.strip())
    raise SystemExit("Provide either --ticket-path or --fingerprint.")


def _compose_ticket_blob(selected: SelectedTicket) -> str:
    lines: list[str] = []
    lines.append("# Ticket context")
    lines.append(f"- fingerprint: {selected.fingerprint}")
    if selected.title is not None:
        lines.append(f"- title: {selected.title}")
    if selected.export_kind is not None:
        lines.append(f"- export_kind: {selected.export_kind}")
    if selected.stage is not None:
        lines.append(f"- stage: {selected.stage}")
    if selected.owner_root is not None:
        lines.append(f"- owner_repo_root: {selected.owner_root}")
    if selected.tickets_export_path is not None:
        lines.append(f"- tickets_export_path: {selected.tickets_export_path}")
    if selected.export_index is not None:
        lines.append(f"- export_index: {selected.export_index}")
    lines.append("")
    lines.append("# Ticket markdown")
    lines.append(selected.ticket_markdown.rstrip())
    lines.append("")
    return "\n".join(lines)


def _default_branch_name(selected: SelectedTicket) -> str:
    fp_part = selected.fingerprint[:12].lower()
    return f"backlog/{fp_part}"


def _resolve_remote_url_for_push(
    *,
    remote_name: str,
    remote_url: str | None,
    candidate_repo_dirs: list[Path],
) -> str | None:
    if isinstance(remote_url, str) and remote_url.strip():
        return remote_url.strip()
    for candidate in candidate_repo_dirs:
        url = _git_remote_url(repo_dir=candidate, remote_name=remote_name)
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def _remote_branch_exists(*, remote_url: str, branch: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--heads", remote_url, branch],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    if proc.returncode != 0:
        return False
    return bool((proc.stdout or "").strip())


def _resolve_default_branch_name(
    *,
    selected: SelectedTicket,
    remote_name: str,
    remote_url: str | None,
    candidate_repo_dirs: list[Path],
    wants_remote_handoff: bool,
) -> str:
    base_branch = _default_branch_name(selected)
    if not wants_remote_handoff:
        return base_branch
    resolved_remote_url = _resolve_remote_url_for_push(
        remote_name=remote_name,
        remote_url=remote_url,
        candidate_repo_dirs=candidate_repo_dirs,
    )
    if resolved_remote_url is None:
        return base_branch
    if not _remote_branch_exists(remote_url=resolved_remote_url, branch=base_branch):
        return base_branch
    suffix = 1
    while True:
        candidate = f"{base_branch}-rerun-{suffix}"
        if not _remote_branch_exists(remote_url=resolved_remote_url, branch=candidate):
            return candidate
        suffix += 1


def _should_move_ticket_to_review(
    *,
    commit_performed: bool,
    push_requested: bool,
    pr_requested: bool,
    push_ref: dict[str, Any] | None,
    pr_ref: dict[str, Any] | None,
) -> bool:
    if not commit_performed:
        return False
    if pr_requested:
        return bool(pr_ref is not None and pr_ref.get("created") is True)
    if push_requested:
        return bool(push_ref is not None and push_ref.get("pushed") is True)
    return True


def _require_stage6_implementation_ticket(selected: SelectedTicket) -> None:
    export_kind = (
        selected.export_kind.strip().lower()
        if isinstance(selected.export_kind, str) and selected.export_kind.strip()
        else None
    )
    stage = (
        selected.stage.strip().lower()
        if isinstance(selected.stage, str) and selected.stage.strip()
        else None
    )
    if export_kind != "implementation":
        raise SystemExit(
            "Ticket is not implementation-ready for `usertest-implement` "
            f"(fingerprint={selected.fingerprint}, export_kind={selected.export_kind!r}). "
            "Select a stage-6 implementation ticket (`export_kind=implementation`, `stage=ready_for_ticket`)."
        )
    if stage != "ready_for_ticket":
        raise SystemExit(
            "Ticket is not stage-6 ready for implementation "
            f"(fingerprint={selected.fingerprint}, stage={selected.stage!r}). "
            "Select a ticket with `stage=ready_for_ticket`."
        )


def _write_pr_manifest(
    *,
    run_dir: Path,
    selected: SelectedTicket,
    branch: str,
    agent: str,
    model: str | None,
) -> tuple[str, str]:
    title = f"{selected.fingerprint}: {selected.title or 'Implement backlog ticket'}"

    def _markdown_fence(text: str) -> str:
        max_run = 0
        cur = 0
        for ch in text:
            if ch == "`":
                cur += 1
                if cur > max_run:
                    max_run = cur
            else:
                cur = 0
        fence_len = max(3, max_run + 1)
        return "`" * fence_len

    ticket_text = selected.ticket_markdown.rstrip()
    ticket_fence = _markdown_fence(ticket_text)

    body_lines: list[str] = []
    body_lines.append(f"Fingerprint: `{selected.fingerprint}`")
    body_lines.append(f"Agent: `{agent}`")
    body_lines.append(f"Model: `{model or 'unknown'}`")
    body_lines.append("")
    body_lines.append("## Ticket (full)")
    body_lines.append("")
    body_lines.append(ticket_fence)
    body_lines.append(ticket_text)
    body_lines.append(ticket_fence)
    body_lines.append("")
    body_lines.append("## Testing")
    body_lines.append("")
    body_lines.append("- [ ] Add notes from `report.json` / `report.md`")
    body = "\n".join(body_lines).rstrip() + "\n"

    manifest_lines: list[str] = []
    manifest_lines.append(f"# {title}")
    manifest_lines.append("")
    manifest_lines.append(body.rstrip())
    manifest_lines.append("")
    manifest_lines.append("## Branch")
    manifest_lines.append("")
    manifest_lines.append(f"- `{branch}`")
    manifest = "\n".join(manifest_lines).rstrip() + "\n"

    (run_dir / "pr_manifest.md").write_text(manifest, encoding="utf-8")
    return title, body


def _run_gh(*, cwd: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise RuntimeError("gh not found on PATH") from exc


def _run_gh_json(*, cwd: Path, argv: list[str]) -> Any:
    proc = _run_gh(cwd=cwd, argv=argv)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        raise RuntimeError(stderr or stdout or "gh failed")
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh returned invalid JSON: {exc}") from exc


def _run_gh_text(*, cwd: Path, argv: list[str]) -> str:
    proc = _run_gh(cwd=cwd, argv=argv)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        raise RuntimeError(stderr or stdout or "gh failed")
    return proc.stdout or ""


def _load_ledger_entry(*, ledger_path: Path, fingerprint: str) -> dict[str, Any]:
    doc = load_ledger(ledger_path)
    actions = doc.get("actions")
    if not isinstance(actions, dict):
        return {}
    entry = actions.get(fingerprint)
    return entry if isinstance(entry, dict) else {}


def _coerce_pr_url(*, handoff_summary: dict[str, Any] | None, pr_ref: dict[str, Any] | None) -> str | None:
    if isinstance(handoff_summary, dict):
        pr_url = handoff_summary.get("pr_url")
        if isinstance(pr_url, str) and pr_url.strip():
            return pr_url.strip()
    if isinstance(pr_ref, dict):
        pr_url = pr_ref.get("url")
        if isinstance(pr_url, str) and pr_url.strip():
            return pr_url.strip()
    return None


def _classify_pr_checks(checks: list[dict[str, Any]]) -> tuple[str, str | None]:
    if not checks:
        return "pending", None

    success_states = {"SUCCESS", "SKIPPING", "NEUTRAL"}
    failure_states = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}
    pending_states = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}

    saw_pending = False
    for check in checks:
        state_raw = check.get("state")
        state = str(state_raw).strip().upper() if isinstance(state_raw, str) else ""
        if state in failure_states:
            return "completed", "failure"
        if state in pending_states or not state:
            saw_pending = True
        elif state not in success_states:
            saw_pending = True

    if saw_pending:
        return "pending", None
    return "completed", "success"


def _collect_pr_review_context(*, workspace_dir: Path, pr_url: str) -> dict[str, Any]:
    view_raw = _run_gh_json(
        cwd=workspace_dir,
        argv=[
            "gh",
            "pr",
            "view",
            pr_url,
            "--json",
            "number,url,title,state,isDraft,headRefName,baseRefName,mergeable,statusCheckRollup",
        ],
    )
    if not isinstance(view_raw, dict):
        raise RuntimeError("gh pr view returned non-object JSON")

    checks_raw = _run_gh_json(
        cwd=workspace_dir,
        argv=[
            "gh",
            "pr",
            "checks",
            pr_url,
            "--json",
            "name,state,startedAt,completedAt,link,bucket,event",
        ],
    )
    checks = [item for item in checks_raw if isinstance(item, dict)] if isinstance(checks_raw, list) else []
    ci_status, ci_conclusion = _classify_pr_checks(checks)

    changed_files_text = _run_gh_text(
        cwd=workspace_dir,
        argv=["gh", "pr", "diff", pr_url, "--name-only"],
    )
    changed_files = [line.strip() for line in changed_files_text.splitlines() if line.strip()]

    diff_full = _run_gh_text(
        cwd=workspace_dir,
        argv=["gh", "pr", "diff", pr_url],
    )
    diff_excerpt = diff_full
    diff_truncated = False
    if len(diff_excerpt) > _MAX_REVIEW_DIFF_CHARS:
        diff_excerpt = diff_excerpt[:_MAX_REVIEW_DIFF_CHARS].rstrip() + "\n\n[diff truncated]\n"
        diff_truncated = True

    return {
        "pr": view_raw,
        "checks": checks,
        "ci_status": ci_status,
        "ci_conclusion": ci_conclusion,
        "changed_files": changed_files,
        "diff_excerpt": diff_excerpt,
        "diff_truncated": diff_truncated,
    }


def _build_review_append_prompt(
    *,
    selected: SelectedTicket,
    handoff_summary: dict[str, Any] | None,
    pr_ref: dict[str, Any] | None,
    ci_gate: dict[str, Any] | None,
    pr_context: dict[str, Any],
) -> str:
    pr_json = json.dumps(pr_context.get("pr", {}), indent=2, ensure_ascii=False)
    checks_json = json.dumps(pr_context.get("checks", []), indent=2, ensure_ascii=False)
    handoff_json = json.dumps(handoff_summary or {}, indent=2, ensure_ascii=False)
    pr_ref_json = json.dumps(pr_ref or {}, indent=2, ensure_ascii=False)
    ci_gate_json = json.dumps(ci_gate or {}, indent=2, ensure_ascii=False)
    changed_files = pr_context.get("changed_files", [])
    changed_file_lines = "\n".join(f"- {path}" for path in changed_files) if changed_files else "- <none>"
    diff_excerpt = str(pr_context.get("diff_excerpt") or "").rstrip()

    return (
        "# Review task\n\n"
        "You are reviewing a PR-backed implementation of an already-selected backlog ticket.\n"
        "Do not redesign the ticket. Review only whether the PR stays aligned with the chosen approach,\n"
        "whether it adds unnecessary scope, whether there are implementation defects/regressions, and whether CI is green.\n\n"
        "Your report must use `task_run_v1` and must set `report.extensions.review_summary` to an object with:\n"
        "- `review_decision`: `approved` | `changes_requested` | `blocked`\n"
        "- `approach_alignment`: `aligned` | `diverged` | `unclear`\n"
        "- `scope_assessment`: `appropriate` | `excessive` | `unclear`\n"
        "- `rationale`: short string\n\n"
        "Use `issues[]` for findings. Do not modify repository source files. Do not merge the PR.\n\n"
        "# Ticket markdown\n\n"
        f"{selected.ticket_markdown.rstrip()}\n\n"
        "# Handoff summary\n\n"
        f"```json\n{handoff_json}\n```\n\n"
        "# PR reference\n\n"
        f"```json\n{pr_ref_json}\n```\n\n"
        "# CI gate artifact from implementation\n\n"
        f"```json\n{ci_gate_json}\n```\n\n"
        "# Current PR metadata\n\n"
        f"```json\n{pr_json}\n```\n\n"
        "# Current PR checks\n\n"
        f"```json\n{checks_json}\n```\n\n"
        "# Changed files\n\n"
        f"{changed_file_lines}\n\n"
        "# PR diff excerpt\n\n"
        "```diff\n"
        f"{diff_excerpt}\n"
        "```\n"
    )


def _extract_agent_review_summary(report: dict[str, Any]) -> dict[str, Any]:
    extensions = report.get("extensions")
    if not isinstance(extensions, dict):
        raise ValueError("report.json missing extensions object")
    review_summary = extensions.get("review_summary")
    if not isinstance(review_summary, dict):
        raise ValueError("report.json missing extensions.review_summary object")

    out: dict[str, Any] = {}
    for key, allowed in (
        ("review_decision", {"approved", "changes_requested", "blocked"}),
        ("approach_alignment", {"aligned", "diverged", "unclear"}),
        ("scope_assessment", {"appropriate", "excessive", "unclear"}),
    ):
        raw = review_summary.get(key)
        value = raw.strip().lower() if isinstance(raw, str) and raw.strip() else None
        if value not in allowed:
            raise ValueError(f"extensions.review_summary.{key} must be one of {sorted(allowed)!r}")
        out[key] = value

    rationale_raw = review_summary.get("rationale")
    if not isinstance(rationale_raw, str) or not rationale_raw.strip():
        raise ValueError("extensions.review_summary.rationale must be a non-empty string")
    out["rationale"] = rationale_raw.strip()
    return out


def _review_findings_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    issues_raw = report.get("issues")
    issues = issues_raw if isinstance(issues_raw, list) else []
    findings: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        severity_raw = issue.get("severity")
        severity = severity_raw if isinstance(severity_raw, str) and severity_raw.strip() else "info"
        title_raw = issue.get("title")
        details_raw = issue.get("details")
        if not isinstance(title_raw, str) or not title_raw.strip():
            continue
        if not isinstance(details_raw, str) or not details_raw.strip():
            continue
        findings.append(
            {
                "severity": severity.strip().lower(),
                "title": title_raw.strip(),
                "details": details_raw.strip(),
                "evidence": issue.get("evidence"),
                "suggested_fix": issue.get("suggested_fix"),
            }
        )
    return findings


def _stringify_review_detail(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _build_pr_review_body(*, review_summary: dict[str, Any]) -> str:
    decision = str(review_summary.get("review_decision") or "").strip().lower()
    alignment = str(review_summary.get("approach_alignment") or "").strip().lower()
    scope = str(review_summary.get("scope_assessment") or "").strip().lower()
    rationale = str(review_summary.get("rationale") or "").strip()
    merge_ready = bool(review_summary.get("merge_ready") is True)
    findings_raw = review_summary.get("findings")
    findings = findings_raw if isinstance(findings_raw, list) else []

    lines = [
        "## Automated implementation review",
        "",
        f"- Decision: `{decision or 'unknown'}`",
        f"- Approach alignment: `{alignment or 'unknown'}`",
        f"- Scope assessment: `{scope or 'unknown'}`",
        f"- Merge ready: `{'yes' if merge_ready else 'no'}`",
        "",
        "### Rationale",
        "",
        rationale or "No rationale provided.",
        "",
        "### Findings",
        "",
    ]
    if not findings:
        lines.append("No additional findings.")
    else:
        for index, finding_raw in enumerate(findings, start=1):
            if not isinstance(finding_raw, dict):
                continue
            severity = str(finding_raw.get("severity") or "info").strip().lower() or "info"
            title = str(finding_raw.get("title") or "Untitled finding").strip() or "Untitled finding"
            details = str(finding_raw.get("details") or "").strip() or "No details provided."
            lines.append(f"{index}. [{severity}] {title}")
            lines.append("")
            lines.append(details)
            evidence = _stringify_review_detail(finding_raw.get("evidence"))
            if evidence:
                lines.append("")
                lines.append(f"Evidence: {evidence}")
            suggested_fix = _stringify_review_detail(finding_raw.get("suggested_fix"))
            if suggested_fix:
                lines.append("")
                lines.append(f"Suggested fix: {suggested_fix}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _submit_pr_review(
    *,
    workspace_dir: Path,
    pr_url: str,
    review_run_dir: Path,
    review_summary: dict[str, Any],
) -> dict[str, Any]:
    body = _build_pr_review_body(review_summary=review_summary)
    body_path = review_run_dir / "pr_review.md"
    body_path.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        [
            "gh",
            "pr",
            "review",
            pr_url,
            "--comment",
            "--body-file",
            str(body_path),
        ],
        cwd=str(workspace_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "schema_version": 1,
        "pr_url": pr_url,
        "event": "COMMENT",
        "submitted": proc.returncode == 0,
        "body_path": str(body_path),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": int(proc.returncode),
        "submitted_at_utc": _utc_now_z() if proc.returncode == 0 else None,
    }


def _build_final_review_summary(
    *,
    selected: SelectedTicket,
    review_run_dir: Path,
    pr_url: str,
    pr_context: dict[str, Any],
    agent_summary: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    pr_meta = pr_context.get("pr")
    if not isinstance(pr_meta, dict):
        raise ValueError("PR context missing metadata")
    pr_number_raw = pr_meta.get("number")
    pr_number = (
        int(pr_number_raw)
        if isinstance(pr_number_raw, int)
        else int(str(pr_number_raw).strip())
        if isinstance(pr_number_raw, str) and str(pr_number_raw).strip().isdigit()
        else None
    )
    mergeable_raw = pr_meta.get("mergeable")
    mergeable = str(mergeable_raw).strip().upper() == "MERGEABLE"
    is_draft = bool(pr_meta.get("isDraft") is True)
    pr_state = str(pr_meta.get("state") or "").strip().upper()
    ci_status = str(pr_context.get("ci_status") or "pending")
    ci_conclusion_raw = pr_context.get("ci_conclusion")
    ci_conclusion = (
        str(ci_conclusion_raw).strip().lower()
        if isinstance(ci_conclusion_raw, str) and str(ci_conclusion_raw).strip()
        else None
    )
    merge_ready = (
        agent_summary["review_decision"] == "approved"
        and agent_summary["approach_alignment"] == "aligned"
        and agent_summary["scope_assessment"] == "appropriate"
        and ci_conclusion == "success"
        and mergeable
        and not is_draft
        and pr_state == "OPEN"
    )
    return {
        "schema_version": 1,
        "ticket_fingerprint": selected.fingerprint,
        "ticket_path": str(selected.idea_path) if selected.idea_path is not None else None,
        "run_dir": str(review_run_dir),
        "pr_url": pr_url,
        "pr_number": pr_number,
        "pr_state": pr_state.lower() if pr_state else None,
        "pr_title": pr_meta.get("title"),
        "head_ref_name": pr_meta.get("headRefName"),
        "base_ref_name": pr_meta.get("baseRefName"),
        "is_draft": is_draft,
        "mergeable": mergeable,
        "mergeable_state": mergeable_raw,
        "ci_status": ci_status,
        "ci_conclusion": ci_conclusion,
        "review_decision": agent_summary["review_decision"],
        "approach_alignment": agent_summary["approach_alignment"],
        "scope_assessment": agent_summary["scope_assessment"],
        "rationale": agent_summary["rationale"],
        "findings": _review_findings_from_report(report),
        "merge_ready": merge_ready,
        "review_source": "automated",
        "reviewed_at_utc": _utc_now_z(),
    }


def _current_merge_gate_from_pr_context(pr_context: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    pr_meta = pr_context.get("pr")
    if not isinstance(pr_meta, dict):
        raise ValueError("PR context missing metadata")
    mergeable_state = pr_meta.get("mergeable")
    mergeable = str(mergeable_state).strip().upper() == "MERGEABLE"
    is_draft = bool(pr_meta.get("isDraft") is True)
    pr_state = str(pr_meta.get("state") or "").strip().upper()
    ci_status = str(pr_context.get("ci_status") or "pending")
    ci_conclusion_raw = pr_context.get("ci_conclusion")
    ci_conclusion = (
        str(ci_conclusion_raw).strip().lower()
        if isinstance(ci_conclusion_raw, str) and str(ci_conclusion_raw).strip()
        else None
    )
    gate = {
        "pr_state": pr_state.lower() if pr_state else None,
        "is_draft": is_draft,
        "mergeable": mergeable,
        "mergeable_state": mergeable_state,
        "ci_status": ci_status,
        "ci_conclusion": ci_conclusion,
    }
    okay = pr_state == "OPEN" and not is_draft and mergeable and ci_conclusion == "success"
    return okay, gate


def _run_review_for_selected_ticket(
    *,
    repo_root: Path,
    cfg: RunnerConfig,
    owner_root: Path,
    selected: SelectedTicket,
    implementation_run_dir: Path,
    ledger_path: Path,
    review_agent: str,
    review_model: str | None,
    review_policy: str,
    review_persona_id: str,
    review_mission_id: str,
    review_seed: int,
    review_agent_config_override: list[str],
    keep_workspace: bool,
    exec_backend: str,
    exec_use_host_agent_login: bool,
    exec_use_target_sandbox_cli_install: bool,
    exec_docker_profile: str | None,
    exec_keep_container: bool,
    exec_cache: str,
    exec_cache_dir: Path | None,
    maintenance_venv_cache: bool,
    dry_run: bool,
) -> tuple[Path | None, dict[str, Any] | None]:
    if not implementation_run_dir.exists():
        raise SystemExit(f"Recorded implementation run dir does not exist: {implementation_run_dir}")
    if selected.idea_path is None or "4 - for_review" not in selected.idea_path.parts:
        raise SystemExit(
            f"Ticket {selected.fingerprint!r} is not in 4 - for_review and cannot be reviewed yet."
        )

    handoff_summary = _read_json(implementation_run_dir / "handoff_summary.json")
    pr_ref = _read_json(implementation_run_dir / "pr_ref.json")
    ci_gate = _read_json(implementation_run_dir / "ci_gate.json")
    pr_url = _coerce_pr_url(handoff_summary=handoff_summary, pr_ref=pr_ref)
    if pr_url is None:
        raise SystemExit(
            f"Ticket {selected.fingerprint!r} does not have a PR to review "
            f"(run_dir={implementation_run_dir})."
        )

    pr_context = _collect_pr_review_context(workspace_dir=owner_root, pr_url=pr_url)
    pr_meta = pr_context.get("pr")
    if not isinstance(pr_meta, dict):
        raise SystemExit("Unable to read PR metadata for review.")
    current_merge_ready, current_gate = _current_merge_gate_from_pr_context(pr_context)
    if not current_merge_ready:
        raise SystemExit(
            "Refusing to run review before the PR gate is green: "
            + json.dumps(current_gate, ensure_ascii=False)
        )

    head_ref_name = pr_meta.get("headRefName")
    review_prompt = _build_review_append_prompt(
        selected=selected,
        handoff_summary=handoff_summary if isinstance(handoff_summary, dict) else None,
        pr_ref=pr_ref if isinstance(pr_ref, dict) else None,
        ci_gate=ci_gate if isinstance(ci_gate, dict) else None,
        pr_context=pr_context,
    )

    if dry_run:
        print(
            json.dumps(
                {
                    "ticket_fingerprint": selected.fingerprint,
                    "ticket_path": str(selected.idea_path) if selected.idea_path is not None else None,
                    "implementation_run_dir": str(implementation_run_dir),
                    "pr_url": pr_url,
                    "head_ref_name": head_ref_name,
                    "review_agent": review_agent,
                    "review_model": review_model,
                    "review_persona_id": review_persona_id,
                    "review_mission_id": review_mission_id,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return None, None

    effective_repo_input = str(owner_root)
    if _looks_like_local_path(effective_repo_input):
        git_root = _infer_git_root(owner_root)
        if git_root is not None:
            remote_url = _git_remote_url(repo_dir=git_root, remote_name="origin")
            if isinstance(remote_url, str) and remote_url.strip():
                effective_repo_input = remote_url.strip()

    effective_exec_backend = str(exec_backend).strip().lower()
    maintenance_profile_eligible = _maintenance_profile_is_eligible(
        repo_root=repo_root,
        repo_input=effective_repo_input,
    )
    effective_exec_docker_profile = _resolve_exec_docker_profile(
        exec_backend=effective_exec_backend,
        requested_profile=exec_docker_profile,
        maintenance_eligible=maintenance_profile_eligible,
    )
    if effective_exec_backend == "docker":
        _require_docker_available()

    staged_review_prompt_dir = repo_root / "runs" / "_tmp_review_prompt_staging"
    staged_review_prompt_dir.mkdir(parents=True, exist_ok=True)
    staged_review_prompt_path = (
        staged_review_prompt_dir
        / f"{selected.fingerprint}_{int(time.time() * 1000)}_review_prompt.md"
    )
    staged_review_prompt_path.write_text(review_prompt, encoding="utf-8")

    request = RunRequest(
        repo=effective_repo_input,
        ref=str(head_ref_name).strip() if isinstance(head_ref_name, str) and head_ref_name.strip() else None,
        agent=review_agent,
        model=review_model,
        policy=review_policy,
        persona_id=review_persona_id,
        mission_id=review_mission_id,
        seed=review_seed,
        agent_config_overrides=tuple(str(v) for v in review_agent_config_override or []),
        agent_append_system_prompt_file=staged_review_prompt_path,
        keep_workspace=bool(keep_workspace),
        verification_commands=(),
        verification_reuse_mode="off",
        exec_backend=effective_exec_backend,
        exec_docker_profile=effective_exec_docker_profile,
        exec_use_host_agent_login=bool(exec_use_host_agent_login),
        exec_use_target_sandbox_cli_install=bool(exec_use_target_sandbox_cli_install),
        exec_cache=str(exec_cache),
        exec_cache_dir=exec_cache_dir,
        exec_maintenance_venv_cache=bool(maintenance_venv_cache),
        exec_keep_container=bool(exec_keep_container),
    )

    try:
        result = run_once(cfg, request)
    finally:
        try:
            staged_review_prompt_path.unlink(missing_ok=True)
        except OSError:
            pass
    review_run_dir = result.run_dir
    if int(result.exit_code or 0) != 0:
        raise SystemExit(f"Review run failed (exit_code={result.exit_code}) in {review_run_dir}")
    if result.report_validation_errors:
        raise SystemExit(
            "Review run produced an invalid report: "
            + "; ".join(str(err) for err in result.report_validation_errors)
        )
    report = _read_json(review_run_dir / "report.json")
    if not isinstance(report, dict):
        raise SystemExit(f"Missing or invalid report.json in review run dir: {review_run_dir}")
    try:
        agent_summary = _extract_agent_review_summary(report)
        review_summary = _build_final_review_summary(
            selected=selected,
            review_run_dir=review_run_dir,
            pr_url=pr_url,
            pr_context=pr_context,
            agent_summary=agent_summary,
            report=report,
        )
    except ValueError as exc:
        raise SystemExit(f"Invalid review output in {review_run_dir}: {exc}") from exc

    _write_json(review_run_dir / "review_summary.json", review_summary)
    pr_review_ref = _submit_pr_review(
        workspace_dir=owner_root,
        pr_url=pr_url,
        review_run_dir=review_run_dir,
        review_summary=review_summary,
    )
    _write_json(review_run_dir / "pr_review_ref.json", pr_review_ref)
    if pr_review_ref.get("submitted") is not True:
        raise SystemExit(
            "Failed to publish PR review: "
            + (
                str(pr_review_ref.get("stderr") or "").strip()
                or str(pr_review_ref.get("stdout") or "").strip()
                or "gh pr review failed"
            )
        )
    _write_json(
        review_run_dir / "review_ref.json",
        {
            "schema_version": 1,
            "ticket_fingerprint": selected.fingerprint,
            "ticket_path": str(selected.idea_path) if selected.idea_path is not None else None,
            "implementation_run_dir": str(implementation_run_dir),
            "pr_url": pr_url,
        },
    )
    update_ledger_file(
        ledger_path,
        fingerprint=selected.fingerprint,
        updates={
            "last_review_run_dir": str(review_run_dir),
            "last_review_pr_url": pr_url,
            "last_review_decision": review_summary["review_decision"],
            "last_review_merge_ready": bool(review_summary["merge_ready"]),
            "last_review_ci_conclusion": review_summary.get("ci_conclusion"),
        },
    )
    return review_run_dir, review_summary


def _build_handoff_summary(
    *,
    branch: str,
    commit_requested: bool,
    commit_performed: bool,
    push_requested: bool,
    push_ref: dict[str, Any] | None,
    pr_requested: bool,
    pr_ref: dict[str, Any] | None,
    ci_gate: dict[str, Any] | None,
    review_required: bool,
    review_run_dir: Path | None,
    review_summary: dict[str, Any] | None,
    review_error: str | None,
) -> dict[str, Any]:
    ci_status = None
    ci_conclusion = None
    ci_run_url = None
    if isinstance(ci_gate, dict):
        ci_status_raw = ci_gate.get("status")
        ci_conclusion_raw = ci_gate.get("conclusion")
        ci_status = str(ci_status_raw).strip() if isinstance(ci_status_raw, str) and ci_status_raw.strip() else None
        ci_conclusion = (
            str(ci_conclusion_raw).strip().lower()
            if isinstance(ci_conclusion_raw, str) and ci_conclusion_raw.strip()
            else None
        )
        ci_run_url_raw = ci_gate.get("run_url")
        if isinstance(ci_run_url_raw, str) and ci_run_url_raw.strip():
            ci_run_url = ci_run_url_raw.strip()
        if ci_status is None and ci_gate.get("skipped") is True:
            ci_status = "skipped"
        if ci_conclusion is None:
            if ci_gate.get("passed") is True:
                ci_conclusion = "success"
                ci_status = ci_status or "completed"
            elif ci_gate.get("passed") is False:
                ci_conclusion = "failure"
                ci_status = ci_status or "completed"

    pr_url = None
    pr_created = False
    if isinstance(pr_ref, dict):
        pr_created = bool(pr_ref.get("created") is True)
        pr_url_raw = pr_ref.get("url")
        if isinstance(pr_url_raw, str) and pr_url_raw.strip():
            pr_url = pr_url_raw.strip()

    pushed = bool(isinstance(push_ref, dict) and push_ref.get("pushed") is True)
    review_decision = None
    review_merge_ready = None
    if isinstance(review_summary, dict):
        review_decision_raw = review_summary.get("review_decision")
        if isinstance(review_decision_raw, str) and review_decision_raw.strip():
            review_decision = review_decision_raw.strip()
        review_merge_ready = bool(review_summary.get("merge_ready") is True)

    final_status = "success"
    if pr_created:
        if review_error is not None:
            final_status = "failure"

    return {
        "schema_version": 1,
        "branch": branch,
        "commit_requested": bool(commit_requested),
        "commit_performed": bool(commit_performed),
        "push_requested": bool(push_requested),
        "pushed": pushed,
        "pr_requested": bool(pr_requested),
        "pr_created": pr_created,
        "pr_url": pr_url,
        "ci_required": pr_created,
        "ci_status": ci_status,
        "ci_run_url": ci_run_url,
        "ci_conclusion": ci_conclusion,
        "review_required": bool(review_required),
        "review_run_dir": str(review_run_dir) if review_run_dir is not None else None,
        "review_decision": review_decision,
        "review_merge_ready": review_merge_ready,
        "review_error": review_error,
        "final_status": final_status,
    }


def _require_docker_available() -> None:
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit(
            "Docker exec backend is enabled but Docker is not available.\n"
            "\n"
            "Reason: `docker` was not found on PATH.\n"
            "\n"
            "Fix: install Docker (Docker Desktop on Windows/macOS; Docker Engine on Linux) and ensure it is running.\n"
            "\n"
            "Opt out (run without sandboxing): pass `--no-docker` (or `--exec-backend local`)."
        )
    try:
        proc = subprocess.run(
            [docker, "version"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(
            "Docker exec backend is enabled but Docker is not responding.\n"
            "\n"
            "Reason: `docker version` timed out.\n"
            "\n"
            "Fix: start Docker and try again.\n"
            "\n"
            "Opt out (run without sandboxing): pass `--no-docker` (or `--exec-backend local`)."
        ) from None
    except OSError as e:
        raise SystemExit(
            "Docker exec backend is enabled but Docker is not usable.\n"
            "\n"
            f"Reason: failed to run `docker version`: {e}\n"
            "\n"
            "Fix: install/start Docker and try again.\n"
            "\n"
            "Opt out (run without sandboxing): pass `--no-docker` (or `--exec-backend local`)."
        ) from e

    if proc.returncode != 0:
        details = (proc.stderr or proc.stdout or "").strip()
        detail_block = f"\n\nDocker output:\n{details}" if details else ""
        raise SystemExit(
            "Docker exec backend is enabled but Docker is not usable.\n"
            "\n"
            "Reason: `docker version` failed (non-zero exit code)."
            f"{detail_block}\n"
            "\n"
            "Fix: start Docker and try again.\n"
            "\n"
            "Opt out (run without sandboxing): pass `--no-docker` (or `--exec-backend local`)."
        )


def _run_selected_ticket(
    *,
    args: argparse.Namespace,
    repo_root: Path,
    cfg: RunnerConfig,
    selected: SelectedTicket,
) -> int:
    _require_stage6_implementation_ticket(selected)
    settings_info = getattr(args, "_settings_info", None)

    repo_input: str | None = None
    repo_is_explicit = False
    if isinstance(args.repo, str) and args.repo.strip():
        repo_input = args.repo.strip()
        repo_is_explicit = True
    elif selected.owner_root is not None:
        repo_input = str(selected.owner_root)
    elif selected.idea_path is not None:
        inferred = _infer_git_root(selected.idea_path.parent)
        repo_input = str(inferred) if inferred is not None else str(selected.idea_path.parent)
    else:
        raise SystemExit("Unable to infer target repo. Provide --repo.")

    # Default handoff flags may be enabled on some subcommands (e.g. tickets run-next).
    # Normalize so disabling an earlier step disables dependent later steps.
    if not bool(args.commit):
        args.push = False
        args.pr = False
    elif not bool(args.push):
        args.pr = False

    if args.push or args.pr:
        if not args.commit:
            raise SystemExit("--push/--pr requires --commit")

    keep_workspace = bool(args.keep_workspace) or bool(args.commit) or bool(args.push) or bool(args.pr)

    verification_commands: list[str] = []
    for cmd in getattr(args, "verification_commands", None) or []:
        if not isinstance(cmd, str) or not cmd.strip():
            raise SystemExit(f"--verify-command entries must be non-empty strings; got {cmd!r}.")
        verification_commands.append(cmd.strip())

    verification_timeout_seconds = getattr(args, "verify_timeout_seconds", None)
    if verification_timeout_seconds is not None and verification_timeout_seconds <= 0:
        verification_timeout_seconds = None
    verification_profile = str(
        getattr(args, "verification_profile", "default_handoff") or "default_handoff"
    ).strip().lower()
    if verification_profile not in {"default_handoff", "none"}:
        raise SystemExit(
            "verification_profile must be one of {'default_handoff', 'none'}; "
            f"got {getattr(args, 'verification_profile', None)!r}."
        )
    verification_reuse_mode = str(getattr(args, "verify_reuse", "auto") or "auto").strip().lower()
    if verification_reuse_mode not in {"auto", "off"}:
        raise SystemExit(
            f"--verify-reuse must be one of auto/off; got {getattr(args, 'verify_reuse', None)!r}."
        )

    wants_handoff = bool(args.commit) or bool(args.push) or bool(args.pr)

    # For ticket implementation workflows that create branches/PRs, it's easy to accidentally run
    # the next ticket off whatever branch your local repo currently has checked out.
    #
    # Default to the PR base branch (dev by default) unless the user explicitly provided --ref.
    effective_ref = args.ref
    if wants_handoff and (effective_ref is None or not str(effective_ref).strip()):
        base = str(getattr(args, "base_branch", "") or "").strip()
        if base:
            effective_ref = base

    # Similarly, when a ticket is being turned into a PR, prefer cloning from the repo's
    # configured remote (e.g. origin) so merged changes on the base branch are picked up even
    # if the local checkout is behind.
    effective_repo_input = repo_input
    if (
        wants_handoff
        and not repo_is_explicit
        and isinstance(repo_input, str)
        and _looks_like_local_path(repo_input)
    ):
        repo_path = Path(repo_input).expanduser()
        git_root = _infer_git_root(repo_path)
        if git_root is not None:
            remote_url = _git_remote_url(
                repo_dir=git_root,
                remote_name=str(getattr(args, "remote_name", "origin") or "origin"),
            )
            if remote_url is not None:
                effective_repo_input = remote_url

    exec_backend = str(args.exec_backend).strip().lower()
    maintenance_profile_eligible = _maintenance_profile_is_eligible(
        repo_root=repo_root,
        repo_input=str(effective_repo_input),
    )
    exec_docker_profile = _resolve_exec_docker_profile(
        exec_backend=exec_backend,
        requested_profile=getattr(args, "exec_docker_profile", None),
        maintenance_eligible=maintenance_profile_eligible,
    )

    if (
        wants_handoff
        and verification_profile == "default_handoff"
        and not verification_commands
        and not bool(getattr(args, "skip_verify", False))
    ):
        install_gate = "python tools/scaffold/scaffold.py run --all --skip-missing install"
        lint_gate = "python tools/scaffold/scaffold.py run --all --skip-missing lint"
        test_gate = "python tools/scaffold/scaffold.py run --all --skip-missing test"

        if exec_backend == "docker":
            scaffold_prefix = (
                'PYTHON_BIN=python; command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN=python3; '
                '"$PYTHON_BIN" tools/scaffold/scaffold.py run --all --skip-missing '
            )
            smoke_cmd = "bash ./scripts/smoke.sh"
            if exec_docker_profile == "maintenance":
                smoke_cmd = "bash ./scripts/smoke.sh --skip-install --use-pythonpath"
            verification_commands = [
                smoke_cmd,
                f"{scaffold_prefix}install",
                f"{scaffold_prefix}lint",
                f"{scaffold_prefix}test",
            ]
        elif os.name == "nt":
            verification_commands = [
                "powershell -NoProfile -ExecutionPolicy Bypass -File .\\scripts\\smoke.ps1",
                install_gate,
                lint_gate,
                test_gate,
            ]
        else:
            verification_commands = [
                "bash ./scripts/smoke.sh",
                install_gate,
                lint_gate,
                test_gate,
            ]

    exec_cache = str(getattr(args, "exec_cache", "cold") or "cold")
    exec_cache_dir = getattr(args, "exec_cache_dir", None)
    if exec_cache_dir is not None:
        exec_cache_dir = exec_cache_dir.resolve()
    if exec_cache == "warm" and exec_cache_dir is None:
        exec_cache_dir = repo_root / "runs" / "_cache" / "usertest_implement"
    maintenance_venv_cache = bool(
        exec_backend == "docker"
        and exec_cache == "warm"
        and bool(getattr(args, "maintenance_venv_cache", True))
    )

    ticket_blob = _compose_ticket_blob(selected)
    request = RunRequest(
        repo=str(effective_repo_input),
        ref=effective_ref,
        agent=str(args.agent),
        policy=str(args.policy),
        persona_id=args.persona_id,
        mission_id=args.mission_id,
        seed=int(args.seed),
        model=args.model,
        agent_config_overrides=tuple(args.agent_config_override or []),
        agent_append_system_prompt=ticket_blob,
        keep_workspace=keep_workspace,
        verification_commands=tuple(verification_commands),
        verification_timeout_seconds=verification_timeout_seconds,
        verification_reuse_mode=verification_reuse_mode,
        exec_backend=exec_backend,
        exec_docker_profile=exec_docker_profile,
        exec_keep_container=bool(args.exec_keep_container),
        exec_cache=exec_cache,
        exec_cache_dir=exec_cache_dir,
        exec_maintenance_venv_cache=maintenance_venv_cache,
        exec_use_host_agent_login=bool(args.exec_use_host_agent_login),
        exec_use_target_sandbox_cli_install=bool(args.exec_use_target_sandbox_cli_install),
    )

    if args.dry_run:
        selected_dict = asdict(selected)
        selected_dict["owner_root"] = (
            str(selected.owner_root) if selected.owner_root is not None else None
        )
        selected_dict["idea_path"] = str(selected.idea_path) if selected.idea_path is not None else None
        selected_dict["tickets_export_path"] = (
            str(selected.tickets_export_path) if selected.tickets_export_path is not None else None
        )
        payload = {
            "selected_ticket": selected_dict,
            "settings": settings_info,
            "run_request": {
                "repo": request.repo,
                "ref": request.ref,
                "agent": request.agent,
                "policy": request.policy,
                "persona_id": request.persona_id,
                "mission_id": request.mission_id,
                "seed": request.seed,
                "model": request.model,
                "keep_workspace": request.keep_workspace,
                "exec_backend": request.exec_backend,
                "exec_docker_profile": request.exec_docker_profile,
                "exec_docker_profile_eligible": maintenance_profile_eligible,
                "exec_keep_container": request.exec_keep_container,
                "exec_cache": request.exec_cache,
                "exec_maintenance_venv_cache": request.exec_maintenance_venv_cache,
                "verification_profile": verification_profile,
                "verification_commands": list(request.verification_commands),
                "verification_timeout_seconds": request.verification_timeout_seconds,
                "verification_reuse_mode": request.verification_reuse_mode,
                "commit": bool(args.commit),
                "push": bool(args.push),
                "pr": bool(args.pr),
                "move_on_start": bool(args.move_on_start),
                "move_on_commit": bool(args.move_on_commit),
            },
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if exec_backend == "docker":
        _require_docker_available()

    if args.move_on_start and selected.owner_root is not None and selected.idea_path is not None:
        try:
            move_ticket_file(
                owner_root=selected.owner_root,
                fingerprint=selected.fingerprint,
                to_bucket="3 - in_progress",
                dry_run=False,
            )
            _sync_ticket_atom_actions(repo_root=repo_root, owner_root=selected.owner_root)
        except Exception as e:
            print(f"WARNING: failed to move ticket to in_progress: {e}", file=sys.stderr)

    started_at = _utc_now_z()
    wall_start = time.monotonic()
    result = run_once(cfg, request)
    finished_at = _utc_now_z()
    duration_seconds = max(0.0, time.monotonic() - wall_start)

    run_dir = result.run_dir
    timing_payload = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
    }
    run_meta = _read_json(run_dir / "run_meta.json")
    if isinstance(run_meta, dict):
        phases = run_meta.get("phases")
        if isinstance(phases, dict):
            timing_payload["phases"] = phases
    _write_json(run_dir / "timing.json", timing_payload)
    _write_json(
        run_dir / "settings_ref.json",
        {
            "schema_version": 1,
            "settings": settings_info,
        },
    )
    _write_json(
        run_dir / "ticket_ref.json",
        {
            "schema_version": 1,
            "fingerprint": selected.fingerprint,
            "title": selected.title,
            "export_kind": selected.export_kind,
            "tickets_export_path": (
                str(selected.tickets_export_path) if selected.tickets_export_path is not None else None
            ),
            "export_index": selected.export_index,
            "owner_repo": {
                "root": str(selected.owner_root) if selected.owner_root is not None else None,
                "idea_path": str(selected.idea_path) if selected.idea_path is not None else None,
            },
        },
    )

    exit_code = int(result.exit_code or 0)
    verification_failed = False
    failing_verification_command: str | None = None

    verification_configured = bool(request.verification_commands)
    if verification_configured and not bool(getattr(args, "skip_verify", False)):
        verification = _read_json(run_dir / "verification.json")
        if isinstance(verification, dict) and verification.get("passed") is False:
            verification_failed = True
            exit_code = max(exit_code, 2)
            commands = verification.get("commands")
            if isinstance(commands, list):
                for cmd in commands:
                    if not isinstance(cmd, dict):
                        continue
                    cmd_exit = cmd.get("exit_code")
                    if isinstance(cmd_exit, int) and cmd_exit != 0:
                        raw_cmd = cmd.get("command")
                        if isinstance(raw_cmd, str) and raw_cmd.strip():
                            failing_verification_command = raw_cmd.strip()
                        break

            if wants_handoff:
                print(
                    "[implement] ERROR: Verification gate failed; refusing to commit/push/PR.",
                    file=sys.stderr,
                )
            else:
                print("[implement] ERROR: Verification gate failed.", file=sys.stderr)
            print(f"  Run dir: {run_dir}", file=sys.stderr)
            if failing_verification_command is not None:
                print(f"  Failing command: {failing_verification_command}", file=sys.stderr)
            print(
                "  Override (debugging only): rerun with --skip-verify",
                file=sys.stderr,
            )

    handoff_blocked = bool(wants_handoff and verification_failed and not args.skip_verify)

    workspace_ref = _read_json(run_dir / "workspace_ref.json")
    workspace_dir: Path | None = None
    if isinstance(workspace_ref, dict):
        ws = workspace_ref.get("workspace_dir")
        if isinstance(ws, str) and ws.strip():
            workspace_dir = Path(ws)

    push_candidates: list[Path] = []
    if selected.owner_root is not None and (selected.owner_root / ".git").exists():
        push_candidates.append(selected.owner_root)
    if _looks_like_local_path(repo_input) and (Path(repo_input) / ".git").exists():
        push_candidates.append(Path(repo_input))

    if args.branch:
        branch = args.branch
    else:
        branch = _resolve_default_branch_name(
            selected=selected,
            remote_name=str(args.remote_name),
            remote_url=args.remote_url,
            candidate_repo_dirs=push_candidates,
            wants_remote_handoff=bool(args.push or args.pr),
        )
    commit_message = (
        args.commit_message
        or f"{selected.fingerprint}: {selected.title or 'Implement backlog ticket'}"
    )

    git_ref: dict[str, Any] | None = None
    push_ref: dict[str, Any] | None = None
    pr_ref: dict[str, Any] | None = None

    observed_model = infer_observed_model(run_dir=run_dir)
    commit_performed = False

    if args.commit and not handoff_blocked:
        git_ref = finalize_commit(
            run_dir=run_dir,
            branch=branch,
            commit_message=commit_message,
            git_user_name=args.git_user_name,
            git_user_email=args.git_user_email,
        )
        commit_performed = bool(git_ref.get("commit_performed") is True)

    if args.push and not handoff_blocked:
        if not commit_performed:
            push_ref = {
                "schema_version": 1,
                "remote_name": str(args.remote_name),
                "remote_url": args.remote_url,
                "branch": branch,
                "force_with_lease": bool(args.force_push),
                "pushed": False,
                "stdout": None,
                "stderr": None,
                "error": "Skipping push: no commit was performed.",
            }
            _write_json(run_dir / "push_ref.json", push_ref)
        else:
            push_ref = finalize_push(
                run_dir=run_dir,
                remote_name=str(args.remote_name),
                remote_url=args.remote_url,
                candidate_repo_dirs=push_candidates,
                branch=branch,
                force_with_lease=bool(args.force_push),
            )

    if (args.push or args.pr) and not handoff_blocked:
        title, body = _write_pr_manifest(
            run_dir=run_dir,
            selected=selected,
            branch=branch,
            agent=str(args.agent),
            model=observed_model,
        )
        pr_ref = {
            "schema_version": 1,
            "requested": bool(args.pr),
            "created": False,
            "url": None,
            "title": title,
            "body": body,
            "agent": str(args.agent),
            "model": observed_model,
            "error": None,
        }
        if args.pr:
            if not commit_performed:
                pr_ref["error"] = "Skipping PR creation: no commit was performed."
            else:
                if workspace_dir is None:
                    pr_ref["error"] = "Missing workspace_ref.json; cannot locate workspace"
                else:
                    create_draft = False
                    pr_body = body

                    if bool(args.skip_ci_wait):
                        head_sha = _git_head_sha(workspace_dir)
                        _write_json(
                            run_dir / "ci_gate.json",
                            {
                                "schema_version": 1,
                                "workflow": "CI",
                                "branch": branch,
                                "head_sha": head_sha,
                                "run_id": None,
                                "run_url": None,
                                "status": None,
                                "conclusion": None,
                                "passed": None,
                                "error": None,
                                "skipped": True,
                                "skip_reason": "flag --skip-ci-wait",
                                "started_at_utc": _utc_now_z(),
                                "finished_at_utc": _utc_now_z(),
                                "timeout_seconds": _ci_timeout_seconds_arg(
                                    args.ci_timeout_seconds
                                ),
                            },
                        )
                    else:
                        if not (push_ref is not None and push_ref.get("pushed") is True):
                            pr_ref["error"] = (
                                "Refusing to create PR before CI: branch was not pushed successfully "
                                "(rerun with --push or pass --skip-ci-wait)."
                            )
                            _write_json(
                                run_dir / "ci_gate.json",
                                {
                                    "schema_version": 1,
                                    "workflow": "CI",
                                    "branch": branch,
                                    "head_sha": None,
                                    "run_id": None,
                                    "run_url": None,
                                    "status": None,
                                    "conclusion": None,
                                    "passed": None,
                                    "error": None,
                                    "skipped": True,
                                    "skip_reason": "branch_not_pushed",
                                    "started_at_utc": _utc_now_z(),
                                    "finished_at_utc": _utc_now_z(),
                                    "timeout_seconds": _ci_timeout_seconds_arg(
                                        args.ci_timeout_seconds
                                    ),
                                },
                            )
                        else:
                            head_sha = _git_head_sha(workspace_dir)
                            if head_sha is None:
                                pr_ref["error"] = "Unable to determine HEAD SHA for CI gating."
                                _write_json(
                                    run_dir / "ci_gate.json",
                                    {
                                        "schema_version": 1,
                                        "workflow": "CI",
                                        "branch": branch,
                                        "head_sha": None,
                                        "run_id": None,
                                        "run_url": None,
                                        "status": None,
                                        "conclusion": None,
                                        "passed": None,
                                        "error": pr_ref["error"],
                                        "skipped": True,
                                        "skip_reason": "head_sha_unavailable",
                                        "started_at_utc": _utc_now_z(),
                                        "finished_at_utc": _utc_now_z(),
                                        "timeout_seconds": _ci_timeout_seconds_arg(
                                            args.ci_timeout_seconds
                                        ),
                                    },
                                )
                            else:
                                ci_timeout = _ci_timeout_seconds_arg(args.ci_timeout_seconds)
                                ci_ref = _wait_for_ci_success(
                                    run_dir=run_dir,
                                    workspace_dir=workspace_dir,
                                    branch=branch,
                                    head_sha=head_sha,
                                    workflow="CI",
                                    timeout_seconds=ci_timeout,
                                )
                                pr_ref["ci_gate_passed"] = bool(ci_ref.get("passed") is True)
                                pr_ref["ci_gate_run_url"] = ci_ref.get("run_url")
                                if ci_ref.get("passed") is not True:
                                    if bool(args.draft_pr_on_ci_failure):
                                        create_draft = True
                                        ci_err = ci_ref.get("error") or "CI gate failed."
                                        pr_ref["ci_gate_error"] = ci_err
                                        pr_body = (
                                            pr_body.rstrip()
                                            + "\n\n---\n\nCI gate failed (draft PR created):\n\n"
                                            + f"- {ci_err}\n"
                                        )
                                    else:
                                        pr_ref["error"] = ci_ref.get("error") or "CI gate failed."
                                if create_draft:
                                    pr_ref["draft"] = True

                    if pr_ref.get("error"):
                        pass
                    else:
                        pr_ref["body"] = pr_body
                        try:
                            pr_url = _run_gh_text(
                                cwd=workspace_dir,
                                argv=[
                                    "gh",
                                    "pr",
                                    "create",
                                    "--base",
                                    str(args.base_branch),
                                    "--title",
                                    title,
                                    "--body",
                                    pr_body,
                                    *(["--draft"] if create_draft else []),
                                ],
                            ).strip()
                            pr_ref["created"] = True
                            pr_ref["url"] = pr_url or None
                        except RuntimeError as exc:
                            pr_ref["error"] = str(exc)
        _write_json(run_dir / "pr_ref.json", pr_ref)

    if (
        args.move_on_commit
        and selected.owner_root is not None
        and selected.idea_path is not None
        and _should_move_ticket_to_review(
            commit_performed=commit_performed,
            push_requested=bool(args.push),
            pr_requested=bool(args.pr),
            push_ref=push_ref,
            pr_ref=pr_ref,
        )
    ):
        try:
            move_ticket_file(
                owner_root=selected.owner_root,
                fingerprint=selected.fingerprint,
                to_bucket="4 - for_review",
                dry_run=False,
            )
            _sync_ticket_atom_actions(repo_root=repo_root, owner_root=selected.owner_root)
        except Exception as e:
            print(f"WARNING: failed to move ticket to for_review: {e}", file=sys.stderr)

    ledger_path: Path | None = None
    if args.ledger is not None:
        ledger_path = _resolve_ledger_path(repo_root=repo_root, raw=args.ledger)
        updates: dict[str, Any] = {
            "title": selected.title,
            "owner_root": str(selected.owner_root) if selected.owner_root is not None else None,
            "idea_path": str(selected.idea_path) if selected.idea_path is not None else None,
            "last_run_dir": str(run_dir),
            "last_exit_code": int(result.exit_code),
            "last_started_at": started_at,
            "last_finished_at": finished_at,
            "last_duration_seconds": duration_seconds,
        }
        if git_ref is not None:
            updates["last_branch"] = git_ref.get("branch")
            updates["last_head_commit"] = git_ref.get("head_commit")
        if push_ref is not None and push_ref.get("pushed") is True:
            updates["last_push_remote"] = push_ref.get("remote_name")
            updates["last_push_remote_url"] = push_ref.get("remote_url")
        if pr_ref is not None and isinstance(pr_ref.get("url"), str):
            updates["last_pr_url"] = pr_ref.get("url")

        try:
            update_ledger_file(ledger_path, fingerprint=selected.fingerprint, updates=updates)
        except Exception as e:
            print(f"WARNING: failed to update ledger: {e}", file=sys.stderr)

    review_required = bool(
        args.pr
        and isinstance(pr_ref, dict)
        and pr_ref.get("created") is True
        and isinstance(args.implementation_review_agent, str)
        and args.implementation_review_agent.strip()
    )
    review_run_dir: Path | None = None
    review_summary: dict[str, Any] | None = None
    review_error: str | None = None
    if review_required:
        owner_root = selected.owner_root if selected.owner_root is not None else repo_root
        resolved_ledger_path = (
            _resolve_ledger_path(repo_root=repo_root, raw=args.ledger)
            if args.ledger is not None
            else _DEFAULT_LEDGER_PATH
        )
        try:
            review_run_dir, review_summary = _run_review_for_selected_ticket(
                repo_root=repo_root,
                cfg=cfg,
                owner_root=owner_root,
                selected=selected,
                implementation_run_dir=run_dir,
                ledger_path=resolved_ledger_path,
                review_agent=str(args.implementation_review_agent),
                review_model=args.implementation_review_model,
                review_policy=str(args.policy),
                review_persona_id=_DEFAULT_REVIEW_PERSONA_ID,
                review_mission_id=_DEFAULT_REVIEW_MISSION_ID,
                review_seed=int(args.seed),
                review_agent_config_override=list(getattr(args, "agent_config_override", []) or []),
                keep_workspace=bool(args.keep_workspace),
                exec_backend=str(args.exec_backend),
                exec_use_host_agent_login=bool(args.exec_use_host_agent_login),
                exec_use_target_sandbox_cli_install=bool(args.exec_use_target_sandbox_cli_install),
                exec_docker_profile=getattr(args, "exec_docker_profile", None),
                exec_keep_container=bool(args.exec_keep_container),
                exec_cache=str(args.exec_cache),
                exec_cache_dir=args.exec_cache_dir,
                maintenance_venv_cache=bool(args.maintenance_venv_cache),
                dry_run=bool(args.dry_run),
            )
        except SystemExit as exc:
            review_error = str(exc)

    handoff_summary = _build_handoff_summary(
        branch=branch,
        commit_requested=bool(args.commit),
        commit_performed=commit_performed,
        push_requested=bool(args.push),
        push_ref=push_ref,
        pr_requested=bool(args.pr),
        pr_ref=pr_ref,
        ci_gate=_read_json(run_dir / "ci_gate.json"),
        review_required=review_required,
        review_run_dir=review_run_dir,
        review_summary=review_summary,
        review_error=review_error,
    )
    _write_json(run_dir / "handoff_summary.json", handoff_summary)

    if result.report_validation_errors:
        print("[implement] WARNING: report validation failed:", file=sys.stderr)
        for err in result.report_validation_errors:
            print(f"  - {err}", file=sys.stderr)
        exit_code = max(exit_code, 2)

    workspace_dir_str = str(workspace_dir) if workspace_dir else "<workspace not kept>"

    # Best-effort git operations: if the user asked for them and they failed, return non-zero and
    # provide a clear remediation path (changes may remain in the kept workspace).
    if args.commit and git_ref is not None and git_ref.get("error"):
        print("[implement] ERROR: git commit step failed:", file=sys.stderr)
        print(f"  {git_ref.get('error')}", file=sys.stderr)
        print(f"  Workspace: {workspace_dir_str}", file=sys.stderr)
        print("  Remediation:", file=sys.stderr)
        print(f"    cd {workspace_dir_str}", file=sys.stderr)
        print("    git status", file=sys.stderr)
        print("    # fix the issue, then retry commit/push/PR manually or rerun this command", file=sys.stderr)
        exit_code = max(exit_code, 3)

    if (args.push or args.pr) and push_ref is not None and push_ref.get("error"):
        print("[implement] ERROR: git push step failed:", file=sys.stderr)
        print(f"  {push_ref.get('error')}", file=sys.stderr)
        print(f"  Workspace: {workspace_dir_str}", file=sys.stderr)
        print("  Remediation:", file=sys.stderr)
        remote = push_ref.get("remote_name") or args.remote_name
        branch = None
        if isinstance(git_ref, dict):
            branch = git_ref.get("branch")
        if not branch:
            branch = args.branch or "<branch>"
        print(f"    cd {workspace_dir_str}", file=sys.stderr)
        print(f"    git push --set-upstream {remote} {branch}", file=sys.stderr)
        exit_code = max(exit_code, 4)

    if args.pr and pr_ref is not None and pr_ref.get("error"):
        print("[implement] ERROR: PR creation failed:", file=sys.stderr)
        print(f"  {pr_ref.get('error')}", file=sys.stderr)
        print(f"  Workspace: {workspace_dir_str}", file=sys.stderr)
        print("  Remediation:", file=sys.stderr)
        print(f"    cd {workspace_dir_str}", file=sys.stderr)
        print("    gh auth status", file=sys.stderr)
        print("    gh pr create --help", file=sys.stderr)
        exit_code = max(exit_code, 5)

    print(str(run_dir))
    return exit_code


def _cmd_run(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)

    selected: SelectedTicket
    if args.ticket_path is not None:
        selected = _select_ticket_from_path(args.ticket_path)
    else:
        selected = _select_ticket_from_export(
            tickets_export_path=args.tickets_export,
            fingerprint=str(args.fingerprint),
        )

    return _run_selected_ticket(args=args, repo_root=repo_root, cfg=cfg, selected=selected)


def _read_review_summary(*, review_run_dir: Path) -> dict[str, Any]:
    summary = _read_json(review_run_dir / "review_summary.json")
    if not isinstance(summary, dict):
        raise SystemExit(f"Missing review_summary.json in {review_run_dir}")
    return summary


def _cmd_review_run(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)
    owner_root = args.owner_root.resolve()
    selected = _select_review_ticket(
        owner_root=owner_root,
        ticket_path=args.ticket_path,
        fingerprint=args.fingerprint,
    )

    ledger_path = _resolve_ledger_path(repo_root=repo_root, raw=args.ledger)
    ledger_entry = _load_ledger_entry(ledger_path=ledger_path, fingerprint=selected.fingerprint)
    run_dir_raw = ledger_entry.get("last_run_dir")
    if not isinstance(run_dir_raw, str) or not run_dir_raw.strip():
        raise SystemExit(
            f"No last_run_dir recorded in ledger for ticket {selected.fingerprint!r}. "
            f"Expected ledger entry in {ledger_path}."
        )
    implementation_run_dir = Path(run_dir_raw)
    review_run_dir, _review_summary = _run_review_for_selected_ticket(
        repo_root=repo_root,
        cfg=cfg,
        owner_root=owner_root,
        selected=selected,
        implementation_run_dir=implementation_run_dir,
        ledger_path=ledger_path,
        review_agent=str(args.agent),
        review_model=args.model,
        review_policy=str(args.policy),
        review_persona_id=str(args.persona_id),
        review_mission_id=str(args.mission_id),
        review_seed=int(args.seed),
        review_agent_config_override=list(getattr(args, "agent_config_override", []) or []),
        keep_workspace=bool(args.keep_workspace),
        exec_backend=str(args.exec_backend),
        exec_use_host_agent_login=bool(args.exec_use_host_agent_login),
        exec_use_target_sandbox_cli_install=bool(args.exec_use_target_sandbox_cli_install),
        exec_docker_profile=getattr(args, "exec_docker_profile", None),
        exec_keep_container=bool(args.exec_keep_container),
        exec_cache=str(args.exec_cache),
        exec_cache_dir=args.exec_cache_dir,
        maintenance_venv_cache=bool(args.maintenance_venv_cache),
        dry_run=bool(args.dry_run),
    )
    if review_run_dir is None:
        return 0
    print(str(review_run_dir))
    return 0


def _cmd_review_status(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    owner_root = args.owner_root.resolve()
    selected = _select_review_ticket(
        owner_root=owner_root,
        ticket_path=args.ticket_path,
        fingerprint=args.fingerprint,
    )
    ledger_path = _resolve_ledger_path(repo_root=repo_root, raw=args.ledger)
    ledger_entry = _load_ledger_entry(ledger_path=ledger_path, fingerprint=selected.fingerprint)
    review_run_dir_raw = ledger_entry.get("last_review_run_dir")
    if not isinstance(review_run_dir_raw, str) or not review_run_dir_raw.strip():
        raise SystemExit(f"No review run recorded in ledger for ticket {selected.fingerprint!r}.")
    review_summary = _read_review_summary(review_run_dir=Path(review_run_dir_raw))
    print(json.dumps(review_summary, indent=2, ensure_ascii=False))
    return 0


def _cmd_review_merge(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    owner_root = args.owner_root.resolve()
    selected = _select_review_ticket(
        owner_root=owner_root,
        ticket_path=args.ticket_path,
        fingerprint=args.fingerprint,
    )
    ledger_path = _resolve_ledger_path(repo_root=repo_root, raw=args.ledger)
    ledger_entry = _load_ledger_entry(ledger_path=ledger_path, fingerprint=selected.fingerprint)
    review_run_dir_raw = ledger_entry.get("last_review_run_dir")
    if not isinstance(review_run_dir_raw, str) or not review_run_dir_raw.strip():
        raise SystemExit(f"No review run recorded in ledger for ticket {selected.fingerprint!r}.")
    review_run_dir = Path(review_run_dir_raw)
    review_summary = _read_review_summary(review_run_dir=review_run_dir)
    pr_url = review_summary.get("pr_url")
    if not isinstance(pr_url, str) or not pr_url.strip():
        raise SystemExit(f"Review summary for {selected.fingerprint!r} is missing pr_url.")
    if review_summary.get("merge_ready") is not True:
        raise SystemExit(
            f"Review summary for {selected.fingerprint!r} is not merge-ready "
            f"(decision={review_summary.get('review_decision')!r}, "
            f"ci_conclusion={review_summary.get('ci_conclusion')!r})."
        )

    pr_context = _collect_pr_review_context(workspace_dir=owner_root, pr_url=pr_url)
    current_merge_ready, current_gate = _current_merge_gate_from_pr_context(pr_context)
    if not current_merge_ready:
        raise SystemExit(
            "Refusing to merge because the current PR gate is not green: "
            f"{json.dumps(current_gate, ensure_ascii=False)}"
        )

    proc = subprocess.run(
        ["gh", "pr", "merge", pr_url, "--merge", "--delete-branch"],
        cwd=str(owner_root),
        capture_output=True,
        text=True,
        check=False,
    )
    merge_ref = {
        "schema_version": 1,
        "pr_url": pr_url,
        "merged": proc.returncode == 0,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": int(proc.returncode),
        "merged_at_utc": _utc_now_z() if proc.returncode == 0 else None,
    }
    _write_json(review_run_dir / "merge_ref.json", merge_ref)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or proc.stdout.strip() or "gh pr merge failed")

    if selected.owner_root is not None:
        move_ticket_file(
            owner_root=selected.owner_root,
            fingerprint=selected.fingerprint,
            to_bucket="5 - complete",
            dry_run=False,
        )
        _sync_ticket_atom_actions(repo_root=repo_root, owner_root=selected.owner_root)
    update_ledger_file(
        ledger_path,
        fingerprint=selected.fingerprint,
        updates={
            "last_merge_pr_url": pr_url,
            "last_merged_at": merge_ref["merged_at_utc"],
        },
    )
    print(pr_url)
    return 0


def _cmd_maintenance_images_list(args: argparse.Namespace) -> int:
    """Print the local maintenance-image inventory as JSON."""

    repo_root = _resolve_repo_root(getattr(args, "repo_root", None))
    payload = list_local_maintenance_images(
        repo_root=repo_root,
        timeout_seconds=_optional_timeout_seconds(args.timeout_seconds),
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _cmd_maintenance_images_cleanup(args: argparse.Namespace) -> int:
    """Prune old local maintenance-image tags using the configured retention policy."""

    repo_root = _resolve_repo_root(getattr(args, "repo_root", None))
    dry_run = args.dry_run
    if dry_run is None:
        dry_run = _load_maintenance_docker_config(repo_root=repo_root).cleanup_dry_run_default
    payload = cleanup_local_maintenance_images(
        repo_root=repo_root,
        timeout_seconds=_optional_timeout_seconds(args.timeout_seconds),
        dry_run=bool(dry_run),
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _cmd_reports_summarize(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)
    runs_dir = args.runs_dir.resolve() if args.runs_dir is not None else cfg.runs_dir
    out_path = (
        args.out.resolve()
        if args.out is not None
        else (runs_dir / "_compiled" / "implementation_metrics.jsonl")
    )
    rows = iter_implementation_rows(
        runs_dir,
        target_slug=args.target,
        repo_input=args.repo_input,
        test_command_regexes=list(args.test_command_regex or []) or None,
    )
    write_jsonl(rows, out_path)
    print(str(out_path))
    return 0


def _cmd_tickets_list(args: argparse.Namespace) -> int:
    owner_root = args.owner_root.resolve()
    index = build_ticket_index(owner_root=owner_root)
    payload = {
        "schema_version": 1,
        "owner_root": str(owner_root),
        "tickets_total": len(index),
        "tickets": [
            {
                "fingerprint": e.fingerprint,
                "paths": [str(p) for p in e.paths],
                "buckets": e.buckets,
                "status": e.status,
            }
            for e in sorted(index.values(), key=lambda x: x.fingerprint)
        ],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _cmd_tickets_next(args: argparse.Namespace) -> int:
    owner_root = args.owner_root.resolve()
    index = build_ticket_index(owner_root=owner_root)
    bucket_priority = list(args.bucket_priority or [])
    if not bucket_priority:
        bucket_priority = ["2 - ready", "1.5 - to_plan", "1 - ideas", "0.5 - to_triage"]
    entry = select_next_ticket(index, bucket_priority=bucket_priority)
    if entry is None:
        print("No tickets found.")
        return 0
    payload = {
        "schema_version": 1,
        "owner_root": str(owner_root),
        "fingerprint": entry.fingerprint,
        "paths": [str(p) for p in entry.paths],
        "buckets": entry.buckets,
        "status": entry.status,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _cmd_tickets_run_next(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)

    owner_root = args.owner_root.resolve()
    if bool(args.refresh_backlog):
        _refresh_backlog_for_ticket_implementation(args=args, repo_root=repo_root)

    index = build_ticket_index(owner_root=owner_root)
    bucket_priority = list(args.bucket_priority or [])
    if not bucket_priority:
        bucket_priority = ["2 - ready", "1.5 - to_plan", "1 - ideas", "0.5 - to_triage"]

    kind_priority = list(args.kind_priority or [])
    if not kind_priority:
        kind_priority = ["implementation"]

    selected = select_next_ticket_path(
        index,
        bucket_priority=bucket_priority,
        kind_priority=kind_priority,
    )
    if selected is None:
        print("No tickets found.")
        return 0

    _, ticket_path = selected
    ticket = _select_ticket_from_path(ticket_path)
    return _run_selected_ticket(args=args, repo_root=repo_root, cfg=cfg, selected=ticket)


def _cmd_tickets_move(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    owner_root = args.owner_root.resolve()
    if str(args.to_bucket) == DISCARDED_PLAN_BUCKET:
        (owner_root / ".agents" / "plans" / DISCARDED_PLAN_BUCKET).mkdir(
            parents=True,
            exist_ok=True,
        )
    dest = move_ticket_file(
        owner_root=owner_root,
        fingerprint=str(args.fingerprint),
        to_bucket=str(args.to_bucket),
        dry_run=bool(args.dry_run),
    )
    if not bool(args.dry_run):
        _sync_ticket_atom_actions(repo_root=repo_root, owner_root=owner_root)
    print(str(dest))
    return 0


def _cmd_tickets_discard(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    owner_root = args.owner_root.resolve()
    fingerprint = str(args.fingerprint).strip().lower()
    discarded_at = _utc_now_z()
    discard_dir = owner_root / ".agents" / "plans" / DISCARDED_PLAN_BUCKET
    discard_dir.mkdir(parents=True, exist_ok=True)

    dest = move_ticket_file(
        owner_root=owner_root,
        fingerprint=fingerprint,
        to_bucket=DISCARDED_PLAN_BUCKET,
        dry_run=False,
    )

    actions_path = args.actions_yaml or _default_backlog_actions_path(repo_root)
    actions = load_backlog_actions_yaml(actions_path)
    entry = dict(actions.get(fingerprint) or {})
    entry.update(
        {
            "fingerprint": fingerprint,
            "status": "discarded",
            "discard_reason": str(args.reason),
            "discarded_at": discarded_at,
            "discarded_path": str(dest),
            "owner_root": str(owner_root),
        }
    )
    if args.note:
        entry["discard_note"] = str(args.note)
    actions[fingerprint] = entry
    write_backlog_actions_yaml(actions_path, actions)

    atom_sync = _sync_ticket_atom_actions(
        repo_root=repo_root,
        owner_root=owner_root,
        atom_actions_path=args.atom_actions_yaml,
        discard_fingerprint=fingerprint,
        discard_reason=str(args.reason),
        discard_note=str(args.note) if args.note else None,
        discarded_path=dest,
        discarded_at=discarded_at,
    )
    payload = {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "discard_reason": str(args.reason),
        "discarded_path": str(dest),
        "actions_yaml": str(actions_path),
        "atom_actions_yaml": str(args.atom_actions_yaml or _default_atom_actions_path(repo_root)),
        "atom_sync": atom_sync,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _add_settings_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--settings",
        type=Path,
        help=(
            "Optional settings YAML path. Defaults to "
            f"<repo_root>/configs/{_SETTINGS_FILENAME} when present."
        ),
    )
    parser.add_argument(
        "--settings-profile",
        help="Optional settings profile name from the settings YAML.",
    )


def _add_run_execution_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="Override target repo input (path or git URL).")
    parser.add_argument("--ref", help="Optional git ref to checkout in the acquired workspace.")

    parser.add_argument("--agent", choices=["claude", "codex", "gemini"], default="codex")
    parser.add_argument("--model", help="Optional model override.")
    parser.add_argument("--policy", default="write")
    parser.add_argument("--persona-id", dest="persona_id", default=_DEFAULT_PERSONA_ID)
    parser.add_argument("--mission-id", dest="mission_id", default=_DEFAULT_MISSION_ID)
    parser.add_argument(
        "--implementation-review-agent",
        dest="implementation_review_agent",
        choices=["claude", "codex", "gemini"],
        help=(
            "Agent CLI used for the automatic post-implementation review after PR creation "
            "(default: settings value, otherwise the implementation agent)."
        ),
    )
    parser.add_argument(
        "--implementation-review-model",
        dest="implementation_review_model",
        help="Optional model override for the automatic post-implementation review.",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--agent-config-override",
        action="append",
        default=[],
        help="Repeatable agent config override strings.",
    )
    parser.add_argument("--keep-workspace", action="store_true", help="Keep workspace directory after run.")

    exec_backend_group = parser.add_mutually_exclusive_group()
    exec_backend_group.add_argument(
        "--exec-backend",
        choices=["docker", "local"],
        default="docker",
        help="Execution backend (default: docker).",
    )
    exec_backend_group.add_argument(
        "--no-docker",
        dest="exec_backend",
        action="store_const",
        const="local",
        help="Opt out of Docker sandboxing (exec_backend=local).",
    )
    run_auth_group = parser.add_mutually_exclusive_group()
    run_auth_group.add_argument(
        "--exec-use-host-agent-login",
        dest="exec_use_host_agent_login",
        action="store_true",
        default=True,
    )
    run_auth_group.add_argument(
        "--exec-use-api-key-auth",
        dest="exec_use_host_agent_login",
        action="store_false",
    )
    parser.add_argument("--exec-use-target-sandbox-cli-install", action="store_true", default=False)
    parser.add_argument(
        "--exec-docker-profile",
        choices=["standard", "maintenance"],
        help=(
            "Docker execution profile. Defaults to maintenance for same-repo maintenance targets "
            "and standard otherwise."
        ),
    )
    parser.add_argument(
        "--exec-keep-container",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep Docker container after the run (default: enabled).",
    )

    parser.add_argument(
        "--exec-cache",
        choices=["cold", "warm"],
        default="warm",
        help=(
            "Docker sandbox cache mode (default: warm). "
            "warm: mount a host directory at /cache (persists across runs; used for pip + PDM caches). "
            "cold: do not mount a persistent host cache (/cache is per-container and discarded)."
        ),
    )
    parser.add_argument(
        "--exec-cache-dir",
        type=Path,
        help=(
            "Host directory mounted at /cache when --exec-cache warm. "
            "Defaults to <repo_root>/runs/_cache/usertest_implement."
        ),
    )
    parser.add_argument(
        "--maintenance-venv-cache",
        dest="maintenance_venv_cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Enable Docker maintenance venv cache reuse for scaffold install tasks when --exec-cache warm "
            "(default: enabled). Use --no-maintenance-venv-cache to force full reinstalls."
        ),
    )

    parser.add_argument("--dry-run", action="store_true")

    parser.add_argument(
        "--verify-command",
        action="append",
        dest="verification_commands",
        default=[],
        help=(
            "Repeatable verification command gate that must pass before handing off "
            "(default: run scripts/smoke.{ps1,sh} then scaffold install/lint/test across the repo "
            "(tools/scaffold/scaffold.py run --all ...) "
            "when --commit/--push/--pr)."
        ),
    )
    parser.add_argument(
        "--verify-timeout-seconds",
        type=float,
        default=None,
        help=(
            "Optional per-command timeout for --verify-command. "
            "When brokered final verification reuse is active, omitted or non-positive "
            "values fall back to the runner's bounded default timeout."
        ),
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Disable default verification gate (useful for debugging).",
    )
    parser.add_argument(
        "--verify-reuse",
        choices=["auto", "off"],
        default="auto",
        help=(
            "Reuse a runner-owned in-session final verification pass when available (default: auto). "
            "Use --verify-reuse off to force a separate post-agent rerun."
        ),
    )
    parser.add_argument(
        "--ci-timeout-seconds",
        type=float,
        default=None,
        help="Optional timeout waiting for GitHub Actions CI before creating a PR.",
    )
    parser.add_argument(
        "--skip-ci-wait",
        action="store_true",
        help="Skip waiting for GitHub Actions CI before creating a PR (not recommended).",
    )
    parser.add_argument(
        "--draft-pr-on-ci-failure",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="If CI does not pass, create a draft PR instead of failing PR creation (default: enabled).",
    )

    parser.add_argument(
        "--commit",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Create branch + commit changes in kept workspace.",
    )
    parser.add_argument("--branch", help="Branch name override.")
    parser.add_argument("--commit-message", dest="commit_message", help="Commit message override.")
    parser.add_argument(
        "--git-user-name",
        dest="git_user_name",
        help="Git user.name used for commits (default: usertest-implement).",
    )
    parser.add_argument(
        "--git-user-email",
        dest="git_user_email",
        help="Git user.email used for commits (default: usertest-implement@local).",
    )

    parser.add_argument(
        "--push",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Push branch to remote.",
    )
    parser.add_argument("--remote-name", default="origin")
    parser.add_argument("--remote-url")
    parser.add_argument("--force-push", dest="force_push", action="store_true")
    parser.add_argument(
        "--base-branch",
        default="dev",
        help="Base branch for PR creation (default: dev).",
    )
    parser.add_argument(
        "--pr",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Best-effort PR creation via gh.",
    )

    parser.add_argument(
        "--move-on-start",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Move ticket file to 3 - in_progress if possible (default: enabled).",
    )
    parser.add_argument(
        "--move-on-commit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Move ticket file to 4 - for_review after --commit (default: enabled).",
    )
    parser.add_argument(
        "--ledger",
        nargs="?",
        const=_DEFAULT_LEDGER_PATH,
        type=Path,
        help=(
            "Optional attempt ledger YAML. If provided without a value, defaults to "
            "<repo_root>/.agents/state/backlog_implement_actions.yaml."
        ),
    )


def _add_review_execution_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--agent", choices=["claude", "codex", "gemini"], default="codex")
    parser.add_argument("--model", help="Optional model override.")
    parser.add_argument("--policy", default="write")
    parser.add_argument("--persona-id", dest="persona_id", default=_DEFAULT_REVIEW_PERSONA_ID)
    parser.add_argument("--mission-id", dest="mission_id", default=_DEFAULT_REVIEW_MISSION_ID)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--agent-config-override",
        action="append",
        default=[],
        help="Repeatable agent config override strings.",
    )
    parser.add_argument("--keep-workspace", action="store_true", help="Keep workspace directory after run.")

    exec_backend_group = parser.add_mutually_exclusive_group()
    exec_backend_group.add_argument(
        "--exec-backend",
        choices=["docker", "local"],
        default="docker",
        help="Execution backend (default: docker).",
    )
    exec_backend_group.add_argument(
        "--no-docker",
        dest="exec_backend",
        action="store_const",
        const="local",
        help="Opt out of Docker sandboxing (exec_backend=local).",
    )
    run_auth_group = parser.add_mutually_exclusive_group()
    run_auth_group.add_argument(
        "--exec-use-host-agent-login",
        dest="exec_use_host_agent_login",
        action="store_true",
        default=True,
    )
    run_auth_group.add_argument(
        "--exec-use-api-key-auth",
        dest="exec_use_host_agent_login",
        action="store_false",
    )
    parser.add_argument("--exec-use-target-sandbox-cli-install", action="store_true", default=False)
    parser.add_argument(
        "--exec-docker-profile",
        choices=["standard", "maintenance"],
        help=(
            "Docker execution profile. Defaults to maintenance for same-repo maintenance targets "
            "and standard otherwise."
        ),
    )
    parser.add_argument(
        "--exec-keep-container",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep Docker container after the run (default: enabled).",
    )
    parser.add_argument(
        "--exec-cache",
        choices=["cold", "warm"],
        default="warm",
        help="Docker sandbox cache mode (default: warm).",
    )
    parser.add_argument(
        "--exec-cache-dir",
        type=Path,
        help="Host directory mounted at /cache when --exec-cache warm.",
    )
    parser.add_argument(
        "--maintenance-venv-cache",
        dest="maintenance_venv_cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable Docker maintenance venv cache reuse (default: enabled).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--ledger",
        nargs="?",
        const=_DEFAULT_LEDGER_PATH,
        type=Path,
        help=(
            "Optional attempt ledger YAML. If provided without a value, defaults to "
            "<repo_root>/.agents/state/backlog_implement_actions.yaml."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="usertest-implement")
    parser.add_argument("--repo-root", type=Path, help="Path to the usertest runner repo root.")

    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run one ticket implementation.")
    ticket_group = run_p.add_mutually_exclusive_group(required=True)
    ticket_group.add_argument(
        "--tickets-export",
        dest="tickets_export",
        type=Path,
        help="Tickets export JSON path.",
    )
    ticket_group.add_argument("--ticket-path", dest="ticket_path", type=Path, help="Ticket markdown path.")
    run_p.add_argument(
        "--fingerprint",
        required=False,
        help="Ticket fingerprint selector (requires --tickets-export).",
    )
    _add_settings_args(run_p)
    _add_run_execution_args(run_p)

    run_p.set_defaults(func=_cmd_run)

    review_p = sub.add_parser("review", help="Review and merge PR-backed implementation tickets.")
    review_sub = review_p.add_subparsers(dest="review_cmd", required=True)

    review_run_p = review_sub.add_parser("run", help="Run an implementation review for a PR-backed ticket.")
    review_run_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    review_run_group = review_run_p.add_mutually_exclusive_group(required=True)
    review_run_group.add_argument("--ticket-path", dest="ticket_path", type=Path)
    review_run_group.add_argument("--fingerprint")
    _add_review_execution_args(review_run_p)
    review_run_p.set_defaults(func=_cmd_review_run)

    review_status_p = review_sub.add_parser("status", help="Show the latest review summary for a ticket.")
    review_status_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    review_status_group = review_status_p.add_mutually_exclusive_group(required=True)
    review_status_group.add_argument("--ticket-path", dest="ticket_path", type=Path)
    review_status_group.add_argument("--fingerprint")
    review_status_p.add_argument(
        "--ledger",
        nargs="?",
        const=_DEFAULT_LEDGER_PATH,
        type=Path,
        help=(
            "Optional attempt ledger YAML. If provided without a value, defaults to "
            "<repo_root>/.agents/state/backlog_implement_actions.yaml."
        ),
    )
    review_status_p.set_defaults(func=_cmd_review_status)

    review_merge_p = review_sub.add_parser("merge", help="Merge a reviewed PR when review + CI are green.")
    review_merge_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    review_merge_group = review_merge_p.add_mutually_exclusive_group(required=True)
    review_merge_group.add_argument("--ticket-path", dest="ticket_path", type=Path)
    review_merge_group.add_argument("--fingerprint")
    review_merge_p.add_argument(
        "--ledger",
        nargs="?",
        const=_DEFAULT_LEDGER_PATH,
        type=Path,
        help=(
            "Optional attempt ledger YAML. If provided without a value, defaults to "
            "<repo_root>/.agents/state/backlog_implement_actions.yaml."
        ),
    )
    review_merge_p.set_defaults(func=_cmd_review_merge)

    maintenance_images_p = sub.add_parser(
        "maintenance-images",
        help="Inspect and prune local maintenance-image tags.",
    )
    maintenance_images_sub = maintenance_images_p.add_subparsers(
        dest="maintenance_images_cmd",
        required=True,
    )

    maintenance_images_list_p = maintenance_images_sub.add_parser(
        "list",
        help="List local maintenance-image tags retained on the Docker host.",
    )
    maintenance_images_list_p.add_argument(
        "--timeout-seconds",
        type=float,
        default=None,
        help="Timeout for Docker inventory commands.",
    )
    maintenance_images_list_p.set_defaults(func=_cmd_maintenance_images_list)

    maintenance_images_cleanup_p = maintenance_images_sub.add_parser(
        "cleanup",
        help="Prune old local maintenance-image tags using the configured retention policy.",
    )
    maintenance_images_cleanup_p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=None,
        help="Show what would be deleted without deleting it.",
    )
    maintenance_images_cleanup_p.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="Delete tags selected by the retention policy.",
    )
    maintenance_images_cleanup_p.add_argument(
        "--timeout-seconds",
        type=float,
        default=None,
        help="Timeout for Docker cleanup commands.",
    )
    maintenance_images_cleanup_p.set_defaults(func=_cmd_maintenance_images_cleanup)

    reports_p = sub.add_parser("reports", help="Reporting utilities.")
    reports_sub = reports_p.add_subparsers(dest="reports_cmd", required=True)
    summarize_p = reports_sub.add_parser("summarize", help="Summarize implementation runs into JSONL.")
    summarize_p.add_argument("--runs-dir", type=Path, help="Runs directory (default: runs/usertest_implement).")
    summarize_p.add_argument("--out", type=Path, help="Output JSONL path.")
    summarize_p.add_argument("--target", help="Optional target slug filter.")
    summarize_p.add_argument("--repo-input", help="Optional repo_input filter.")
    summarize_p.add_argument(
        "--test-command-regex",
        action="append",
        default=[],
        help="Override/extend test command regex patterns.",
    )
    summarize_p.set_defaults(func=_cmd_reports_summarize)

    tickets_p = sub.add_parser("tickets", help="Local ticket queue helpers (from .agents/plans).")
    tickets_sub = tickets_p.add_subparsers(dest="tickets_cmd", required=True)

    tickets_list_p = tickets_sub.add_parser("list", help="List tickets in .agents/plans.")
    tickets_list_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    tickets_list_p.set_defaults(func=_cmd_tickets_list)

    tickets_next_p = tickets_sub.add_parser("next", help="Select the next ticket by bucket priority.")
    tickets_next_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    tickets_next_p.add_argument("--bucket-priority", action="append", default=[])
    tickets_next_p.set_defaults(func=_cmd_tickets_next)

    tickets_run_next_p = tickets_sub.add_parser(
        "run-next",
        help=(
            "Refresh the backlog + ticket exports, then implement the next local plan ticket "
            "(implementation-only by default; commits/pushes/opens a PR unless disabled)."
        ),
    )
    tickets_run_next_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    tickets_run_next_p.add_argument("--bucket-priority", action="append", default=[])
    tickets_run_next_p.add_argument(
        "--kind-priority",
        action="append",
        default=[],
        help=(
            "Ticket kind ordering derived from markdown (repeatable). "
            "Defaults to: implementation."
        ),
    )
    tickets_run_next_p.add_argument(
        "--no-refresh-backlog",
        action="store_false",
        dest="refresh_backlog",
        default=True,
        help="Skip running usertest-backlog refresh steps before selecting the next ticket.",
    )
    tickets_run_next_p.add_argument("--backlog-target", help="Target slug for usertest-backlog refresh.")
    tickets_run_next_p.add_argument(
        "--backlog-runs-dir",
        type=Path,
        help="Runs directory for usertest-backlog refresh (default: <repo_root>/runs/usertest).",
    )
    tickets_run_next_p.add_argument(
        "--backlog-agent",
        choices=["claude", "codex", "gemini"],
        help="Agent CLI used for `usertest-backlog reports backlog`.",
    )
    tickets_run_next_p.add_argument(
        "--backlog-model",
        help="Optional model override for `usertest-backlog reports backlog`.",
    )
    tickets_run_next_p.add_argument(
        "--review-agent",
        choices=["claude", "codex", "gemini"],
        help="Agent CLI used for `usertest-backlog reports review-ux` (default: --backlog-agent).",
    )
    tickets_run_next_p.add_argument(
        "--review-model",
        help="Optional model override for `usertest-backlog reports review-ux`.",
    )
    _add_settings_args(tickets_run_next_p)
    _add_run_execution_args(tickets_run_next_p)
    tickets_run_next_p.set_defaults(commit=True, push=True, pr=True)
    tickets_run_next_p.set_defaults(func=_cmd_tickets_run_next)

    tickets_move_p = tickets_sub.add_parser("move", help="Move a ticket file between plan buckets.")
    tickets_move_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    tickets_move_p.add_argument("--fingerprint", required=True)
    tickets_move_p.add_argument("--to-bucket", required=True)
    tickets_move_p.add_argument("--dry-run", action="store_true")
    tickets_move_p.set_defaults(func=_cmd_tickets_move)

    tickets_discard_p = tickets_sub.add_parser(
        "discard",
        help="Move a generated ticket to the non-actioned discarded bucket.",
    )
    tickets_discard_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    tickets_discard_p.add_argument("--fingerprint", required=True)
    tickets_discard_p.add_argument(
        "--reason",
        required=True,
        choices=["bad_solution", "duplicate", "obsolete", "not_repro", "other"],
    )
    tickets_discard_p.add_argument("--note")
    tickets_discard_p.add_argument(
        "--actions-yaml",
        type=Path,
        help="Backlog action ledger path (defaults to configs/backlog_actions.yaml).",
    )
    tickets_discard_p.add_argument(
        "--atom-actions-yaml",
        type=Path,
        help="Atom action ledger path (defaults to configs/backlog_atom_actions.yaml).",
    )
    tickets_discard_p.set_defaults(func=_cmd_tickets_discard)

    add_batch_subcommands(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(raw_argv)

    _apply_cli_settings(args=args, parser=parser, argv=raw_argv)

    if args.cmd == "run":
        if args.ticket_path is None:
            if args.tickets_export is None:
                raise SystemExit(2)
            if not args.fingerprint:
                raise SystemExit("Provide --fingerprint with --tickets-export.")
        raise SystemExit(args.func(args))

    if args.cmd == "review":
        raise SystemExit(args.func(args))

    raise SystemExit(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
