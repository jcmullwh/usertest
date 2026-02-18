from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from json import JSONDecoder
from pathlib import Path
from typing import Any

from agent_adapters import (
    normalize_claude_events,
    normalize_codex_events,
    normalize_gemini_events,
    run_claude_print,
    run_codex_exec,
    run_gemini,
    validate_codex_personality_config_overrides,
    validate_codex_reasoning_effort_config_overrides,
)
from agent_adapters.codex_config import toml_basic_string
from normalized_events import iter_events_jsonl, make_event
from reporter import (
    compute_metrics,
    render_report_markdown,
    validate_report,
)
from sandbox_runner.diagnostics import (
    capture_container_artifacts,
    capture_dns_snapshot,
    probe_commands_in_container,
)

from runner_core.agent_docs import obfuscate_target_agent_docs
from runner_core.catalog import load_catalog_config
from runner_core.execution_backend import prepare_execution_backend
from runner_core.pathing import slugify, utc_timestamp_compact
from runner_core.pip_bootstrap import (
    PipBootstrapResult,
    bootstrap_pip_requirements,
)
from runner_core.python_interpreter_probe import probe_python_interpreters
from runner_core.pip_target import (
    is_pip_repo_input,
    parse_pip_repo_input,
)
from runner_core.pip_target import (
    requirements_path as pip_requirements_path,
)
from runner_core.prompt import TemplateSubstitutionError, build_prompt_from_template
from runner_core.run_spec import resolve_effective_run_inputs
from runner_core.target_acquire import acquire_target


@dataclass(frozen=True)
class RunnerConfig:
    repo_root: Path
    runs_dir: Path
    agents: dict[str, Any]
    policies: dict[str, Any]


@dataclass(frozen=True)
class RunRequest:
    repo: str
    ref: str | None = None
    agent: str = "codex"
    policy: str = "write"
    persona_id: str | None = None
    mission_id: str | None = None
    obfuscate_agent_docs: bool = False
    seed: int = 0
    model: str | None = None
    agent_config_overrides: tuple[str, ...] = ()
    agent_system_prompt_file: Path | None = None
    agent_append_system_prompt: str | None = None
    agent_append_system_prompt_file: Path | None = None
    keep_workspace: bool = False
    preflight_commands: tuple[str, ...] = ()
    preflight_required_commands: tuple[str, ...] = ()

    exec_backend: str = "local"
    exec_docker_context: Path | None = None
    exec_dockerfile: Path | None = None
    exec_docker_python: str = "auto"
    exec_docker_timeout_seconds: float | None = None
    exec_use_target_sandbox_cli_install: bool = False
    exec_use_host_agent_login: bool = True
    exec_network: str = "open"
    exec_cache: str = "cold"
    exec_cache_dir: Path | None = None
    exec_env: tuple[str, ...] = ()
    exec_keep_container: bool = False
    exec_rebuild_image: bool = False
    agent_rate_limit_retries: int = 2
    agent_rate_limit_backoff_seconds: float = 1.0
    agent_rate_limit_backoff_multiplier: float = 2.0
    agent_followup_attempts: int = 2


@dataclass(frozen=True)
class RunResult:
    run_dir: Path
    exit_code: int
    report_validation_errors: list[str]


_BASE_PREFLIGHT_COMMANDS = [
    "git",
    "rg",
    "python3",
    "python",
    "pip",
    "pip3",
    "pdm",
    "node",
    "npm",
    # Common package managers / installers (useful for dependency bootstrapping).
    "apt-get",
    "apk",
    "dnf",
    "yum",
    "pacman",
    "brew",
    "choco",
    "winget",
    "scoop",
]

_FAILURE_SUBTYPE_RULES: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "codex_model_messages_missing",
        (
            re.compile(r"codex_model_messages_missing", re.IGNORECASE),
            re.compile(r"model personality requested but model_messages is missing", re.IGNORECASE),
            re.compile(r"\bmodel_messages\b.*\bmissing\b", re.IGNORECASE),
        ),
    ),
    (
        "invalid_agent_config",
        (
            re.compile(r"invalid value.*model_reasoning_effort", re.IGNORECASE),
            re.compile(r"model_reasoning_effort.*\b(enum|expected|invalid)\b", re.IGNORECASE),
        ),
    ),
    (
        "provider_capacity",
        (
            re.compile(r"\b429\b", re.IGNORECASE),
            re.compile(r"resource_exhausted", re.IGNORECASE),
            re.compile(r"model_capacity_exhausted", re.IGNORECASE),
            re.compile(r"no capacity available", re.IGNORECASE),
            re.compile(r"exhausted your capacity", re.IGNORECASE),
            re.compile(r"hit your limit", re.IGNORECASE),
            re.compile(r"rate[_ -]?limit", re.IGNORECASE),
            re.compile(r"too many requests", re.IGNORECASE),
            re.compile(r"\bquota\b", re.IGNORECASE),
        ),
    ),
    (
        "provider_auth",
        (
            re.compile(r"\b401\b", re.IGNORECASE),
            re.compile(r"\bunauthorized\b", re.IGNORECASE),
            re.compile(r"invalid api key", re.IGNORECASE),
            re.compile(r"incorrect api key", re.IGNORECASE),
            re.compile(r"authentication failed", re.IGNORECASE),
        ),
    ),
    (
        "disk_full",
        (
            re.compile(r"\bENOSPC\b", re.IGNORECASE),
            re.compile(r"no space left on device", re.IGNORECASE),
            re.compile(r"disk quota exceeded", re.IGNORECASE),
        ),
    ),
    (
        "permission_policy",
        (
            re.compile(r"interactive approval", re.IGNORECASE),
            re.compile(r"apply_patch_approval_request", re.IGNORECASE),
            re.compile(r"denied by policy", re.IGNORECASE),
            re.compile(r"permission mode", re.IGNORECASE),
            re.compile(r"outside the allowed workspace", re.IGNORECASE),
        ),
    ),
    (
        "binary_or_command_missing",
        (
            re.compile(r"command not found", re.IGNORECASE),
            re.compile(r"could not launch .*cli process", re.IGNORECASE),
            re.compile(r"failed to launch .*cli", re.IGNORECASE),
            re.compile(r"no such file or directory", re.IGNORECASE),
        ),
    ),
)

_GEMINI_STDERR_STRIP_LINES: frozenset[str] = frozenset({"Loaded cached credentials."})
_CODEX_PERSONALITY_MISSING_MESSAGES_WARNING = (
    "Model personality requested but model_messages is missing"
)
_READ_FILE_NOT_FOUND_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"Error executing tool read_file:\s*File not found(?::\s*|\s+)(?P<path>\S+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"read_file.*file not found(?::\s*|\s+)(?P<path>\S+)",
        re.IGNORECASE,
    ),
)
_WINDOWS_POSIX_DRIVE_PATH_RE = re.compile(r"^/([a-zA-Z])/(.*)$")


def _sanitize_agent_stderr_text(*, agent: str, text: str) -> str:
    if not text:
        return text

    if agent == "gemini":
        lines = [
            line for line in text.splitlines() if line.strip() not in _GEMINI_STDERR_STRIP_LINES
        ]
        return "\n".join(lines)

    if agent == "codex":
        # Codex can emit this warning on every turn; keep one copy for debugging, drop duplicates.
        saw_personality_warning = False
        lines: list[str] = []
        for line in text.splitlines():
            if _CODEX_PERSONALITY_MISSING_MESSAGES_WARNING in line:
                if saw_personality_warning:
                    continue
                saw_personality_warning = True
            lines.append(line)
        return "\n".join(lines)

    return text


def _sanitize_agent_stderr_file(*, agent: str, path: Path) -> None:
    if agent not in {"gemini", "codex"} or not path.exists():
        return
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    sanitized = _sanitize_agent_stderr_text(agent=agent, text=raw)
    if sanitized == raw:
        return
    try:
        path.write_text(sanitized, encoding="utf-8")
    except OSError:
        return


