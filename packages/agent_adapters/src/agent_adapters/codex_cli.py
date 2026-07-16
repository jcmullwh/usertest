from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from agent_adapters.docker_exec_env import inject_docker_exec_env, looks_like_docker_exec_prefix
from agent_adapters.events import utc_now_iso

_CODEX_REFRESH_TOKEN_REUSED_MARKER = "[usertest] detected codex auth error: refresh_token_reused"
_CODEX_REFRESH_TOKEN_REUSED_SUBSTRING = "refresh_token_reused"
_CHATGPT_LOGIN_STATUS = "Logged in using ChatGPT"
_PLAIN_CODEX_BINARY_NAMES: frozenset[str] = frozenset({"codex", "codex.exe"})
_WINDOWS_DESKTOP_CODEX_RELATIVE_BIN = Path("OpenAI") / "Codex" / "bin"
_WINDOWS_DESKTOP_CODEX_PROBE_TIMEOUT_SECONDS = 10.0
CODEX_CHATGPT_SUBSCRIPTION_BASE_URL = "https://chatgpt.com/backend-api/"
CODEX_OPENAI_SUBSCRIPTION_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_SUBSCRIPTION_AUTH_ENV_VARS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
)

# Keep subscription-backed Codex invocations independent from ambient provider
# routing as well as alternate credentials.  These are deliberately applied only
# to the child process; callers retain their original environment unchanged.
CODEX_SUBSCRIPTION_PROVIDER_ENV_VARS: tuple[str, ...] = (
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_ORG_ID",
    "OPENAI_ORGANIZATION",
)

CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            *CODEX_SUBSCRIPTION_AUTH_ENV_VARS,
            *CODEX_SUBSCRIPTION_PROVIDER_ENV_VARS,
        )
    )
)

# These overrides are deliberately the final CLI configuration layer for every
# subscription-backed Codex process.  ``model_providers={}`` removes caller/project
# provider definitions while retaining Codex's built-in providers; the two explicit
# URLs pin both ChatGPT and the built-in OpenAI provider to the subscription service.
CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES: tuple[str, ...] = (
    "model_providers={}",
    f'chatgpt_base_url="{CODEX_CHATGPT_SUBSCRIPTION_BASE_URL}"',
    f'openai_base_url="{CODEX_OPENAI_SUBSCRIPTION_BASE_URL}"',
    'forced_login_method="chatgpt"',
    'model_provider="openai"',
)

_CODEX_SUBSCRIPTION_FORBIDDEN_EXACT_PATHS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("chatgpt_base_url",),
        ("openai_base_url",),
        ("model_provider",),
        ("forced_login_method",),
        ("profile",),
        ("cli_auth_credentials_store",),
        ("forced_chatgpt_workspace_id",),
        ("experimental_thread_config_endpoint",),
    }
)
_CODEX_SUBSCRIPTION_FORBIDDEN_TOP_LEVEL_PREFIXES: frozenset[str] = frozenset(
    {"model_providers", "profiles"}
)
_CODEX_SUBSCRIPTION_FORBIDDEN_PATH_SEGMENTS: frozenset[str] = frozenset(
    {
        "api_key",
        "access_token",
        "refresh_token",
        "bearer_token",
        "auth_token",
        "credential",
        "credentials",
        "env_key",
        "env_http_headers",
        "http_headers",
    }
)


def _codex_config_override_key_path(raw: str) -> tuple[str, ...]:
    """Parse one Codex ``-c key=value`` key without inspecting or retaining its value."""

    key_raw, separator, _value = raw.partition("=")
    key = key_raw.strip()
    if not separator or not key:
        raise ValueError("codex_subscription_config_override_malformed")
    try:
        parsed = tomllib.loads(f"{key}=true")
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("codex_subscription_config_override_key_malformed") from exc
    path: list[str] = []
    current: object = parsed
    while isinstance(current, dict) and len(current) == 1:
        part, current = next(iter(current.items()))
        path.append(str(part).strip().lower().replace("-", "_"))
    if current is not True or not path or any(not part for part in path):
        raise ValueError("codex_subscription_config_override_key_ambiguous")
    return tuple(path)


