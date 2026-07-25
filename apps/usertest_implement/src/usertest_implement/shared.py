#!/usr/bin/env python
# ruff: noqa: E501,F401,F403,F405
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
    "powershell -NoProfile -ExecutionPolicy Bypass -File ./scripts/offline_first_success.ps1"
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


try:
    from runner_core import (
        RunnerConfig,
        RunRequest,
        find_repo_root,
        run_once,
        verification_command_safety_errors,
    )
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
        validate_outcome_record,
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
    case_id: str | None = None
    plan_revision_id: str | None = None
    case_lifecycle_id: str | None = None
    ticket_body_sha256: str | None = None
    local_plan_sha256: str | None = None
    verification_contract_sha256: str | None = None
    target_contract_sha256: str | None = None


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
    "exec_maintenance_image_metadata_path": _SettingsValueSpec("path", allow_none=True),
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
    resolved_atom_actions_path = atom_actions_path or _default_atom_actions_path(owner_root)
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


def _load_runner_config(
    repo_root: Path,
    *,
    runs_dir: Path | None = None,
) -> RunnerConfig:
    agents_cfg = _load_yaml(repo_root / "configs" / "agents.yaml").get("agents", {})
    policies_cfg = _load_yaml(repo_root / "configs" / "policies.yaml").get("policies", {})
    if not isinstance(agents_cfg, dict) or not isinstance(policies_cfg, dict):
        raise ValueError("Invalid configs under configs/.")
    return RunnerConfig(
        repo_root=repo_root,
        runs_dir=(runs_dir or (repo_root / "runs" / "usertest_implement")).resolve(),
        agents=agents_cfg,
        policies=policies_cfg,
    )



def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _resolve_ledger_path(*, repo_root: Path, raw: Path | None) -> Path:
    ledger_path = raw if raw is not None else _DEFAULT_LEDGER_PATH
    if ledger_path.is_absolute():
        return ledger_path.resolve()
    return (repo_root / ledger_path).resolve()




__all__ = [name for name in globals() if not name.startswith("__")]
