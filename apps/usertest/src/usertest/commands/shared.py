# ruff: noqa: E501
from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path
from typing import Any

_WINDOWS_OFFLINE_FIRST_SUCCESS_CMD = (
    "powershell -NoProfile -ExecutionPolicy Bypass -File ./scripts/offline_first_success.ps1"
)
_POSIX_OFFLINE_FIRST_SUCCESS_CMD = "bash ./scripts/offline_first_success.sh"

_EXEC_NETWORK_HELP = (
    "Docker sandbox container runtime network mode (maps to `docker run --network ...`). "
    "Only applies when `--exec-backend docker`. "
    "`none` disables outbound network for the container, but it is NOT an end-to-end offline/privacy mode: "
    "`docker build` may still pull base images and download dependencies, and any host-side steps "
    "(e.g., when using `--exec-backend local`) are unaffected. "
    "If the agent CLI runs inside the container, `none` also prevents hosted agent CLIs "
    "(codex/claude/gemini) from reaching their APIs."
)

_EXEC_CACHE_HELP = (
    "Docker sandbox cache mode (default: cold). "
    "Only applies when `--exec-backend docker`. "
    "cold: do not mount a persistent host cache (/cache is per-container and discarded). "
    "warm: mount a host directory at /cache (persists across runs; used for pip + PDM caches)."
)

_EXEC_CACHE_DIR_HELP = (
    "Host directory mounted at /cache when `--exec-cache warm` "
    "(default: <repo_root>/runs/_cache/usertest)."
)

_LEGACY_RUN_TIMESTAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
_WINDOWS_ABS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_SENSITIVE_KV_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|pwd)", re.IGNORECASE
)

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

def _missing_dependency_remediation_simple(*, dependency: str) -> str:
    return (
        f"Missing dependency `{dependency}`.\n"
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
    from runner_core import RunnerConfig, RunRequest, find_repo_root
except ModuleNotFoundError as exc:
    raise SystemExit(_from_source_import_remediation(missing_module="runner_core")) from exc

def _enable_console_backslashreplace(stream: Any) -> None:
    """Configure stream error handling to backslash escapes when supported."""
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
    """Configure stdout and stderr for resilient console output."""
    _enable_console_backslashreplace(sys.stdout)
    _enable_console_backslashreplace(sys.stderr)

def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML mapping from disk."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {path}, got {type(data).__name__}")
    return data

def _load_runner_config(repo_root: Path) -> RunnerConfig:
    """Load runner configuration from repository config files."""
    agents_cfg = _load_yaml(repo_root / "configs" / "agents.yaml").get("agents", {})
    policies_cfg = _load_yaml(repo_root / "configs" / "policies.yaml").get("policies", {})
    if not isinstance(agents_cfg, dict) or not isinstance(policies_cfg, dict):
        raise ValueError("Invalid configs under configs/.")
    return RunnerConfig(
        repo_root=repo_root,
        runs_dir=repo_root / "runs" / "usertest",
        agents=agents_cfg,
        policies=policies_cfg,
    )

def _looks_like_local_repo_input(repo: str) -> bool:
    """Return whether the repo input looks like a local filesystem path."""
    raw = repo.strip()
    if not raw:
        return False
    if raw.startswith(("http://", "https://", "git@")):
        return False
    if raw.startswith(("pip:", "pdm:")):
        return False
    if _WINDOWS_ABS_PATH_RE.match(raw):
        return True
    if raw.startswith(("\\\\", "/", "./", "../", ".\\", "..\\", "~")):
        return True
    return ("\\" in raw) or ("/" in raw)

def _resolve_local_repo_root(repo_root: Path, repo: str) -> Path | None:
    """Resolve a repo input to a local repository root when possible."""
    try:
        candidate = Path(repo).expanduser()
    except OSError:
        return None
    if candidate.exists():
        try:
            return candidate.resolve()
        except OSError:
            return candidate
    if not candidate.is_absolute():
        alt = (repo_root / candidate).expanduser()
        if alt.exists():
            try:
                return alt.resolve()
            except OSError:
                return alt
    return None

def _redact_kv_list(items: list[str], *, redacted_value: str = "<redacted>") -> list[str]:
    redacted: list[str] = []
    for item in items:
        if not isinstance(item, str):
            redacted.append(str(item))
            continue
        if "=" not in item:
            redacted.append(item)
            continue
        key, value = item.split("=", 1)
        key_stripped = key.strip()
        value_stripped = value.strip()
        if not key_stripped or not value_stripped:
            redacted.append(item)
            continue
        if _SENSITIVE_KV_KEY_RE.search(key_stripped):
            redacted.append(f"{key_stripped}={redacted_value}")
        else:
            redacted.append(item)
    return redacted