def _codex_subscription_forbidden_override_reason(path: tuple[str, ...]) -> str | None:
    if path in _CODEX_SUBSCRIPTION_FORBIDDEN_EXACT_PATHS:
        return "protected_route_or_auth_key"
    if path[0] in _CODEX_SUBSCRIPTION_FORBIDDEN_TOP_LEVEL_PREFIXES:
        return "protected_route_or_profile_tree"
    if path[:2] == ("debug", "config_lockfile"):
        return "effective_config_replay_forbidden"
    if any(part in _CODEX_SUBSCRIPTION_FORBIDDEN_PATH_SEGMENTS for part in path):
        return "credential_or_header_key_forbidden"
    credential_markers = (
        "api_key",
        "access_token",
        "refresh_token",
        "bearer_token",
        "auth_token",
        "credential",
    )
    if any(
        any(marker in part for marker in credential_markers)
        or part.endswith("_token")
        or part.endswith("_headers")
        for part in path
    ):
        return "credential_or_header_key_forbidden"
    return None


def validate_codex_subscription_config_overrides(
    config_overrides: Iterable[str],
    *,
    source: str,
) -> list[str]:
    """Return safe ordinary Codex overrides or fail before a subscription process starts."""

    validated: list[str] = []
    for index, raw in enumerate(config_overrides):
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"codex_subscription_config_override_invalid:{source}:{index}")
        try:
            path = _codex_config_override_key_path(raw)
        except ValueError as exc:
            raise ValueError(
                f"codex_subscription_config_override_invalid:{source}:{index}:{exc}"
            ) from exc
        reason = _codex_subscription_forbidden_override_reason(path)
        if reason is not None:
            rendered_path = ".".join(path)
            raise ValueError(
                "codex_subscription_config_override_forbidden:"
                f"{source}:{index}:{rendered_path}:{reason}"
            )
        validated.append(raw.strip())
    return validated


def build_codex_subscription_config_overrides(
    config_overrides: Iterable[str],
    *,
    source: str,
    internal_safe_overrides: Iterable[str] = (),
) -> list[str]:
    """Build one sanitized effective list with canonical subscription routing last."""

    caller = validate_codex_subscription_config_overrides(config_overrides, source=source)
    internal = validate_codex_subscription_config_overrides(
        internal_safe_overrides,
        source=f"{source}:internal",
    )
    return [*caller, *internal, *CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES]


def codex_subscription_config_errors(config_overrides: Iterable[str]) -> list[str]:
    """Verify canonical final precedence and the absence of earlier routing/auth overrides."""

    values = list(config_overrides)
    errors: list[str] = []
    suffix_length = len(CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES)
    if values[-suffix_length:] != list(CODEX_SUBSCRIPTION_ROUTE_CONFIG_OVERRIDES):
        errors.append("codex_subscription_canonical_route_suffix_missing")
        safe_prefix = values
    else:
        safe_prefix = values[:-suffix_length]
    try:
        validate_codex_subscription_config_overrides(
            safe_prefix,
            source="effective",
        )
    except ValueError as exc:
        errors.append(str(exc))
    return list(dict.fromkeys(errors))


@dataclass(frozen=True)
class CodexExecResult:
    argv: list[str]
    exit_code: int
    raw_events_path: Path
    last_message_path: Path
    stderr_path: Path
    thread_id: str | None = None
    terminal_turn_salvaged: bool = False


def _canonical_codex_thread_id(value: str) -> str:
    """Accept only an exact UUID captured from a prior Codex thread.started event."""
    normalized = str(value).strip()
    try:
        parsed = UUID(normalized)
    except (ValueError, AttributeError) as exc:
        raise ValueError("codex_resume_session_id_invalid") from exc
    canonical = str(parsed)
    if normalized.casefold() != canonical:
        raise ValueError("codex_resume_session_id_not_canonical")
    return canonical


def _codex_thread_id_from_raw_events(path: Path) -> str | None:
    """Extract one unambiguous, canonical Codex session id from JSONL output."""
    if not path.is_file():
        return None
    observed: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "thread.started":
            continue
        raw = event.get("thread_id")
        if not isinstance(raw, str):
            continue
        try:
            observed.add(_canonical_codex_thread_id(raw))
        except ValueError:
            continue
    return next(iter(observed)) if len(observed) == 1 else None