def _override_key_matches_suffix(*, key: str, suffix: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return normalized == suffix or normalized.endswith("." + suffix)


def _strip_codex_personality_overrides(overrides: list[str]) -> list[str]:
    """
    Remove personality/model_personality overrides.

    This is used to turn the "personality requested but model_messages missing" situation into a
    warning-only path (by preventing Codex from receiving an invalid personality config override).
    """

    kept: list[str] = []
    for raw in overrides:
        key_raw, sep, _value_raw = raw.partition("=")
        key = key_raw.strip()
        if not sep or not key:
            kept.append(raw)
            continue
        if _override_key_matches_suffix(
            key=key, suffix="personality"
        ) or _override_key_matches_suffix(key=key, suffix="model_personality"):
            continue
        kept.append(raw)
    return kept


def _codex_personality_warning_lines(*, source: str, warning_line: str | None = None) -> list[str]:
    lines = [
        (
            "Codex warned that personality was requested but model_messages is missing "
            "(Codex fell back to base instructions)."
        ),
        f"source={source}",
        "code=codex_model_messages_missing",
    ]
    if isinstance(warning_line, str) and warning_line.strip():
        lines.append(f"warning={warning_line.strip()}")
    lines.append(
        "hint=If you intended to use a personality, provide model_messages alongside "
        "personality/model_personality (configs/agents.yaml or --agent-config). Otherwise, "
        "ignore this warning."
    )
    return lines


def _looks_like_windows_drive_path(path_str: str) -> bool:
    return bool(re.match(r"^[a-zA-Z]:[\\/]", path_str))


def _normalize_windowsish_path_token(raw_path: str) -> str:
    token = raw_path.strip().strip("'\"`")
    if not token:
        return token
    posixish = token.replace("\\", "/")
    match = _WINDOWS_POSIX_DRIVE_PATH_RE.match(posixish)
    if match is None:
        return token
    drive = match.group(1).upper()
    remainder = match.group(2)
    return f"{drive}:/{remainder}"


def _augment_tool_file_not_found_diagnostics(
    *,
    stderr_text: str,
    workspace_root: Path | None,
) -> str:
    if not stderr_text.strip():
        return stderr_text

    raw_paths: list[str] = []
    for line in stderr_text.splitlines():
        for pattern in _READ_FILE_NOT_FOUND_PATTERNS:
            match = pattern.search(line)
            if match is None:
                continue
            raw = str(match.group("path")).strip().rstrip(".,;")
            if raw:
                raw_paths.append(raw)
            break

    if not raw_paths:
        return stderr_text

    unique_raw_paths = list(dict.fromkeys(raw_paths))
    diagnostics: list[str] = []
    workspace_text = str(workspace_root.resolve()) if workspace_root is not None else "<unknown>"
    for raw in unique_raw_paths:
        normalized = _normalize_windowsish_path_token(raw)
        candidate = Path(normalized)
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        elif _looks_like_windows_drive_path(normalized):
            resolved = Path(normalized)
        elif workspace_root is not None:
            resolved = (workspace_root / candidate).resolve(strict=False)
        else:
            resolved = candidate.resolve(strict=False)
        diagnostics.extend(
            [
                "[path_diagnostic]",
                f"raw_path={raw}",
                f"resolved_path={resolved}",
                f"workspace_root={workspace_text}",
                (
                    "hint=On Windows, both /c/... and C:\\... are accepted, but files must exist "
                    "in the active workspace/backend path."
                ),
            ]
        )

    if diagnostics:
        return stderr_text.rstrip() + "\n" + "\n".join(diagnostics)
    return stderr_text


def _build_preflight_command_list(request: RunRequest) -> list[str]:
    """
    Build the ordered list of command names to probe during preflight.

    Preflight is intended to be generic: the baseline list contains common developer tooling and
    installer entry points, while repo-specific dependencies can be supplied per run via
    `RunRequest.preflight_commands` (CLI: `--preflight-command`) and required checks can be
    supplied via `RunRequest.preflight_required_commands` (CLI: `--require-preflight-command`).
    """

    merged: list[str] = []
    seen: set[str] = set()

    candidates: list[str] = [
        *_BASE_PREFLIGHT_COMMANDS,
        *request.preflight_commands,
        *request.preflight_required_commands,
    ]
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        cmd = raw.strip()
        if not cmd or cmd in seen:
            continue
        merged.append(cmd)
        seen.add(cmd)

    return merged


def _classify_failure_subtype(text: str) -> str | None:
    if not text.strip():
        return None
    for subtype, patterns in _FAILURE_SUBTYPE_RULES:
        if any(pattern.search(text) for pattern in patterns):
            return subtype
    return None


def _agent_binary_for_preflight_probe(*, agent: str, agent_cfg: dict[str, Any]) -> str | None:
    default_binary = {
        "codex": "codex",
        "claude": "claude",
        "gemini": "gemini",
    }.get(agent, "")
    raw_binary = agent_cfg.get("binary", default_binary)
    if not isinstance(raw_binary, str) or not raw_binary.strip():
        return None

    binary = raw_binary.strip()
    if Path(binary).is_absolute():
        return None
    if any(sep in binary for sep in ("/", "\\")):
        return None
    if os.name == "nt" and ":" in binary:
        return None

    return binary


def _probe_commands_local(commands: list[str]) -> tuple[dict[str, bool], dict[str, Any]]:
    out: dict[str, bool] = {}
    probe_details: dict[str, dict[str, Any]] = {}
    python_commands = [cmd for cmd in commands if cmd in {"python", "python3", "py"}]
    python_probe = (
        probe_python_interpreters(candidate_commands=python_commands, timeout_seconds=5.0)
        if python_commands
        else None
    )
    python_by_command = python_probe.by_command() if python_probe is not None else {}
    for cmd in commands:
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        if cmd in python_by_command:
            candidate = python_by_command[cmd]
            out[cmd] = bool(candidate.usable)
            probe_details[cmd] = candidate.to_dict()
            continue

        resolved = shutil.which(cmd)
        present = resolved is not None
        out[cmd] = present
        probe_details[cmd] = {
            "command": cmd,
            "resolved_path": resolved,
            "present": present,
            "usable": present,
            "reason_code": (None if present else "not_found"),
            "reason": (None if present else f"`{cmd}` was not found on PATH."),
        }

    meta: dict[str, Any] = {"command_probe_details": probe_details}
    if python_probe is not None:
        meta["python_interpreter"] = python_probe.to_dict()
    return out, meta


def _snapshot_workspace_root(workspace_dir: Path, *, max_entries: int = 200) -> dict[str, Any]:
    if max_entries <= 0:
        return {"entries": [], "total_entries": 0, "truncated": False, "error": None}
    try:
        items = sorted(workspace_dir.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        return {"entries": [], "total_entries": 0, "truncated": False, "error": str(exc)}

    total_entries = len(items)
    truncated = total_entries > max_entries
    entries: list[dict[str, Any]] = []
    for p in items[:max_entries]:
        kind = "other"
        try:
            if p.is_dir():
                kind = "dir"
            elif p.is_file():
                kind = "file"
        except OSError:
            kind = "other"
        entries.append({"name": p.name, "kind": kind})

    return {
        "entries": entries,
        "total_entries": total_entries,
        "truncated": truncated,
        "error": None,
    }


def _runner_host_os() -> str:
    """
    Return a stable host OS label without relying on Windows WMI calls.

    Notes
    -----
    Python's `platform.system()` can hang on some Windows hosts due to WMI queries. The runner
    uses this value only for lightweight environment metadata; avoid the risk by treating the
    Windows case as a constant label.
    """

    if os.name == "nt":
        return "Windows"
    return platform.system()


def _effective_gemini_cli_sandbox(*, policy_value: Any, has_outer_sandbox: bool) -> bool:
    enabled = bool(policy_value) if isinstance(policy_value, bool) else True
    if not enabled:
        return False
    if has_outer_sandbox:
        # Gemini CLI's `--sandbox` uses docker/podman; when the runner itself is already
        # executing inside a Docker sandbox, rely on the outer sandbox and disable Gemini's
        # nested sandbox.
        return False
    if os.name == "nt":
        # Gemini CLI's `--sandbox` relies on docker/podman and can hang on Windows hosts in
        # headless/non-interactive runs. For runner use-cases, prefer the runner's own Docker
        # sandbox backend instead.
        return False
    return True


def _infer_shell_policy_status(
    *,
    agent: str,
    claude_policy: dict[str, Any],
    gemini_policy: dict[str, Any],
    has_outer_sandbox: bool,
) -> tuple[str, str, list[str] | None]:
    """
    Infer whether shell commands should be treated as allowed/blocked for the selected agent.

    Returns `(status, reason, allowed_tools)` where status is one of: allowed, blocked, unknown.
    """

    if agent == "claude":
        raw_allowed = claude_policy.get("allowed_tools")
        allowed_tools = (
            [x for x in raw_allowed if isinstance(x, str) and x.strip()]
            if isinstance(raw_allowed, list)
            else []
        )
        shell_enabled = "Bash" in allowed_tools
        return (
            ("allowed" if shell_enabled else "blocked"),
            ("claude.allowed_tools includes Bash" if shell_enabled else "Bash not enabled"),
            allowed_tools,
        )

    if agent == "gemini":
        raw_allowed = gemini_policy.get("allowed_tools")
        allowed_tools = (
            [x for x in raw_allowed if isinstance(x, str) and x.strip()]
            if isinstance(raw_allowed, list)
            else []
        )
        shell_enabled = "run_shell_command" in allowed_tools
        effective_gemini_sandbox = _effective_gemini_cli_sandbox(
            policy_value=gemini_policy.get("sandbox", True),
            has_outer_sandbox=has_outer_sandbox,
        )
        shell_available = has_outer_sandbox or effective_gemini_sandbox
        if shell_enabled and not shell_available:
            return (
                "blocked",
                (
                    "run_shell_command requested, but Gemini sandbox is disabled/unavailable. "
                    "Use --exec-backend docker (recommended) or enable gemini.sandbox."
                ),
                allowed_tools,
            )
        return (
            ("allowed" if shell_enabled else "blocked"),
            (
                "gemini.allowed_tools includes run_shell_command"
                if shell_enabled
                else "run_shell_command not enabled"
            ),
            allowed_tools,
        )

    if agent == "codex":
        return (
            "unknown",
            (
                "Codex CLI command execution depends on Codex sandbox policy/approvals. "
                "This runner can't reliably precompute allowlist outcome."
            ),
            None,
        )

    return (
        "unknown",
        f"Unknown agent={agent!r}; cannot infer shell allowlist status.",
        None,
    )


def _resolve_agent_prompt_input_path(*, raw: Path, repo_root: Path, workspace_dir: Path) -> Path:
    if raw.is_absolute():
        candidate = raw
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
        raise FileNotFoundError(f"Agent prompt file not found: {raw}")

    candidates = [
        workspace_dir / raw,
        repo_root / raw,
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()

    raise FileNotFoundError(
        f"Agent prompt file not found.\ninput={raw}\ntried={', '.join(str(p) for p in candidates)}"
    )


def _stage_agent_prompt_text(*, run_dir: Path, name: str, text: str) -> Path:
    dest_dir = run_dir / "agent_prompts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / name
    dest_path.write_text(text, encoding="utf-8")
    return dest_path


def _stage_agent_prompt_file(*, run_dir: Path, name: str, src_path: Path) -> Path:
    dest_dir = run_dir / "agent_prompts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / name
    shutil.copyfile(src_path, dest_path)
    return dest_path


def _agent_path_for_staged_file(
    staged_path: Path, *, run_dir: Path, run_dir_mount: str | None
) -> str:
    if run_dir_mount is None:
        return str(staged_path.resolve())

    mount = run_dir_mount.strip().replace("\\", "/").rstrip("/")
    if not mount:
        mount = "/run_dir"
    if not mount.startswith("/"):
        mount = f"/{mount}"

    rel = staged_path.resolve().relative_to(run_dir.resolve()).as_posix()
    if not rel:
        return mount
    return f"{mount}/{rel}"


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("Agent output was empty; expected a JSON object.")

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        parsed = json.loads(cleaned)
    except Exception:  # noqa: BLE001
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    decoder = JSONDecoder()
    for idx, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            parsed_obj, _ = decoder.raw_decode(cleaned[idx:])
        except Exception:  # noqa: BLE001
            continue
        if isinstance(parsed_obj, dict):
            return parsed_obj

    raise ValueError("Could not find a JSON object in agent output.")


def _build_followup_prompt(
    *,
    base_prompt: str,
    report_validation_errors: list[str],
    schema_dict: dict[str, Any],
    prior_last_message_text: str,
    attempt_number: int,
) -> str:
    errors = [str(e).strip() for e in report_validation_errors if str(e).strip()]
    error_block = "\n".join(f"- {line}" for line in errors[:20]) or "- (no error details)"

    prior_message = prior_last_message_text.strip()
    if len(prior_message) > 4000:
        prior_message = prior_message[:4000] + "\n...[truncated]"
    if not prior_message:
        prior_message = "(no prior message captured)"

    schema_json = json.dumps(schema_dict, indent=2, ensure_ascii=False)

    return (
        f"{base_prompt}\n\n"
        "Follow-up required.\n"
        f"This is follow-up attempt #{attempt_number} because your previous response did not "
        "validate against the report schema.\n\n"
        "Validation errors:\n"
        f"{error_block}\n\n"
        "Previous assistant output:\n"
        "```\n"
        f"{prior_message}\n"
        "```\n\n"
        "Return ONLY one JSON object that validates against this schema.\n"
        "Do not include markdown fences, prose, or extra keys.\n\n"
        "Schema:\n"
        f"{schema_json}\n"
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _git_diff(path: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), "diff"], capture_output=True, text=True, check=False
    )
    return proc.stdout


def _git_numstat(path: Path) -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["git", "-C", str(path), "diff", "--numstat"], capture_output=True, text=True, check=False
    )
    out: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_s, removed_s, file_path = parts
        try:
            added = int(added_s) if added_s != "-" else 0
            removed = int(removed_s) if removed_s != "-" else 0
        except ValueError:
            continue
        out.append({"path": file_path, "lines_added": added, "lines_removed": removed})
    return out


def _git_status_porcelain(path: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout


def _ensure_git_user_config(path: Path) -> None:
    email = subprocess.run(
        ["git", "-C", str(path), "config", "user.email"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    name = subprocess.run(
        ["git", "-C", str(path), "config", "user.name"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()

    if not email:
        subprocess.run(
            ["git", "-C", str(path), "config", "user.email", "usertest@local"],
            capture_output=True,
            text=True,
            check=True,
        )
    if not name:
        subprocess.run(
            ["git", "-C", str(path), "config", "user.name", "usertest"],
            capture_output=True,
            text=True,
            check=True,
        )


def _maybe_commit_preprocess_workspace(path: Path, *, message: str) -> str | None:
    status = _git_status_porcelain(path)
    if not status.strip():
        return None

    _ensure_git_user_config(path)
    subprocess.run(
        ["git", "-C", str(path), "add", "-A"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "commit",
            "--no-gpg-sign",
            "-m",
            message,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _maybe_codex_login_in_sandbox(
    *,
    command_prefix: list[str],
    run_dir: Path,
) -> None:
    setup_log = run_dir / "sandbox_setup.txt"

    # Avoid `printenv ... | codex login ...`:
    # - It appends a newline to the key.
    # - In POSIX `sh`, pipeline exit codes do not reflect earlier failures (no pipefail),
    #   so a missing OPENAI_API_KEY can be silently ignored.
    login_cmd = (
        'if [ -z "${OPENAI_API_KEY:-}" ]; then '
        'echo "OPENAI_API_KEY is not set in the sandbox environment" >&2; exit 1; '
        "fi; "
        'echo "OPENAI_API_KEY length=${#OPENAI_API_KEY}" >&2; '
        'printf "%s" "$OPENAI_API_KEY" | codex login --with-api-key'
    )
    proc = subprocess.run(
        [*command_prefix, "sh", "-lc", login_cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    setup_log.parent.mkdir(parents=True, exist_ok=True)
    setup_log.write_text(
        "\n".join(
            [
                f"$ docker exec ... sh -lc {login_cmd!r}",
                f"exit_code={proc.returncode}",
                "",
                "stdout:",
                proc.stdout.strip(),
                "",
                "stderr:",
                proc.stderr.strip(),
                "",
            ]
        ),
        encoding="utf-8",
    )

    if proc.returncode != 0:
        raise RuntimeError(
            "Failed to log into Codex inside the Docker sandbox. "
            "Prefer --exec-use-host-agent-login to reuse your local Codex subscription login "
            "state (~/.codex) inside Docker without API keys. "
            "If you must use an API key, opt into API-key mode with --exec-use-api-key-auth, "
            "ensure OPENAI_API_KEY is set on the host, and allowlist it via "
            "--exec-env OPENAI_API_KEY. "
            f"See {setup_log}."
        )


def run_once(config: RunnerConfig, request: RunRequest) -> RunResult:
    policy_cfg = config.policies.get(request.policy, {})
    if not isinstance(policy_cfg, dict):
        policy_cfg = {}

    codex_policy = policy_cfg.get("codex", {})
    codex_policy = codex_policy if isinstance(codex_policy, dict) else {}

    claude_policy = policy_cfg.get("claude", {})
    claude_policy = claude_policy if isinstance(claude_policy, dict) else {}

    gemini_policy = policy_cfg.get("gemini", {})
    gemini_policy = gemini_policy if isinstance(gemini_policy, dict) else {}

    if request.agent == "codex":
        allow_edits = bool(codex_policy.get("allow_edits", False))
    elif request.agent == "claude":
        allow_edits = bool(claude_policy.get("allow_edits", False))
    elif request.agent == "gemini":
        allow_edits = bool(gemini_policy.get("allow_edits", False))
    else:
        raise NotImplementedError(
            f"Unsupported agent={request.agent!r}. "
            "MVP implements `codex`, `claude`, and `gemini`; other agents are placeholders."
        )

    acquired = None

    target_slug = slugify(request.repo)
    timestamp = utc_timestamp_compact()
    run_dir = config.runs_dir / target_slug / timestamp / request.agent / str(request.seed)
    run_dir.mkdir(parents=True, exist_ok=True)

    workspace_id = f"{target_slug}_{timestamp}_{request.agent}_{request.seed}"
    try:
        preferred_workspace_dir = config.runs_dir / "_workspaces" / workspace_id
        acquired = acquire_target(
            repo=request.repo,
            dest_dir=preferred_workspace_dir,
            ref=request.ref,
        )

        target_ref: dict[str, Any] = {
            "repo_input": acquired.repo_input,
            "ref": acquired.ref,
            "commit_sha": acquired.commit_sha,
            "acquire_mode": acquired.mode,
            "agent": request.agent,
            "policy": request.policy,
            "seed": request.seed,
            "obfuscate_agent_docs": bool(request.obfuscate_agent_docs),
        }
        _write_json(run_dir / "target_ref.json", target_ref)

        agent_cfg = config.agents.get(request.agent, {}) if isinstance(config.agents, dict) else {}
        agent_cfg_dict = agent_cfg if isinstance(agent_cfg, dict) else {}
        codex_binary = agent_cfg_dict.get("binary", "codex")
        codex_subcommand = agent_cfg_dict.get("subcommand", "exec")
        default_overrides: list[str] = []
        raw_defaults = agent_cfg_dict.get("config_overrides")
        if isinstance(raw_defaults, list):
            default_overrides = [x for x in raw_defaults if isinstance(x, str)]

        combined_overrides = [*default_overrides, *request.agent_config_overrides]
        preflight_warnings: list[dict[str, Any]] = []
        if request.agent == "codex":
            reasoning_issue = validate_codex_reasoning_effort_config_overrides(combined_overrides)
            if reasoning_issue is not None:
                message = reasoning_issue.message
                hint = reasoning_issue.hint
                _write_json(
                    run_dir / "preflight.json",
                    {
                        "warnings": preflight_warnings,
                        "agent_config_validation": {
                            "ok": False,
                            "issues": [
                                {
                                    "code": "codex_model_reasoning_effort_invalid",
                                    "message": message,
                                    "hint": hint,
                                    "details": reasoning_issue.details,
                                }
                            ],
                        },
                    },
                )
                _write_json(
                    run_dir / "error.json",
                    {
                        "type": "AgentPreflightFailed",
                        "subtype": "invalid_agent_config",
                        "code": "codex_model_reasoning_effort_invalid",
                        "agent": request.agent,
                        "message": message,
                        "hint": hint,
                        "details": reasoning_issue.details,
                    },
                )
                return RunResult(
                    run_dir=run_dir,
                    exit_code=1,
                    report_validation_errors=[
                        message,
                        "code=codex_model_reasoning_effort_invalid",
                        f"hint={hint}",
                    ],
                )

            personality_issue = validate_codex_personality_config_overrides(combined_overrides)
            if personality_issue is not None:
                preflight_warnings.append(
                    {
                        "code": "codex_model_messages_missing",
                        "agent": request.agent,
                        "message": personality_issue.message,
                        "hint": personality_issue.hint,
                        "details": personality_issue.details,
                    },
                )
                combined_overrides = _strip_codex_personality_overrides(list(combined_overrides))

        catalog_config = load_catalog_config(config.repo_root, acquired.workspace_dir)

        resolved_inputs = resolve_effective_run_inputs(
            runner_repo_root=config.repo_root,
            target_repo_root=acquired.workspace_dir,
            catalog_config=catalog_config,
            persona_id=request.persona_id,
            mission_id=request.mission_id,
        )
        effective_spec = resolved_inputs.effective

        # Fail fast: permission requirements are validated before any expensive backend setup.
        shell_status, shell_reason, allowed_tools = _infer_shell_policy_status(
            agent=request.agent,
            claude_policy=claude_policy,
            gemini_policy=gemini_policy,
            has_outer_sandbox=(request.exec_backend == "docker"),
        )
        if bool(resolved_inputs.mission.requires_shell) and shell_status == "blocked":
            message = (
                f"Mission '{effective_spec.mission_id}' requires shell commands, but "
                f"policy '{request.policy}' for agent '{request.agent}' blocks shell commands."
            )
            hint = "Use --policy inspect (read-only + shell) or --policy write."
            _write_json(
                run_dir / "preflight.json",
                {
                    "warnings": preflight_warnings,
                    "capabilities": {
                        "shell_commands": {
                            "status": shell_status,
                            "reason": shell_reason,
                            "allowed_tools": allowed_tools,
                        },
                        "edits": {"allowed": bool(allow_edits)},
                    },
                    "mission_requirements": {
                        "mission_id": effective_spec.mission_id,
                        "requires_shell": bool(resolved_inputs.mission.requires_shell),
                        "requires_edits": bool(resolved_inputs.mission.requires_edits),
                    },
                },
            )
            _write_json(
                run_dir / "error.json",
                {
                    "type": "AgentPreflightFailed",
                    "subtype": "mission_requires_shell",
                    "code": "mission_requires_shell",
                    "agent": request.agent,
                    "policy": request.policy,
                    "mission_id": effective_spec.mission_id,
                    "capability": "shell_commands",
                    "message": message,
                    "hint": hint,
                    "preflight": {
                        "capabilities": {
                            "shell_commands": {
                                "status": shell_status,
                                "reason": shell_reason,
                                "allowed_tools": allowed_tools,
                            }
                        }
                    },
                },
            )
            return RunResult(
                run_dir=run_dir,
                exit_code=1,
                report_validation_errors=[message, "code=mission_requires_shell", f"hint={hint}"],
            )

        if bool(resolved_inputs.mission.requires_edits) and not allow_edits:
            message = (
                f"Mission '{effective_spec.mission_id}' requires edits, but policy "
                f"'{request.policy}' for agent '{request.agent}' has allow_edits=false."
            )
            hint = "Use --policy write (or update configs/policies.yaml to allow edits)."
            _write_json(
                run_dir / "preflight.json",
                {
                    "warnings": preflight_warnings,
                    "capabilities": {
                        "shell_commands": {
                            "status": shell_status,
                            "reason": shell_reason,
                            "allowed_tools": allowed_tools,
                        },
                        "edits": {"allowed": bool(allow_edits)},
                    },
                    "mission_requirements": {
                        "mission_id": effective_spec.mission_id,
                        "requires_shell": bool(resolved_inputs.mission.requires_shell),
                        "requires_edits": bool(resolved_inputs.mission.requires_edits),
                    },
                },
            )
            _write_json(
                run_dir / "error.json",
                {
                    "type": "AgentPreflightFailed",
                    "subtype": "mission_requires_edits",
                    "code": "mission_requires_edits",
                    "agent": request.agent,
                    "policy": request.policy,
                    "mission_id": effective_spec.mission_id,
                    "capability": "edits",
                    "message": message,
                    "hint": hint,
                    "preflight": {"capabilities": {"edits": {"allowed": bool(allow_edits)}}},
                },
            )
            return RunResult(
                run_dir=run_dir,
                exit_code=1,
                report_validation_errors=[message, "code=mission_requires_edits", f"hint={hint}"],
            )

        if request.policy in {"inspect", "write"} and shell_status == "blocked":
            message = (
                f"Policy '{request.policy}' for agent '{request.agent}' blocks shell commands. "
                "Fix configs/policies.yaml or pick a policy that enables shell command execution."
            )
            _write_json(
                run_dir / "preflight.json",
                {
                    "warnings": preflight_warnings,
                    "capabilities": {
                        "shell_commands": {
                            "status": shell_status,
                            "reason": shell_reason,
                            "allowed_tools": allowed_tools,
                        },
                        "edits": {"allowed": bool(allow_edits)},
                    },
                    "mission_requirements": {
                        "mission_id": effective_spec.mission_id,
                        "requires_shell": bool(resolved_inputs.mission.requires_shell),
                        "requires_edits": bool(resolved_inputs.mission.requires_edits),
                    },
                },
            )
            _write_json(
                run_dir / "error.json",
                {
                    "type": "AgentPreflightFailed",
                    "subtype": "policy_block",
                    "agent": request.agent,
                    "capability": "shell_commands",
                    "message": message,
                    "preflight": {
                        "capabilities": {
                            "shell_commands": {
                                "status": shell_status,
                                "reason": shell_reason,
                                "allowed_tools": allowed_tools,
                            }
                        }
                    },
                },
            )
            return RunResult(run_dir=run_dir, exit_code=1, report_validation_errors=[message])

        if request.obfuscate_agent_docs:
            obfuscate_target_agent_docs(workspace_dir=acquired.workspace_dir, run_dir=run_dir)
            if allow_edits:
                preprocess_commit_sha = _maybe_commit_preprocess_workspace(
                    acquired.workspace_dir,
                    message="usertest: preprocess workspace (obfuscate agent docs)",
                )
                if preprocess_commit_sha:
                    (run_dir / "preprocess_commit.txt").write_text(
                        preprocess_commit_sha + "\n", encoding="utf-8"
                    )
                    target_ref["preprocess_commit_sha"] = preprocess_commit_sha
                    _write_json(run_dir / "target_ref.json", target_ref)

        users_md_path = acquired.workspace_dir / "USERS.md"
        users_md_present = users_md_path.exists()
        users_md_text = users_md_path.read_text(encoding="utf-8") if users_md_present else ""
        if users_md_present:
            (run_dir / "users.md").write_text(users_md_text, encoding="utf-8")

        persona_source_text = resolved_inputs.persona.source_path.read_text(encoding="utf-8")
        mission_source_text = resolved_inputs.mission.source_path.read_text(encoding="utf-8")

        (run_dir / "persona.source.md").write_text(persona_source_text, encoding="utf-8")
        (run_dir / "persona.resolved.md").write_text(
            effective_spec.persona_md_resolved.rstrip() + "\n", encoding="utf-8"
        )
        (run_dir / "mission.source.md").write_text(mission_source_text, encoding="utf-8")
        (run_dir / "mission.resolved.md").write_text(
            effective_spec.mission_md_resolved.rstrip() + "\n", encoding="utf-8"
        )
        (run_dir / "prompt.template.md").write_text(
            effective_spec.prompt_template_text, encoding="utf-8"
        )

        _write_json(run_dir / "report.schema.json", effective_spec.report_schema_dict)

        _write_json(
            run_dir / "effective_run_spec.json",
            {
                "persona_id": effective_spec.persona_id,
                "persona_name": effective_spec.persona_name,
                "persona_md_resolved": effective_spec.persona_md_resolved,
                "persona_source_path": str(resolved_inputs.persona.source_path),
                "mission_id": effective_spec.mission_id,
                "mission_name": effective_spec.mission_name,
                "mission_md_resolved": effective_spec.mission_md_resolved,
                "mission_source_path": str(resolved_inputs.mission.source_path),
                "execution_mode": effective_spec.execution_mode,
                "prompt_template_path": str(effective_spec.prompt_template_path),
                "prompt_template_text": effective_spec.prompt_template_text,
                "report_schema_path": str(effective_spec.report_schema_path),
                "report_schema_dict": effective_spec.report_schema_dict,
            },
        )

        target_ref.update(
            {
                "users_md_present": users_md_present,
                "persona_id": effective_spec.persona_id,
                "mission_id": effective_spec.mission_id,
                "prompt_template_path": str(effective_spec.prompt_template_path),
                "report_schema_path": str(effective_spec.report_schema_path),
            }
        )
        _write_json(run_dir / "target_ref.json", target_ref)

        raw_events_path = run_dir / "raw_events.jsonl"
        last_message_path = run_dir / "agent_last_message.txt"
        stderr_path = run_dir / "agent_stderr.txt"

        backend = prepare_execution_backend(
            repo_root=config.repo_root,
            run_dir=run_dir,
            workspace_dir=acquired.workspace_dir,
            request=request,
            workspace_id=workspace_id,
            agent_cfg=agent_cfg_dict,
        )
        sandbox = backend.sandbox_instance
        command_prefix = backend.command_prefix
        workspace_mount = backend.workspace_mount
        # When executing inside a docker sandbox, `workspace_mount` is a POSIX path like
        # `/workspace`. On Windows hosts, `Path("/workspace")` becomes `\\workspace`, which
        # breaks agents that interpret `--cd` literally. Keep it as a string when mounted.
        workspace_dir_for_agent: Path | str = (
            workspace_mount if workspace_mount is not None else acquired.workspace_dir
        )
        staged_system_prompt: Path | None = None
        system_prompt_path_for_agent: str | None = None
        if request.agent_system_prompt_file is not None:
            src_path = _resolve_agent_prompt_input_path(
                raw=request.agent_system_prompt_file,
                repo_root=config.repo_root,
                workspace_dir=acquired.workspace_dir,
            )
            staged_system_prompt = _stage_agent_prompt_file(
                run_dir=run_dir,
                name="system_prompt.md",
                src_path=src_path,
            )
            system_prompt_path_for_agent = _agent_path_for_staged_file(
                staged_system_prompt,
                run_dir=run_dir,
                run_dir_mount=backend.run_dir_mount,
            )

        append_text = request.agent_append_system_prompt
        if isinstance(append_text, str) and not append_text.strip():
            append_text = None

        staged_append_system_prompt: Path | None = None
        append_system_prompt_path_for_agent: str | None = None
        if request.agent_append_system_prompt_file is not None or append_text is not None:
            if request.agent == "gemini":
                raise ValueError(
                    "Gemini system prompt append is not supported. "
                    "Use --agent-system-prompt-file to replace the system prompt instead."
                )

            if request.agent_append_system_prompt_file is not None:
                src_path = _resolve_agent_prompt_input_path(
                    raw=request.agent_append_system_prompt_file,
                    repo_root=config.repo_root,
                    workspace_dir=acquired.workspace_dir,
                )
                staged_append_system_prompt = _stage_agent_prompt_file(
                    run_dir=run_dir,
                    name="append_system_prompt.md",
                    src_path=src_path,
                )
            else:
                assert append_text is not None
                staged_append_system_prompt = _stage_agent_prompt_text(
                    run_dir=run_dir,
                    name="append_system_prompt.md",
                    text=append_text,
                )

            append_system_prompt_path_for_agent = _agent_path_for_staged_file(
                staged_append_system_prompt,
                run_dir=run_dir,
                run_dir_mount=backend.run_dir_mount,
            )

        if staged_system_prompt is not None:
            try:
                target_ref["agent_system_prompt_file"] = (
                    staged_system_prompt.resolve().relative_to(run_dir.resolve()).as_posix()
                )
            except Exception:
                target_ref["agent_system_prompt_file"] = staged_system_prompt.as_posix()
        if staged_append_system_prompt is not None:
            try:
                target_ref["agent_append_system_prompt_file"] = (
                    staged_append_system_prompt.resolve().relative_to(run_dir.resolve()).as_posix()
                )
            except Exception:
                target_ref["agent_append_system_prompt_file"] = (
                    staged_append_system_prompt.as_posix()
                )
        if staged_system_prompt is not None or staged_append_system_prompt is not None:
            _write_json(run_dir / "target_ref.json", target_ref)

        try:
            bootstrap: PipBootstrapResult | None = None
            if is_pip_repo_input(request.repo):
                pip_spec = parse_pip_repo_input(request.repo)
                req_path = pip_requirements_path(acquired.workspace_dir)
                requirements_rel = req_path.relative_to(acquired.workspace_dir).as_posix()
                bootstrap = bootstrap_pip_requirements(
                    workspace_dir=acquired.workspace_dir,
                    requirements_relpath=requirements_rel,
                    run_dir=run_dir,
                    command_prefix=command_prefix,
                    workspace_mount=workspace_mount,
                    installer=pip_spec.installer,
                )

            agent_env_overrides = bootstrap.env_overrides if bootstrap is not None else None

            codex_sandbox_mode: str | None = None
            codex_ask_for_approval: str | None = None
            if request.agent == "codex":
                sandbox_policy_raw = codex_policy.get("sandbox", "read-only")
                sandbox_policy = (
                    str(sandbox_policy_raw)
                    if isinstance(sandbox_policy_raw, str) and sandbox_policy_raw.strip()
                    else "read-only"
                )
                if sandbox is not None and request.policy == "write":
                    sandbox_policy = "danger-full-access"
                codex_sandbox_mode = sandbox_policy

                ask_for_approval_raw = codex_policy.get("ask_for_approval", "never")
                codex_ask_for_approval = (
                    str(ask_for_approval_raw)
                    if isinstance(ask_for_approval_raw, str) and ask_for_approval_raw.strip()
                    else "never"
                )

            required_agent_binary = _agent_binary_for_preflight_probe(
                agent=request.agent,
                agent_cfg=agent_cfg_dict,
            )

            preflight_required_commands = [
                cmd.strip()
                for cmd in request.preflight_required_commands
                if isinstance(cmd, str) and cmd.strip()
            ]

            probe_commands = _build_preflight_command_list(request)
            if required_agent_binary is not None and required_agent_binary not in probe_commands:
                probe_commands.append(required_agent_binary)
            preflight_commands_present: dict[str, bool] = {}
            preflight_meta: dict[str, Any] = {}
            effective_probe_commands = list(probe_commands)
            try:
                if sandbox is not None:
                    effective_probe_commands = [
                        c for c in probe_commands if isinstance(c, str) and c.strip()
                    ]
                    preflight_commands_present, preflight_meta = probe_commands_in_container(
                        command_prefix=command_prefix,
                        commands=effective_probe_commands,
                    )
                else:
                    preflight_commands_present, preflight_meta = _probe_commands_local(
                        probe_commands
                    )
            except Exception as e:  # noqa: BLE001
                preflight_commands_present = {}
                preflight_meta = {"error": str(e)}

            preflight_workspace_snapshot = _snapshot_workspace_root(acquired.workspace_dir)

            shell_status = "unknown"
            shell_reason = ""
            allowed_tools: list[str] | None = None
            if request.agent == "claude":
                raw_allowed = claude_policy.get("allowed_tools")
                if isinstance(raw_allowed, list):
                    allowed_tools = [x for x in raw_allowed if isinstance(x, str) and x.strip()]
                else:
                    allowed_tools = []
                shell_enabled = "Bash" in allowed_tools
                shell_status = "allowed" if shell_enabled else "blocked"
                shell_reason = (
                    "claude.allowed_tools includes Bash" if shell_enabled else "Bash not enabled"
                )
            elif request.agent == "gemini":
                raw_allowed = gemini_policy.get("allowed_tools")
                if isinstance(raw_allowed, list):
                    allowed_tools = [x for x in raw_allowed if isinstance(x, str) and x.strip()]
                else:
                    allowed_tools = []
                shell_enabled = "run_shell_command" in allowed_tools
                effective_gemini_sandbox = _effective_gemini_cli_sandbox(
                    policy_value=gemini_policy.get("sandbox", True),
                    has_outer_sandbox=sandbox is not None,
                )
                shell_available = (sandbox is not None) or effective_gemini_sandbox
                if shell_enabled and not shell_available:
                    shell_status = "blocked"
                    shell_reason = (
                        "run_shell_command requested, but Gemini sandbox is disabled/unavailable. "
                        "Use --exec-backend docker (recommended) or enable gemini.sandbox."
                    )
                else:
                    shell_status = "allowed" if shell_enabled else "blocked"
                    shell_reason = (
                        "gemini.allowed_tools includes run_shell_command"
                        if shell_enabled
                        else "run_shell_command not enabled"
                    )
            else:
                shell_reason = (
                    "Codex CLI command execution depends on Codex sandbox policy/approvals. "
                    "This runner can't reliably precompute allowlist outcome."
                )

            probe_details = preflight_meta.get("command_probe_details")
            probe_details_dict = probe_details if isinstance(probe_details, dict) else {}
            python_interpreter_meta = preflight_meta.get("python_interpreter")
            python_interpreter_summary = (
                python_interpreter_meta if isinstance(python_interpreter_meta, dict) else None
            )

            command_diagnostics: dict[str, Any] = {}
            for cmd in effective_probe_commands:
                present = preflight_commands_present.get(cmd)
                status = "unknown"
                if present is True:
                    status = "present"
                elif present is False:
                    status = "missing"

                detail = probe_details_dict.get(cmd)
                detail_dict = detail if isinstance(detail, dict) else {}
                reason_code = detail_dict.get("reason_code")
                reason_code_s = reason_code if isinstance(reason_code, str) else None
                reason = detail_dict.get("reason")
                reason_s = reason if isinstance(reason, str) else None
                resolved_path = detail_dict.get("resolved_path")
                resolved_path_s = resolved_path if isinstance(resolved_path, str) else None

                if shell_status == "blocked" and status == "present":
                    status = "blocked_by_policy"
                remediation: str | None = None
                if status == "missing":
                    if reason_code_s == "windowsapps_alias":
                        remediation = (
                            "Install and expose a full CPython interpreter (not WindowsApps "
                            "alias), then retry."
                        )
                    elif reason_code_s == "missing_stdlib":
                        remediation = (
                            "Selected Python runtime is incomplete (missing stdlib). "
                            "Install a full interpreter and retry."
                        )
                    else:
                        remediation = (
                            f"Install `{cmd}` in the selected execution backend, "
                            "or switch --exec-backend."
                        )
                elif status == "blocked_by_policy":
                    remediation = (
                        "Enable shell commands in policy (recommended: --policy inspect), "
                        "or switch agent/policy."
                    )
                command_diagnostics[cmd] = {
                    "present": present,
                    "status": status,
                    "resolved_path": resolved_path_s,
                    "reason_code": reason_code_s,
                    "reason": reason_s,
                    "remediation": remediation,
                }

            required_agent_binary_present = (
                preflight_commands_present.get(required_agent_binary)
                if required_agent_binary is not None
                else None
            )

            _write_json(
                run_dir / "preflight.json",
                {
                    "commands": preflight_commands_present,
                    "command_diagnostics": command_diagnostics,
                    "required_commands": preflight_required_commands,
                    "meta": preflight_meta,
                    "warnings": preflight_warnings,
                    "probe_commands": effective_probe_commands,
                    "required_agent_binary": required_agent_binary,
                    "required_agent_binary_present": required_agent_binary_present,
                    "python_interpreter": python_interpreter_summary,
                    "capabilities": {
                        "shell_commands": {
                            "status": shell_status,
                            "reason": shell_reason,
                            "allowed_tools": allowed_tools,
                        },
                        "edits": {"allowed": bool(allow_edits)},
                    },
                    "mission_requirements": {
                        "mission_id": effective_spec.mission_id,
                        "requires_shell": bool(resolved_inputs.mission.requires_shell),
                        "requires_edits": bool(resolved_inputs.mission.requires_edits),
                    },
                    "workspace_root_snapshot": preflight_workspace_snapshot,
                },
            )

            if bool(resolved_inputs.mission.requires_shell) and shell_status == "blocked":
                message = (
                    f"Mission '{effective_spec.mission_id}' requires shell commands, but "
                    f"policy '{request.policy}' for agent '{request.agent}' blocks shell commands."
                )
                hint = "Use --policy inspect (read-only + shell) or --policy write."
                _write_json(
                    run_dir / "error.json",
                    {
                        "type": "AgentPreflightFailed",
                        "subtype": "mission_requires_shell",
                        "code": "mission_requires_shell",
                        "agent": request.agent,
                        "policy": request.policy,
                        "mission_id": effective_spec.mission_id,
                        "capability": "shell_commands",
                        "message": message,
                        "hint": hint,
                        "preflight": {
                            "capabilities": {
                                "shell_commands": {
                                    "status": shell_status,
                                    "reason": shell_reason,
                                    "allowed_tools": allowed_tools,
                                }
                            }
                        },
                    },
                )
                return RunResult(
                    run_dir=run_dir,
                    exit_code=1,
                    report_validation_errors=[
                        message,
                        "code=mission_requires_shell",
                        f"hint={hint}",
                    ],
                )

            if bool(resolved_inputs.mission.requires_edits) and not allow_edits:
                message = (
                    f"Mission '{effective_spec.mission_id}' requires edits, but policy "
                    f"'{request.policy}' for agent '{request.agent}' has allow_edits=false."
                )
                hint = "Use --policy write (or update configs/policies.yaml to allow edits)."
                _write_json(
                    run_dir / "error.json",
                    {
                        "type": "AgentPreflightFailed",
                        "subtype": "mission_requires_edits",
                        "code": "mission_requires_edits",
                        "agent": request.agent,
                        "policy": request.policy,
                        "mission_id": effective_spec.mission_id,
                        "capability": "edits",
                        "message": message,
                        "hint": hint,
                        "preflight": {"capabilities": {"edits": {"allowed": bool(allow_edits)}}},
                    },
                )
                return RunResult(
                    run_dir=run_dir,
                    exit_code=1,
                    report_validation_errors=[
                        message,
                        "code=mission_requires_edits",
                        f"hint={hint}",
                    ],
                )

            if request.policy in {"inspect", "write"} and shell_status == "blocked":
                message = (
                    f"Policy '{request.policy}' for agent '{request.agent}' blocks shell commands. "
                    "Fix configs/policies.yaml or pick a policy that enables "
                    "shell command execution."
                )
                _write_json(
                    run_dir / "error.json",
                    {
                        "type": "AgentPreflightFailed",
                        "subtype": "policy_block",
                        "agent": request.agent,
                        "capability": "shell_commands",
                        "message": message,
                        "preflight": {
                            "capabilities": {
                                "shell_commands": {
                                    "status": shell_status,
                                    "reason": shell_reason,
                                    "allowed_tools": allowed_tools,
                                }
                            }
                        },
                    },
                )
                return RunResult(
                    run_dir=run_dir,
                    exit_code=1,
                    report_validation_errors=[message],
                )

            if (
                required_agent_binary is not None
                and preflight_commands_present
                and preflight_commands_present.get(required_agent_binary) is False
            ):
                message = (
                    f"Required agent binary '{required_agent_binary}' is not available in the "
                    "execution environment. "
                    "Install the CLI in the selected backend, or update configs/agents.yaml "
                    f"for agent '{request.agent}' to a valid binary path."
                )
                _write_json(
                    run_dir / "error.json",
                    {
                        "type": "AgentPreflightFailed",
                        "subtype": "binary_missing",
                        "agent": request.agent,
                        "required_binary": required_agent_binary,
                        "message": message,
                        "preflight": {
                            "commands": preflight_commands_present,
                            "meta": preflight_meta,
                            "probe_commands": effective_probe_commands,
                        },
                    },
                )
                return RunResult(
                    run_dir=run_dir,
                    exit_code=1,
                    report_validation_errors=[message],
                )

            if preflight_required_commands:
                failures: dict[str, Any] = {}
                for cmd in preflight_required_commands:
                    diag = command_diagnostics.get(cmd)
                    status = diag.get("status") if isinstance(diag, dict) else None
                    if status != "present":
                        failures[cmd] = diag
                if failures:
                    failing_list = ", ".join(sorted(failures))
                    message = (
                        "Preflight failed: required command(s) unavailable: "
                        f"{failing_list}. See preflight.json for details."
                    )
                    _write_json(
                        run_dir / "error.json",
                        {
                            "type": "AgentPreflightFailed",
                            "subtype": "required_command_unavailable",
                            "agent": request.agent,
                            "message": message,
                            "required_commands": preflight_required_commands,
                            "failures": failures,
                        },
                    )
                    return RunResult(
                        run_dir=run_dir,
                        exit_code=1,
                        report_validation_errors=[message],
                    )

            if sandbox is not None:
                capture_dns_snapshot(
                    command_prefix=command_prefix,
                    artifacts_dir=run_dir / "sandbox",
                )

            policy_json = json.dumps(
                {
                    "agent": request.agent,
                    "policy": request.policy,
                    "allow_edits": allow_edits,
                    "exec_backend": request.exec_backend,
                    "exec_network": request.exec_network,
                    "exec_cache": request.exec_cache,
                    "codex": {
                        "sandbox": codex_sandbox_mode,
                        "ask_for_approval": codex_ask_for_approval,
                    }
                    if request.agent == "codex"
                    else {},
                },
                indent=2,
                ensure_ascii=False,
            )

            environment_json = json.dumps(
                {
                    "runner_host_os": _runner_host_os(),
                    "runner_host_python": platform.python_version(),
                    "workspace": {
                        "path": str(workspace_dir_for_agent),
                        "mount": workspace_mount,
                        "provenance": acquired.mode,
                    },
                    "execution_backend": {
                        "backend": request.exec_backend,
                        "network": request.exec_network,
                        "cache": request.exec_cache,
                        "container_image": getattr(sandbox, "image_tag", None)
                        if sandbox is not None
                        else None,
                    },
                    "preflight": {
                        "commands": preflight_commands_present,
                        "command_diagnostics": command_diagnostics,
                        "python_interpreter": python_interpreter_summary,
                        "meta": preflight_meta,
                        "probe_commands": effective_probe_commands,
                        "capabilities": {
                            "shell_commands": {
                                "status": shell_status,
                                "reason": shell_reason,
                                "allowed_tools": allowed_tools,
                            },
                            "edits": {"allowed": bool(allow_edits)},
                        },
                        "workspace_root_snapshot": preflight_workspace_snapshot,
                    },
                    "bootstrap": bootstrap.meta if bootstrap is not None else None,
                },
                indent=2,
                ensure_ascii=False,
            )

            report_schema_json = json.dumps(
                effective_spec.report_schema_dict, indent=2, ensure_ascii=False
            )

            try:
                prompt = build_prompt_from_template(
                    template_text=effective_spec.prompt_template_text,
                    variables={
                        "persona_name": effective_spec.persona_name,
                        "persona_md": effective_spec.persona_md_resolved,
                        "mission_name": effective_spec.mission_name,
                        "mission_md": effective_spec.mission_md_resolved,
                        "users_md": users_md_text,
                        "policy_json": policy_json,
                        "environment_json": environment_json,
                        "report_schema_json": report_schema_json,
                    },
                )
            except TemplateSubstitutionError as e:
                template_path = effective_spec.prompt_template_path
                raise TemplateSubstitutionError(
                    f"Prompt template substitution failed for {template_path}:\n{e}"
                ) from e
            (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

            codex_overrides = list(combined_overrides)
            if system_prompt_path_for_agent is not None:
                codex_overrides.append(
                    "model_instructions_file=" + toml_basic_string(system_prompt_path_for_agent)
                )
            if staged_append_system_prompt is not None:
                try:
                    dev_text = staged_append_system_prompt.read_text(encoding="utf-8")
                except OSError:
                    dev_text = ""
                if dev_text.strip():
                    codex_overrides.append("developer_instructions=" + toml_basic_string(dev_text))

            claude_cfg = config.agents.get("claude", {}) if isinstance(config.agents, dict) else {}
            claude_binary = (
                claude_cfg.get("binary", "claude") if isinstance(claude_cfg, dict) else "claude"
            )
            claude_output_format = (
                claude_cfg.get("output_format", "stream-json")
                if isinstance(claude_cfg, dict)
                else "stream-json"
            )
            claude_allowed_tools: list[str] = []
            raw_claude_allowed = claude_policy.get("allowed_tools")
            if isinstance(raw_claude_allowed, list):
                claude_allowed_tools = [x for x in raw_claude_allowed if isinstance(x, str)]
            claude_permission_mode = claude_policy.get("permission_mode")
            claude_permission_mode = (
                claude_permission_mode if isinstance(claude_permission_mode, str) else None
            )

            gemini_cfg = config.agents.get("gemini", {}) if isinstance(config.agents, dict) else {}
            gemini_binary = (
                gemini_cfg.get("binary", "gemini") if isinstance(gemini_cfg, dict) else "gemini"
            )
            gemini_output_format = (
                gemini_cfg.get("output_format", "stream-json")
                if isinstance(gemini_cfg, dict)
                else "stream-json"
            )
            gemini_sandbox_enabled = _effective_gemini_cli_sandbox(
                policy_value=gemini_policy.get("sandbox", True),
                has_outer_sandbox=sandbox is not None,
            )
            gemini_approval_mode = gemini_policy.get("approval_mode", "default")
            gemini_approval_mode = (
                gemini_approval_mode if isinstance(gemini_approval_mode, str) else "default"
            )
            gemini_allowed_tools: list[str] = []
            raw_gemini_allowed = gemini_policy.get("allowed_tools")
            if isinstance(raw_gemini_allowed, list):
                gemini_allowed_tools = [x for x in raw_gemini_allowed if isinstance(x, str)]
            gemini_env_overrides: dict[str, str] | None = None
            if system_prompt_path_for_agent is not None:
                gemini_env_overrides = {"GEMINI_SYSTEM_MD": system_prompt_path_for_agent}
            if agent_env_overrides is not None:
                gemini_env_overrides = {**(gemini_env_overrides or {}), **agent_env_overrides}

            if (
                request.agent == "codex"
                and sandbox is not None
                and not bool(getattr(request, "exec_use_host_agent_login", False))
            ):
                if "OPENAI_API_KEY" not in request.exec_env:
                    raise RuntimeError(
                        "Running Codex inside the Docker execution backend requires credentials. "
                        "Prefer --exec-use-host-agent-login to reuse your local Codex subscription "
                        "login state (~/.codex) inside Docker without API keys (default). "
                        "To opt into API-key login mode, pass --exec-use-api-key-auth, "
                        "--exec-env OPENAI_API_KEY, and set OPENAI_API_KEY on the host."
                    )
                if not os.environ.get("OPENAI_API_KEY"):
                    raise RuntimeError(
                        "OPENAI_API_KEY is allowlisted for the Docker sandbox but is not set on "
                        "the host. Set OPENAI_API_KEY on the host, or remove "
                        "--exec-use-api-key-auth to use host-agent-login mode."
                    )

                _maybe_codex_login_in_sandbox(command_prefix=command_prefix, run_dir=run_dir)

            def _attempt_paths(attempt: int) -> tuple[Path, Path, Path]:
                suffix = f"attempt{attempt}"
                return (
                    run_dir / f"raw_events.{suffix}.jsonl",
                    run_dir / f"agent_last_message.{suffix}.txt",
                    run_dir / f"agent_stderr.{suffix}.txt",
                )

            def _run_agent_attempt(
                *,
                prompt_text: str,
                raw_events_attempt_path: Path,
                last_message_attempt_path: Path,
                stderr_attempt_path: Path,
            ) -> tuple[int, list[str]]:
                if request.agent == "codex":
                    codex_last_message_for_attempt = (
                        _agent_path_for_staged_file(
                            last_message_attempt_path,
                            run_dir=run_dir,
                            run_dir_mount=backend.run_dir_mount,
                        )
                        if backend.run_dir_mount
                        else None
                    )
                    codex_result = run_codex_exec(
                        workspace_dir=workspace_dir_for_agent,
                        prompt=prompt_text,
                        raw_events_path=raw_events_attempt_path,
                        last_message_path=last_message_attempt_path,
                        stderr_path=stderr_attempt_path,
                        sandbox=str(codex_sandbox_mode or "read-only"),
                        ask_for_approval=str(codex_ask_for_approval or "never"),
                        binary=str(codex_binary),
                        subcommand=str(codex_subcommand),
                        model=request.model,
                        config_overrides=codex_overrides,
                        command_prefix=command_prefix,
                        env_overrides=agent_env_overrides,
                        agent_last_message_path=codex_last_message_for_attempt,
                    )
                    return codex_result.exit_code, codex_result.argv

                if request.agent == "claude":
                    claude_result = run_claude_print(
                        workspace_dir=workspace_dir_for_agent,
                        prompt=prompt_text,
                        raw_events_path=raw_events_attempt_path,
                        last_message_path=last_message_attempt_path,
                        stderr_path=stderr_attempt_path,
                        binary=str(claude_binary),
                        output_format=str(claude_output_format),
                        model=request.model,
                        allowed_tools=claude_allowed_tools,
                        permission_mode=claude_permission_mode,
                        system_prompt_file=system_prompt_path_for_agent,
                        append_system_prompt_file=append_system_prompt_path_for_agent,
                        command_prefix=command_prefix,
                        env_overrides=agent_env_overrides,
                    )
                    return claude_result.exit_code, claude_result.argv

                gemini_result = run_gemini(
                    workspace_dir=workspace_dir_for_agent,
                    prompt=prompt_text,
                    raw_events_path=raw_events_attempt_path,
                    last_message_path=last_message_attempt_path,
                    stderr_path=stderr_attempt_path,
                    binary=str(gemini_binary),
                    output_format=str(gemini_output_format),
                    sandbox=gemini_sandbox_enabled,
                    model=request.model,
                    approval_mode=gemini_approval_mode,
                    allowed_tools=gemini_allowed_tools,
                    command_prefix=command_prefix,
                    env_overrides=gemini_env_overrides,
                )
                return gemini_result.exit_code, gemini_result.argv

            rate_limit_retries = max(0, int(request.agent_rate_limit_retries))
            rate_limit_backoff_seconds = max(0.0, float(request.agent_rate_limit_backoff_seconds))
            rate_limit_backoff_multiplier = max(
                1.0, float(request.agent_rate_limit_backoff_multiplier)
            )
            followup_attempts = max(0, int(request.agent_followup_attempts))

            current_prompt = prompt
            rate_limit_retry_count = 0
            followup_count = 0
            attempts_meta: list[dict[str, Any]] = []
            selected_raw_events_path = raw_events_path
            selected_last_message_path = last_message_path
            selected_stderr_path = stderr_path
            selected_stderr_text = ""
            selected_last_message_text = ""
            report_json = None
            report_validation_errors = []

            while True:
                attempt_number = len(attempts_meta) + 1
                (
                    raw_events_attempt_path,
                    last_message_attempt_path,
                    stderr_attempt_path,
                ) = _attempt_paths(attempt_number)

                agent_exit_code, agent_argv = _run_agent_attempt(
                    prompt_text=current_prompt,
                    raw_events_attempt_path=raw_events_attempt_path,
                    last_message_attempt_path=last_message_attempt_path,
                    stderr_attempt_path=stderr_attempt_path,
                )

                raw_attempt_stderr_text = ""
                if stderr_attempt_path.exists():
                    try:
                        raw_attempt_stderr_text = stderr_attempt_path.read_text(
                            encoding="utf-8", errors="replace"
                        ).strip()
                    except OSError:
                        raw_attempt_stderr_text = ""

                codex_personality_warning_line = ""
                codex_personality_warning_detected = bool(
                    request.agent == "codex"
                    and _CODEX_PERSONALITY_MISSING_MESSAGES_WARNING in raw_attempt_stderr_text
                )
                attempt_warnings: list[str] = []
                if codex_personality_warning_detected:
                    for line in raw_attempt_stderr_text.splitlines():
                        if _CODEX_PERSONALITY_MISSING_MESSAGES_WARNING in line:
                            codex_personality_warning_line = line.strip()
                            break
                    attempt_warnings = _codex_personality_warning_lines(
                        source="agent_stderr",
                        warning_line=codex_personality_warning_line,
                    )

                _sanitize_agent_stderr_file(agent=request.agent, path=stderr_attempt_path)

                attempt_stderr_text = ""
                if stderr_attempt_path.exists():
                    try:
                        attempt_stderr_text = stderr_attempt_path.read_text(
                            encoding="utf-8", errors="replace"
                        ).strip()
                    except OSError:
                        attempt_stderr_text = ""

                attempt_report_validation_errors: list[str] = []
                attempt_report_json: dict[str, Any] | None = None
                attempt_last_text = ""
                if last_message_attempt_path.exists():
                    try:
                        attempt_last_text = last_message_attempt_path.read_text(encoding="utf-8")
                    except OSError:
                        attempt_last_text = ""
                if agent_exit_code == 0:
                    try:
                        attempt_report_json = _extract_json_object(attempt_last_text)
                    except Exception as e:  # noqa: BLE001
                        attempt_report_validation_errors = [
                            f"$: failed to parse JSON from agent output: {e}"
                        ]
                    if attempt_report_json is not None:
                        attempt_report_validation_errors = validate_report(
                            attempt_report_json, effective_spec.report_schema_dict
                        )

                failure_subtype = _classify_failure_subtype(
                    "\n".join(
                        [
                            value
                            for value in (
                                attempt_stderr_text,
                                attempt_last_text.strip() if attempt_last_text else "",
                            )
                            if value
                        ]
                    )
                )
                attempt_meta: dict[str, Any] = {
                    "attempt": attempt_number,
                    "exit_code": agent_exit_code,
                    "argv": agent_argv,
                    "failure_subtype": failure_subtype,
                    "report_validation_errors": attempt_report_validation_errors,
                    "warnings": attempt_warnings,
                    "raw_events_path": raw_events_attempt_path.name,
                    "last_message_path": last_message_attempt_path.name,
                    "stderr_path": stderr_attempt_path.name,
                }
                attempts_meta.append(attempt_meta)

                if (
                    agent_exit_code != 0
                    and failure_subtype == "provider_capacity"
                    and rate_limit_retry_count < rate_limit_retries
                ):
                    delay_seconds = rate_limit_backoff_seconds * (
                        rate_limit_backoff_multiplier**rate_limit_retry_count
                    )
                    attempt_meta["retry_reason"] = "provider_capacity"
                    attempt_meta["retry_delay_seconds"] = delay_seconds
                    rate_limit_retry_count += 1
                    if delay_seconds > 0:
                        time.sleep(delay_seconds)
                    continue

                if (
                    agent_exit_code == 0
                    and attempt_report_validation_errors
                    and followup_count < followup_attempts
                    and failure_subtype is None
                    and attempt_last_text.strip()
                ):
                    followup_count += 1
                    attempt_meta["followup_scheduled"] = True
                    attempt_meta["followup_index"] = followup_count
                    current_prompt = _build_followup_prompt(
                        base_prompt=prompt,
                        report_validation_errors=attempt_report_validation_errors,
                        schema_dict=effective_spec.report_schema_dict,
                        prior_last_message_text=attempt_last_text,
                        attempt_number=followup_count,
                    )
                    continue

                selected_raw_events_path = raw_events_attempt_path
                selected_last_message_path = last_message_attempt_path
                selected_stderr_path = stderr_attempt_path
                selected_stderr_text = attempt_stderr_text
                selected_last_message_text = attempt_last_text
                report_json = attempt_report_json
                report_validation_errors = attempt_report_validation_errors
                break

            _write_json(
                run_dir / "agent_attempts.json",
                {
                    "attempts": attempts_meta,
                    "rate_limit_retries_configured": rate_limit_retries,
                    "rate_limit_retries_used": rate_limit_retry_count,
                    "followup_attempts_configured": followup_attempts,
                    "followup_attempts_used": followup_count,
                },
            )

            def _materialize_attempt_artifact(
                src: Path,
                dst: Path,
                *,
                fallback_text: str | None = None,
            ) -> None:
                if src == dst:
                    return
                if src.exists():
                    shutil.copyfile(src, dst)
                    return
                try:
                    dst.write_text(fallback_text or "", encoding="utf-8")
                except OSError:
                    return

            _materialize_attempt_artifact(selected_raw_events_path, raw_events_path)
            _materialize_attempt_artifact(
                selected_last_message_path,
                last_message_path,
                fallback_text=selected_last_message_text,
            )
            _materialize_attempt_artifact(
                selected_stderr_path,
                stderr_path,
                fallback_text=selected_stderr_text,
            )

            if agent_exit_code != 0 and not report_validation_errors:
                if selected_stderr_text:
                    report_validation_errors = selected_stderr_text.splitlines()[:20]
                elif selected_last_message_text.strip():
                    report_validation_errors = selected_last_message_text.strip().splitlines()[:20]
                else:
                    report_validation_errors = [
                        f"{request.agent} exited with code {agent_exit_code}"
                    ]
        finally:
            if sandbox is not None:
                capture_container_artifacts(
                    container_name=getattr(sandbox, "container_name", ""),
                    artifacts_dir=run_dir / "sandbox",
                )
                sandbox.close()

        run_errors: list[str] = []
        if agent_exit_code != 0:
            _sanitize_agent_stderr_file(agent=request.agent, path=stderr_path)

            stderr_text = ""
            if stderr_path.exists():
                try:
                    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    stderr_text = ""
            stderr_text = _augment_tool_file_not_found_diagnostics(
                stderr_text=stderr_text,
                workspace_root=acquired.workspace_dir if acquired is not None else None,
            )
            if stderr_text and stderr_path.exists():
                try:
                    stderr_path.write_text(stderr_text.rstrip() + "\n", encoding="utf-8")
                except OSError:
                    pass

            last_message_text = ""
            if last_message_path.exists():
                try:
                    last_message_text = last_message_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).strip()
                except OSError:
                    last_message_text = ""

            last_message_excerpt = last_message_text
            last_message_truncated = False
            if len(last_message_excerpt) > 4000:
                last_message_excerpt = last_message_excerpt[:4000] + "\n...[truncated]..."
                last_message_truncated = True

            combined_text = "\n".join([x for x in (stderr_text, last_message_text) if x])
            failure_subtype = _classify_failure_subtype(combined_text)
            stderr_was_empty = not bool(stderr_text)
            raw_events_size_bytes = (
                raw_events_path.stat().st_size if raw_events_path.exists() else 0
            )
            last_message_size_chars = len(last_message_text)

            if not stderr_text:
                synthetic_lines = [
                    "[synthetic_stderr] No stderr captured from agent process.",
                    f"agent={request.agent}",
                    f"exit_code={agent_exit_code}",
                    f"failure_subtype={failure_subtype or 'unknown'}",
                    f"raw_events={raw_events_path.name}",
                    f"last_message={last_message_path.name}",
                    f"raw_events_size_bytes={raw_events_size_bytes}",
                    f"last_message_size_chars={last_message_size_chars}",
                ]
                if request.agent == "claude":
                    synthetic_lines.append(
                        "hint=Claude produced no stderr; inspect raw_events.jsonl and "
                        "agent_attempts.json for additional context."
                    )
                if last_message_excerpt:
                    synthetic_lines.extend(["", "[agent_last_message]", last_message_excerpt])
                stderr_text = "\n".join(synthetic_lines).strip()
                try:
                    stderr_path.write_text(stderr_text + "\n", encoding="utf-8")
                except OSError:
                    pass

            if stderr_text:
                run_errors = stderr_text.splitlines()[:20]
            elif last_message_text:
                run_errors = last_message_text.splitlines()[:20]
            else:
                run_errors = [f"{request.agent} exited with code {agent_exit_code}"]

            _write_json(
                run_dir / "error.json",
                {
                    "type": "AgentExecFailed",
                    "exit_code": agent_exit_code,
                    "stderr": "\n".join(run_errors).strip(),
                    "stderr_synthesized": stderr_was_empty,
                    "artifacts": {
                        "raw_events": raw_events_path.name,
                        "last_message": last_message_path.name,
                        "stderr": stderr_path.name,
                    },
                    **({"subtype": failure_subtype} if failure_subtype is not None else {}),
                    **(
                        {
                            "last_message": last_message_excerpt,
                            "last_message_truncated": last_message_truncated,
                        }
                        if last_message_excerpt
                        else {}
                    ),
                },
            )

        normalized_events_path = run_dir / "normalized_events.jsonl"
        if request.agent == "codex":
            normalize_codex_events(
                raw_events_path=raw_events_path,
                normalized_events_path=normalized_events_path,
                workspace_root=acquired.workspace_dir,
                workspace_mount=workspace_mount,
            )
        elif request.agent == "claude":
            normalize_claude_events(
                raw_events_path=raw_events_path,
                normalized_events_path=normalized_events_path,
                workspace_root=acquired.workspace_dir,
                workspace_mount=workspace_mount,
            )
        else:
            normalize_gemini_events(
                raw_events_path=raw_events_path,
                normalized_events_path=normalized_events_path,
                workspace_root=acquired.workspace_dir,
                workspace_mount=workspace_mount,
            )

        diff_numstat: list[dict[str, Any]] = []
        if allow_edits:
            diff_numstat = _git_numstat(acquired.workspace_dir)
            _write_json(run_dir / "diff_numstat.json", diff_numstat)
            if diff_numstat:
                with normalized_events_path.open("a", encoding="utf-8", newline="\n") as out_f:
                    for item in diff_numstat:
                        path = item.get("path")
                        lines_added = item.get("lines_added")
                        lines_removed = item.get("lines_removed")
                        if not isinstance(path, str):
                            continue
                        if not isinstance(lines_added, int) or not isinstance(lines_removed, int):
                            continue
                        event = make_event(
                            "write_file",
                            {
                                "path": path,
                                "lines_added": lines_added,
                                "lines_removed": lines_removed,
                            },
                        )
                        out_f.write(json.dumps(event, ensure_ascii=False) + "\n")

        metrics = compute_metrics(iter_events_jsonl(normalized_events_path))
        if allow_edits:
            metrics["diff_numstat"] = diff_numstat
        _write_json(run_dir / "metrics.json", metrics)

        if report_json is not None:
            _write_json(run_dir / "report.json", report_json)
        elif agent_exit_code != 0 and not report_validation_errors:
            report_validation_errors = run_errors

        if report_validation_errors:
            _write_json(run_dir / "report_validation_errors.json", report_validation_errors)

        if allow_edits:
            patch = _git_diff(acquired.workspace_dir)
            if patch.strip():
                (run_dir / "patch.diff").write_text(patch, encoding="utf-8")

        md = render_report_markdown(
            report=report_json or {}, metrics=metrics, target_ref=target_ref
        )
        (run_dir / "report.md").write_text(md, encoding="utf-8")

        return RunResult(
            run_dir=run_dir,
            exit_code=agent_exit_code,
            report_validation_errors=report_validation_errors,
        )
    except Exception as e:  # noqa: BLE001
        message = str(e)
        subtype = _classify_failure_subtype(message)
        extra: dict[str, Any] = {}
        user_errors: list[str] = [message]
        code = getattr(e, "code", None)
        details = getattr(e, "details", None)
        hint = getattr(e, "hint", None)
        if isinstance(code, str) and code.strip():
            code_s = code.strip()
            extra["code"] = code_s
            user_errors.append(f"code={code_s}")
        if isinstance(details, dict) and details:
            extra["details"] = details
            user_errors.append(f"details={json.dumps(details, ensure_ascii=False)}")
        if isinstance(hint, str) and hint.strip():
            hint_s = hint.strip()
            extra["hint"] = hint_s
            user_errors.append(f"hint={hint_s}")
        _write_json(
            run_dir / "error.json",
            {
                "type": type(e).__name__,
                "message": message,
                **({"subtype": subtype} if subtype is not None else {}),
                **extra,
            },
        )
        return RunResult(run_dir=run_dir, exit_code=1, report_validation_errors=user_errors)
    finally:
        if (
            acquired is not None
            and not (request.keep_workspace or request.exec_keep_container)
            and acquired.workspace_dir.exists()
        ):
            shutil.rmtree(acquired.workspace_dir, ignore_errors=True)