def _jsonify(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonify(v) for v in value]
    if isinstance(value, list):
        return [_jsonify(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    return str(value)

def _serialize_run_request_for_print(req: RunRequest) -> dict[str, Any]:
    rendered: dict[str, Any] = {}
    for field in dataclasses.fields(RunRequest):
        rendered[field.name] = _jsonify(getattr(req, field.name))
    exec_env = rendered.get("exec_env")
    if isinstance(exec_env, list):
        rendered["exec_env"] = _redact_kv_list([str(x) for x in exec_env])
    agent_config_overrides = rendered.get("agent_config_overrides")
    if isinstance(agent_config_overrides, list):
        rendered["agent_config_overrides"] = _redact_kv_list(
            [str(x) for x in agent_config_overrides]
        )
    return rendered

def _resolve_repo_root(arg: Path | None) -> Path:
    """Resolve the monorepo root from CLI input or discovery."""
    if arg is not None:
        return arg.resolve()
    return find_repo_root()

def _resolve_optional_path(repo_root: Path, arg: Path | None) -> Path | None:
    """Resolve an optional path argument relative to the repository root."""
    if arg is None:
        return None
    path = arg
    if not path.is_absolute() and not path.exists():
        path = repo_root / path
    return path.resolve()

def _default_builtin_sandbox_cli_context(repo_root: Path) -> Path:
    """Resolve the built-in sandbox_cli Docker context from source or package layout."""
    candidates = (
        repo_root
        / "packages"
        / "sandbox_runner"
        / "builtins"
        / "docker"
        / "contexts"
        / "sandbox_cli",
        repo_root
        / "packages"
        / "sandbox_runner"
        / "src"
        / "sandbox_runner"
        / "builtins"
        / "docker"
        / "contexts"
        / "sandbox_cli",
    )
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists() and resolved.is_dir():
            return resolved
    return candidates[0].resolve()

def _coerce_string(value: Any) -> str | None:
    """Return a stripped non-empty string value when possible."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None

def _coerce_string_list(value: Any) -> list[str]:
    """Return a list of stripped non-empty string values."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out

def _looks_like_run_timestamp_dirname(name: str) -> bool:
    """
    Check whether `name` looks like a UTC run timestamp directory.

    Format: YYYYMMDDTHHMMSSZ (e.g., 20260126T183234Z)
    """

    return bool(_LEGACY_RUN_TIMESTAMP_RE.match(name))

def _looks_like_legacy_target_runs_dir(path: Path) -> bool:
    """
    Heuristic for detecting a legacy `runs/<target>/...` directory.

    The legacy layout uses `runs/<target>/<timestamp>/<agent>/<seed>/...` where `timestamp`
    is the compact UTC form YYYYMMDDTHHMMSSZ.
    """

    if not path.exists() or not path.is_dir():
        return False

    try:
        for child in path.iterdir():
            if child.is_dir() and _looks_like_run_timestamp_dirname(child.name):
                return True
    except OSError:
        return False
    return False

def _warn_legacy_runs_layout(repo_root: Path) -> None:
    """
    Warn (to stderr) when legacy run output directories are present.

    This does not move anything automatically. It only nudges the user to run the explicit
    migration script.
    """

    legacy_app_local = repo_root / "usertest" / "runs"
    legacy_root_runs = repo_root / "runs"

    has_legacy = False
    legacy_notes: list[str] = []

    if legacy_app_local.exists() and legacy_app_local.is_dir():
        try:
            if any(True for _ in legacy_app_local.iterdir()):
                has_legacy = True
                legacy_notes.append(f"- legacy dir present: {legacy_app_local}")
        except OSError:
            has_legacy = True
            legacy_notes.append(f"- legacy dir present (unreadable): {legacy_app_local}")

    if legacy_root_runs.exists() and legacy_root_runs.is_dir():
        try:
            for child in legacy_root_runs.iterdir():
                if not child.is_dir():
                    continue
                if child.name in {"usertest", "_cache"}:
                    continue
                if child.name == "_workspaces" or _looks_like_legacy_target_runs_dir(child):
                    has_legacy = True
                    legacy_notes.append(f"- legacy dir present: {child}")
        except OSError:
            # If we can't inspect, keep this quiet to avoid spamming unrelated commands.
            pass

    if not has_legacy:
        return

    print(
        "WARNING: Legacy run layout detected. New runs go to runs/usertest/.\n"
        "To migrate existing runs (dry-run by default):\n"
        "  python tools/migrations/migrate_runs_layout.py\n"
        "To apply moves:\n"
        "  python tools/migrations/migrate_runs_layout.py --apply\n"
        "Detected:\n" + "\n".join(legacy_notes),
        file=sys.stderr,
    )