@dataclass(frozen=True)
class CodexLoginStatusResult:
    """Result of a non-interactive Codex login-mode check.

    Raw output remains available to the caller for local diagnostics. Persisted receipts
    should use :meth:`to_redacted_dict`, which never includes command output or environment
    values.
    """

    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    codex_home: str
    auth_env_vars_blank: dict[str, bool]
    error_kind: str | None = None

    @property
    def normalized_status_output(self) -> str:
        normalized_outputs = [
            value.replace("\r\n", "\n").replace("\r", "\n").strip()
            for value in (self.stdout, self.stderr)
        ]
        nonempty = [value for value in normalized_outputs if value]
        return nonempty[0] if len(nonempty) == 1 else "\n".join(nonempty)

    @property
    def ok(self) -> bool:
        return (
            self.exit_code == 0
            and self.error_kind is None
            and self.normalized_status_output == _CHATGPT_LOGIN_STATUS
            and all(
                self.auth_env_vars_blank.get(name) is True
                for name in CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS
            )
        )

    def to_redacted_dict(self) -> dict[str, object]:
        normalized = self.normalized_status_output
        if normalized == _CHATGPT_LOGIN_STATUS:
            status_kind = "chatgpt"
        elif normalized.lower().startswith("logged in using an api key"):
            status_kind = "api_key"
        elif normalized:
            status_kind = "unexpected"
        else:
            status_kind = "missing"
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "expected_status": _CHATGPT_LOGIN_STATUS,
            "status_kind": status_kind,
            "chatgpt_status_exact": normalized == _CHATGPT_LOGIN_STATUS,
            "stdout_length": len(self.stdout.encode("utf-8", errors="replace")),
            "stderr_length": len(self.stderr.encode("utf-8", errors="replace")),
            "codex_home": self.codex_home,
            "auth_env_vars_blank": dict(self.auth_env_vars_blank),
            "error_kind": self.error_kind,
            "argv": list(self.argv),
        }


@dataclass(frozen=True)
class CodexPersonalityConfigIssue:
    message: str
    hint: str
    details: dict[str, object]


@dataclass(frozen=True)
class CodexReasoningEffortConfigIssue:
    message: str
    hint: str
    details: dict[str, object]


_EMPTY_OVERRIDE_VALUES: frozenset[str] = frozenset({"", "[]", "{}", "''", '""'})
_CODEX_REASONING_EFFORT_ALLOWED_VALUES: tuple[str, ...] = ("minimal", "low", "medium", "high")


def _override_key_matches_suffix(*, key: str, suffix: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return normalized == suffix or normalized.endswith("." + suffix)


def _override_value_is_present(value: str) -> bool:
    compact = value.strip().replace(" ", "")
    return compact.lower() not in _EMPTY_OVERRIDE_VALUES


def _normalize_override_value(value: str) -> str:
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'"}:
        normalized = normalized[1:-1]
    return normalized.strip()


def validate_codex_personality_config_overrides(
    config_overrides: Iterable[str],
) -> CodexPersonalityConfigIssue | None:
    """
    Validate Codex config overrides for personality/model_messages consistency.

    Codex warns and silently falls back to base instructions when personality is requested but
    model_messages is absent. The runner treats this as an invalid configuration to keep behavior
    deterministic.
    """

    overrides = [item for item in config_overrides if isinstance(item, str)]
    personality_keys: list[str] = []
    model_messages_keys: list[str] = []
    malformed_overrides: list[str] = []

    for raw in overrides:
        key_raw, sep, value_raw = raw.partition("=")
        key = key_raw.strip()
        value = value_raw.strip()
        if not sep or not key:
            malformed_overrides.append(raw)
            continue
        # Codex has used both `personality` and `model_personality` naming across versions.
        if _override_key_matches_suffix(
            key=key, suffix="personality"
        ) or _override_key_matches_suffix(key=key, suffix="model_personality"):
            if _override_value_is_present(value):
                personality_keys.append(key)
            continue
        if _override_key_matches_suffix(key=key, suffix="model_messages"):
            if _override_value_is_present(value):
                model_messages_keys.append(key)

    if personality_keys and not model_messages_keys:
        details: dict[str, object] = {
            "personality_keys": sorted(set(personality_keys)),
            "model_messages_keys": [],
            "overrides_checked": overrides,
        }
        if malformed_overrides:
            details["malformed_overrides"] = malformed_overrides
        return CodexPersonalityConfigIssue(
            message=(
                "Codex personality was requested but model_messages is missing. "
                "This runner will fail fast to avoid silently falling back to base instructions."
            ),
            hint=(
                "Remove the personality override for headless runs, or pass a known-good "
                "Codex configuration where personality/model_personality and model_messages "
                "are paired."
            ),
            details=details,
        )

    return None


def validate_codex_reasoning_effort_config_overrides(
    config_overrides: Iterable[str],
) -> CodexReasoningEffortConfigIssue | None:
    """
    Validate Codex `model_reasoning_effort` overrides and surface actionable guidance.

    Codex rejects unknown enum values (for example `xhigh`) during startup.
    """

    overrides = [item for item in config_overrides if isinstance(item, str)]
    invalid_entries: list[dict[str, str]] = []
    matched_keys: list[str] = []

    for raw in overrides:
        key_raw, sep, value_raw = raw.partition("=")
        key = key_raw.strip()
        if not sep or not key:
            continue
        if not _override_key_matches_suffix(key=key, suffix="model_reasoning_effort"):
            continue

        matched_keys.append(key)
        normalized_value = _normalize_override_value(value_raw)
        if not normalized_value:
            continue
        if normalized_value.lower() in _CODEX_REASONING_EFFORT_ALLOWED_VALUES:
            continue
        invalid_entries.append({"override": raw, "value": normalized_value})

    if not invalid_entries:
        return None

    invalid_values = sorted({item["value"] for item in invalid_entries})
    allowed = ", ".join(_CODEX_REASONING_EFFORT_ALLOWED_VALUES)
    return CodexReasoningEffortConfigIssue(
        message=(
            f"Codex config override model_reasoning_effort is invalid: {', '.join(invalid_values)}."
        ),
        hint=(
            "Use one of the supported values "
            f"({allowed}). Example: --agent-config model_reasoning_effort=high."
        ),
        details={
            "keys": sorted(set(matched_keys)),
            "invalid_entries": invalid_entries,
            "allowed_values": list(_CODEX_REASONING_EFFORT_ALLOWED_VALUES),
        },
    )


def _windows_desktop_codex_candidates(*, local_app_data: str | None) -> list[Path]:
    """Return Desktop-managed Codex executables from newest to oldest.

    Codex Desktop extracts its host CLI below ``%LOCALAPPDATA%/OpenAI/Codex/bin``
    into content-addressed directories. The directory name is intentionally opaque, so
    discovery must not pin a particular installation hash or Windows user profile.
    """

    if not isinstance(local_app_data, str) or not local_app_data.strip():
        return []

    bin_dir = Path(local_app_data).expanduser() / _WINDOWS_DESKTOP_CODEX_RELATIVE_BIN
    try:
        children = list(bin_dir.iterdir())
    except OSError:
        return []

    candidates: list[tuple[int, str, Path]] = []
    direct_candidate = bin_dir / "codex.exe"
    possible_candidates = [direct_candidate]
    for child in children:
        try:
            if child.is_dir():
                possible_candidates.append(child / "codex.exe")
        except OSError:
            continue
    for candidate in possible_candidates:
        try:
            if not candidate.is_file():
                continue
            modified_ns = candidate.stat().st_mtime_ns
        except OSError:
            continue
        candidates.append((modified_ns, str(candidate).casefold(), candidate))

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [candidate for _modified_ns, _path_key, candidate in candidates]


def _codex_executable_is_usable(candidate: Path) -> bool:
    """Check that a discovered Desktop CLI can execute its non-network version probe."""

    try:
        completed = subprocess.run(
            [str(candidate), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_WINDOWS_DESKTOP_CODEX_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _resolve_windows_desktop_codex_executable(*, local_app_data: str | None) -> str | None:
    for candidate in _windows_desktop_codex_candidates(local_app_data=local_app_data):
        if _codex_executable_is_usable(candidate):
            return str(candidate)
    return None


def resolve_codex_executable(
    binary: str,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve the configured Codex executable for a local host invocation.

    Explicit paths remain authoritative. On Windows, only the plain ``codex``/``codex.exe``
    configuration receives Desktop-aware resolution: a usable CLI managed by the signed-in
    Desktop installation is preferred over an older npm shim on ``PATH``. If Desktop is absent
    or its retained candidates cannot execute, ordinary ``PATH`` resolution remains the fallback.
    """

    p = Path(binary)
    if p.is_absolute():
        return str(p)

    # Treat anything with a path separator or drive spec as an explicit path, not a PATH lookup.
    if any(sep in binary for sep in ("/", "\\")) or (os.name == "nt" and ":" in binary):
        return binary

    effective_env: Mapping[str, str] = os.environ if env is None else env
    if os.name == "nt" and binary.strip().casefold() in _PLAIN_CODEX_BINARY_NAMES:
        desktop_binary = _resolve_windows_desktop_codex_executable(
            local_app_data=effective_env.get("LOCALAPPDATA")
        )
        if desktop_binary is not None:
            return desktop_binary

    resolved = shutil.which(binary, path=effective_env.get("PATH"))
    return resolved if resolved is not None else binary


def _resolve_executable(binary: str) -> str:
    """Backward-compatible private alias for the public Codex resolver."""

    return resolve_codex_executable(binary)


def _codex_program_argv(binary: str) -> tuple[list[str], str]:
    """Resolve a Codex command without invoking a shell."""

    resolved_binary = _resolve_executable(binary)
    if os.name == "nt" and resolved_binary.lower().endswith((".cmd", ".bat")):
        codex_js = (
            Path(resolved_binary).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        )
        if codex_js.exists():
            node_binary = shutil.which("node") or "node"
            return [node_binary, str(codex_js)], resolved_binary
    return [resolved_binary], resolved_binary


def probe_codex_login_status(
    *,
    binary: str = "codex",
    codex_home: Path | str,
    cwd: Path | str,
    config_overrides: Iterable[str] = (),
    env_overrides: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
) -> CodexLoginStatusResult:
    """Require the configured Codex CLI to report the host ChatGPT login mode.

    This helper deliberately bypasses a shell, blanks every supported alternate/API
    credential variable, and returns a receipt-safe result separately from raw local
    diagnostics. It does not make a model request; callers must pair it with an actual
    Codex activation probe before treating the subscription credential as usable.
    """

    codex_home_value = str(Path(codex_home).resolve())
    env = os.environ.copy()
    if env_overrides is not None:
        env.update(env_overrides)
    env["CODEX_HOME"] = codex_home_value
    for name in CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS:
        env[name] = ""
    auth_env_vars_blank = {
        name: env.get(name, "") == "" for name in CODEX_SUBSCRIPTION_BLOCKED_ENV_VARS
    }

    argv: list[str] = [binary, "login", "status"]
    try:
        program_argv, _ = _codex_program_argv(binary)
        argv = list(program_argv)
        for override in config_overrides:
            argv.extend(["-c", override])
        argv.extend(["login", "status"])
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_seconds,
        )
        return CodexLoginStatusResult(
            argv=argv,
            exit_code=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            codex_home=codex_home_value,
            auth_env_vars_blank=auth_env_vars_blank,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return CodexLoginStatusResult(
            argv=argv,
            exit_code=124,
            stdout=stdout,
            stderr=stderr,
            codex_home=codex_home_value,
            auth_env_vars_blank=auth_env_vars_blank,
            error_kind="TimeoutExpired",
        )
    except OSError as exc:
        return CodexLoginStatusResult(
            argv=argv,
            exit_code=1,
            stdout="",
            stderr="",
            codex_home=codex_home_value,
            auth_env_vars_blank=auth_env_vars_blank,
            error_kind=type(exc).__name__,
        )


def _scrub_prompt(argv: list[str]) -> list[str]:
    if not argv:
        return []
    scrubbed = argv.copy()
    if scrubbed[-1] not in {"-", "<prompt>"}:
        scrubbed[-1] = "<prompt>"
    return scrubbed


def _strip_codex_log_prefix(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return stripped
    z_index = stripped.find("Z ")
    if z_index > 0 and stripped[:4].isdigit():
        return stripped[z_index + 2 :].lstrip()
    return stripped


def _rewrite_refresh_token_reused_stderr(path: Path) -> None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    excerpt_lines: list[str] = []
    for line in raw.splitlines():
        if _CODEX_REFRESH_TOKEN_REUSED_SUBSTRING in line:
            excerpt_lines.append(_strip_codex_log_prefix(line))
            break
    for line in raw.splitlines():
        if "Please log out and sign in again" in line:
            excerpt_lines.append(_strip_codex_log_prefix(line))
            break

    excerpt_deduped: list[str] = []
    seen: set[str] = set()
    for line in excerpt_lines:
        if not line or line in seen:
            continue
        excerpt_deduped.append(line)
        seen.add(line)

    summary_lines = [
        "Codex authentication failed: refresh_token_reused.",
        "",
        "This usually means your stored Codex login state is invalid (refresh token already used).",
        "Fix by re-authenticating once:",
        "  - codex logout",
        "  - codex login",
        "",
        "Alternative (API key login):",
        "  - macOS/Linux: printenv OPENAI_API_KEY | codex login --with-api-key",
        "  - PowerShell:  $env:OPENAI_API_KEY | codex login --with-api-key",
    ]
    if excerpt_deduped:
        summary_lines.append("")
        summary_lines.append("Codex stderr excerpt:")
        summary_lines.extend([f"  {line}" for line in excerpt_deduped])

    summary_lines.append("")
    path.write_text("\n".join(summary_lines), encoding="utf-8", newline="\n")


def _stderr_contains_refresh_token_reused(path: Path, *, full_scan: bool) -> bool:
    needle = _CODEX_REFRESH_TOKEN_REUSED_SUBSTRING.encode("utf-8")
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size <= 0:
        return False

    read_size = size if full_scan else min(size, 8192)
    try:
        with path.open("rb") as stderr_reader:
            if not full_scan and size > read_size:
                stderr_reader.seek(size - read_size)
            overlap = b""
            while True:
                chunk = stderr_reader.read(65536)
                if not chunk:
                    return False
                haystack = overlap + chunk
                if needle in haystack:
                    return True
                overlap = haystack[-(len(needle) - 1) :] if len(needle) > 1 else b""
    except OSError:
        return False


def _prepare_codex_argv_and_env(
    *,
    argv: list[str],
    prefix: list[str],
    env_overrides: dict[str, str] | None,
) -> tuple[list[str], dict[str, str] | None]:
    if prefix:
        if env_overrides is None:
            return [*prefix, *argv], None
        if looks_like_docker_exec_prefix(prefix):
            return [*inject_docker_exec_env(prefix, env_overrides), *argv], None

        env = os.environ.copy()
        env.update(env_overrides)
        return [*prefix, *argv], env

    if env_overrides is None:
        return argv, None

    env = os.environ.copy()
    env.update(env_overrides)
    return argv, env


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return
        except Exception:
            pass
    proc.kill()


def run_codex_exec(
    *,
    workspace_dir: Path | str,
    prompt: str,
    raw_events_path: Path,
    last_message_path: Path,
    stderr_path: Path,
    sandbox: str,
    ask_for_approval: str | None = None,
    binary: str = "codex",
    subcommand: str = "exec",
    model: str | None = None,
    timeout_seconds: float | None = None,
    config_overrides: Iterable[str] = (),
    ignore_user_config: bool = True,
    ignore_rules: bool = False,
    skip_git_repo_check: bool = False,
    command_prefix: Iterable[str] = (),
    env_overrides: dict[str, str] | None = None,
    agent_last_message_path: str | None = None,
    resume_session_id: str | None = None,
) -> CodexExecResult:
    raw_events_path.parent.mkdir(parents=True, exist_ok=True)
    last_message_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    raw_events_ts_path = raw_events_path.with_suffix(".ts.jsonl")

    prefix = [p for p in command_prefix if isinstance(p, str) and p]

    if prefix:
        resolved_binary = binary
        argv: list[str] = [resolved_binary]
    else:
        argv, resolved_binary = _codex_program_argv(binary)
    if ask_for_approval is not None:
        argv.extend(["--ask-for-approval", ask_for_approval])

    canonical_resume_session_id = (
        _canonical_codex_thread_id(resume_session_id)
        if resume_session_id is not None
        else None
    )
    if canonical_resume_session_id is not None and subcommand != "exec":
        raise ValueError("codex_resume_requires_exec_subcommand")
    # The `exec resume` subparser does not accept --cd/--sandbox, but both are top-level Codex
    # options. Reassert the runner-owned workspace and sandbox before `exec`; otherwise a resumed
    # invocation falls back to Codex's read-only permission profile instead of preserving the
    # effective policy used by the original turn.
    if canonical_resume_session_id is not None:
        argv.extend(["--cd", str(workspace_dir), "--sandbox", sandbox])
    argv.append(subcommand)
    if canonical_resume_session_id is not None:
        argv.append("resume")
    if ignore_user_config:
        argv.append("--ignore-user-config")
    argv.append("--json")
    # Fresh `exec` accepts these options directly. Resume receives the same values as top-level
    # options above while retaining the exact session id below.
    if canonical_resume_session_id is None:
        argv.extend(["--cd", str(workspace_dir), "--sandbox", sandbox])
    if ignore_rules:
        argv.append("--ignore-rules")
    if skip_git_repo_check:
        argv.append("--skip-git-repo-check")
    if model is not None:
        argv.extend(["--model", model])
    for override in config_overrides:
        argv.extend(["-c", override])
    effective_last_message_path = Path(agent_last_message_path or str(last_message_path))
    argv.extend(["--output-last-message", str(effective_last_message_path)])
    if canonical_resume_session_id is not None:
        argv.append(canonical_resume_session_id)
    argv.append("-")

    full_argv, env = _prepare_codex_argv_and_env(
        argv=argv,
        prefix=prefix,
        env_overrides=env_overrides,
    )

    saw_refresh_token_reused = False
    with (
        raw_events_path.open("w", encoding="utf-8", newline="\n") as stdout_f,
        raw_events_ts_path.open("w", encoding="utf-8", newline="\n") as ts_f,
        stderr_path.open("w", encoding="utf-8", newline="\n") as stderr_f,
    ):
        effective_timeout_seconds = timeout_seconds
        if effective_timeout_seconds is None:
            timeout_raw = os.environ.get("AGENT_ADAPTERS_CODEX_TIMEOUT_SECONDS")
            if timeout_raw is None or not timeout_raw.strip():
                timeout_raw = os.environ.get("USERTEST_CODEX_TIMEOUT_SECONDS")

            if timeout_raw is not None and timeout_raw.strip():
                try:
                    effective_timeout_seconds = float(timeout_raw)
                except ValueError:
                    stderr_f.write(
                        "Invalid Codex timeout setting; expected seconds as a number.\n"
                        f"got={timeout_raw!r}\n"
                        "Tip: set AGENT_ADAPTERS_CODEX_TIMEOUT_SECONDS "
                        "or pass timeout_seconds=...\n"
                    )
                    effective_timeout_seconds = None

        try:
            proc = subprocess.Popen(
                full_argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_f,
                text=True,
                encoding="utf-8",
                env=env,
                cwd=None if prefix else str(workspace_dir),
            )
        except FileNotFoundError as e:
            stderr_f.write(
                "Failed to launch Codex CLI process.\n"
                f"binary={binary!r}\n"
                f"resolved={resolved_binary!r}\n"
                f"argv={_scrub_prompt(full_argv)!r}\n"
            )
            if prefix:
                raise RuntimeError(
                    f"Could not launch sandbox exec prefix. prefix={prefix!r}"
                ) from e
            raise RuntimeError(
                "Could not launch Codex CLI process. "
                f"binary={binary!r} resolved={resolved_binary!r}. "
                "On Windows, ensure the Codex executable is on PATH and consider setting "
                "configs/agents.yaml `agents.codex.binary` to the full path shown by `where codex`."
            ) from e

        if proc.stdin is not None:
            try:
                proc.stdin.write(prompt)
            except BrokenPipeError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        saw_apply_patch_approval_request = threading.Event()
        saw_terminal_turn_completed = threading.Event()

        def _stream_stdout() -> None:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                stdout_f.write(line)
                stdout_f.flush()
                if line.strip():
                    ts_f.write(utc_now_iso() + "\n")
                    ts_f.flush()
                # Avoid false positives if the agent prints this token in normal output.
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") == "apply_patch_approval_request":
                    saw_apply_patch_approval_request.set()
                    continue
                if payload.get("type") == "turn.completed":
                    saw_terminal_turn_completed.set()
                    continue
                msg = payload.get("msg")
                if isinstance(msg, dict) and msg.get("type") == "apply_patch_approval_request":
                    saw_apply_patch_approval_request.set()
                if isinstance(msg, dict) and msg.get("type") == "turn.completed":
                    saw_terminal_turn_completed.set()

        reader = threading.Thread(target=_stream_stdout, daemon=True)
        reader.start()

        start = time.monotonic()
        last_auth_scan = start - 1.0
        terminal_completion_observed_at: float | None = None
        terminal_turn_salvaged = False
        while True:
            if not saw_refresh_token_reused and (time.monotonic() - last_auth_scan) > 0.2:
                last_auth_scan = time.monotonic()
                try:
                    if _stderr_contains_refresh_token_reused(stderr_path, full_scan=False):
                        saw_refresh_token_reused = True
                        stderr_f.write(
                            f"\n{_CODEX_REFRESH_TOKEN_REUSED_MARKER}\n"
                            "Codex returned a non-retriable auth error. Terminating early.\n"
                        )
                        stderr_f.flush()
                        _kill_process_tree(proc)
                        break
                except Exception:
                    pass

            if saw_apply_patch_approval_request.is_set():
                stderr_f.write(
                    "Codex emitted apply_patch_approval_request and is waiting for interactive "
                    "approval.\n"
                    "This library runs Codex in headless mode and cannot respond to approvals, "
                    "so the process\n"
                    "was terminated to avoid hanging.\n"
                    "\n"
                    "Workarounds:\n"
                    "- Configure Codex to avoid interactive approval "
                    '(for example ask_for_approval="never"), or\n'
                    "- Run Codex interactively.\n"
                )
                stderr_f.flush()
                _kill_process_tree(proc)
                break

            if saw_terminal_turn_completed.is_set() and effective_last_message_path.is_file():
                if terminal_completion_observed_at is None:
                    terminal_completion_observed_at = time.monotonic()
                elif time.monotonic() - terminal_completion_observed_at >= 2.0:
                    stderr_f.write(
                        "Codex emitted turn.completed and persisted output-last-message, but "
                        "the CLI process remained alive after protocol completion. Cleaned up "
                        "the orphaned process tree and retained the completed turn.\n"
                    )
                    stderr_f.flush()
                    terminal_turn_salvaged = True
                    _kill_process_tree(proc)
                    break

            if (
                effective_timeout_seconds is not None
                and (time.monotonic() - start) > effective_timeout_seconds
            ):
                stderr_f.write(
                    f"Codex CLI timed out after {effective_timeout_seconds:.1f}s; "
                    "terminating to avoid hanging.\n"
                    "You can increase/disable this via timeout_seconds=... or "
                    "AGENT_ADAPTERS_CODEX_TIMEOUT_SECONDS.\n"
                )
                stderr_f.flush()
                _kill_process_tree(proc)
                break

            if proc.poll() is not None:
                break

            time.sleep(0.05)

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # Keep moving and return a failure result; avoid hanging here.
                    pass

        try:
            reader.join(timeout=5)
        except Exception:
            pass

    if not saw_refresh_token_reused:
        saw_refresh_token_reused = _stderr_contains_refresh_token_reused(
            stderr_path, full_scan=True
        )
    if saw_refresh_token_reused:
        _rewrite_refresh_token_reused_stderr(stderr_path)

    observed_thread_id = _codex_thread_id_from_raw_events(raw_events_path)
    if (
        canonical_resume_session_id is not None
        and observed_thread_id is not None
        and observed_thread_id != canonical_resume_session_id
    ):
        raise RuntimeError("codex_resume_session_id_changed")
    return CodexExecResult(
        argv=full_argv,
        exit_code=(
            0
            if terminal_turn_salvaged
            else proc.returncode
            if proc.returncode is not None
            else 1
        ),
        raw_events_path=raw_events_path,
        last_message_path=last_message_path,
        stderr_path=stderr_path,
        thread_id=observed_thread_id or canonical_resume_session_id,
        terminal_turn_salvaged=terminal_turn_salvaged,
    )
