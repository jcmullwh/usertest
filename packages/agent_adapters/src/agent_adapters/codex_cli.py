from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from agent_adapters.docker_exec_env import inject_docker_exec_env, looks_like_docker_exec_prefix
from agent_adapters.events import utc_now_iso

_CODEX_REFRESH_TOKEN_REUSED_MARKER = "[usertest] detected codex auth error: refresh_token_reused"
_CODEX_REFRESH_TOKEN_REUSED_SUBSTRING = "refresh_token_reused"


@dataclass(frozen=True)
class CodexExecResult:
    argv: list[str]
    exit_code: int
    raw_events_path: Path
    last_message_path: Path
    stderr_path: Path


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


def _classify_override_state(value: str) -> str:
    """
    Classify a Codex config override value into one of three states.

    Returns:
        "absent": No meaningful value provided (backwards compat with _override_value_is_present)
        "explicit_empty": An explicit empty marker was provided (e.g., "", '""', "[]")
        "present_non_empty": A non-empty value was provided

    This distinction matters for validation: explicit empty values should be rejected
    before Codex config.toml loading, while truly absent values are acceptable.
    """
    stripped = value.strip()

    # If the raw stripped value is completely empty, treat as absent for backwards compatibility
    if not stripped:
        return "absent"

    # Check if it's an explicit empty marker
    compact = stripped.replace(" ", "").lower()
    if compact in _EMPTY_OVERRIDE_VALUES:
        return "explicit_empty"

    # Otherwise it's a present non-empty value
    return "present_non_empty"


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

    This validator now distinguishes between absent, explicit empty, and present non-empty
    override values to prevent invalid configurations from reaching Codex config.toml loading.
    """

    overrides = [item for item in config_overrides if isinstance(item, str)]
    personality_keys: list[str] = []
    personality_explicit_empty_keys: list[str] = []
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
        if (
            _override_key_matches_suffix(key=key, suffix="personality")
            or _override_key_matches_suffix(key=key, suffix="model_personality")
        ):
            state = _classify_override_state(value)
            if state == "present_non_empty":
                personality_keys.append(key)
            elif state == "explicit_empty":
                personality_explicit_empty_keys.append(key)
            continue
        if _override_key_matches_suffix(key=key, suffix="model_messages"):
            state = _classify_override_state(value)
            if state == "present_non_empty":
                model_messages_keys.append(key)

    # Reject explicit empty personality/model_personality values
    if personality_explicit_empty_keys:
        details: dict[str, object] = {
            "explicit_empty_personality_keys": sorted(set(personality_explicit_empty_keys)),
            "overrides_checked": overrides,
        }
        if malformed_overrides:
            details["malformed_overrides"] = malformed_overrides
        return CodexPersonalityConfigIssue(
            message=(
                "Codex personality override was provided with an explicit empty value. "
                "This runner rejects explicit empty values to avoid later Codex startup failures."
            ),
            hint=(
                "Remove the personality override entirely, or provide a valid non-empty value. "
                "Do not use explicit empty markers like personality=\"\" or personality='\"\"'."
            ),
            details=details,
        )

    # Reject non-empty personality without model_messages
    if personality_keys and not model_messages_keys:
        details = {
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
    This validator uses the same override state classification as personality validation
    to ensure consistent handling of explicit empty values.
    """

    overrides = [item for item in config_overrides if isinstance(item, str)]
    invalid_entries: list[dict[str, str]] = []
    matched_keys: list[str] = []
    explicit_empty_keys: list[str] = []

    for raw in overrides:
        key_raw, sep, value_raw = raw.partition("=")
        key = key_raw.strip()
        if not sep or not key:
            continue
        if not _override_key_matches_suffix(key=key, suffix="model_reasoning_effort"):
            continue

        matched_keys.append(key)
        state = _classify_override_state(value_raw)

        if state == "absent":
            continue
        elif state == "explicit_empty":
            explicit_empty_keys.append(key)
            continue

        # For present_non_empty values, validate against allowed values
        normalized_value = _normalize_override_value(value_raw)
        if normalized_value.lower() in _CODEX_REASONING_EFFORT_ALLOWED_VALUES:
            continue
        invalid_entries.append({"override": raw, "value": normalized_value})

    # Reject explicit empty values
    if explicit_empty_keys:
        allowed = ", ".join(_CODEX_REASONING_EFFORT_ALLOWED_VALUES)
        return CodexReasoningEffortConfigIssue(
            message=(
                "Codex model_reasoning_effort override was provided with an explicit empty value."
            ),
            hint=(
                "Remove the model_reasoning_effort override entirely, or use one of the supported values "
                f"({allowed}). Do not use explicit empty markers."
            ),
            details={
                "explicit_empty_keys": sorted(set(explicit_empty_keys)),
                "allowed_values": list(_CODEX_REASONING_EFFORT_ALLOWED_VALUES),
            },
        )

    # Reject invalid values
    if not invalid_entries:
        return None

    invalid_values = sorted({item["value"] for item in invalid_entries})
    allowed = ", ".join(_CODEX_REASONING_EFFORT_ALLOWED_VALUES)
    return CodexReasoningEffortConfigIssue(
        message=(
            "Codex config override model_reasoning_effort is invalid: "
            f"{', '.join(invalid_values)}."
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


def _resolve_executable(binary: str) -> str:
    p = Path(binary)
    if p.is_absolute():
        return str(p)

    # Treat anything with a path separator or drive spec as an explicit path, not a PATH lookup.
    if any(sep in binary for sep in ("/", "\\")) or (os.name == "nt" and ":" in binary):
        return binary

    resolved = shutil.which(binary)
    return resolved if resolved is not None else binary


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
    ignore_rules: bool = False,
    skip_git_repo_check: bool = False,
    command_prefix: Iterable[str] = (),
    env_overrides: dict[str, str] | None = None,
    agent_last_message_path: str | None = None,
) -> CodexExecResult:
    raw_events_path.parent.mkdir(parents=True, exist_ok=True)
    last_message_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    raw_events_ts_path = raw_events_path.with_suffix(".ts.jsonl")

    prefix = [p for p in command_prefix if isinstance(p, str) and p]

    resolved_binary = binary if prefix else _resolve_executable(binary)

    argv: list[str]
    if not prefix and os.name == "nt" and resolved_binary.lower().endswith((".cmd", ".bat")):
        codex_js = (
            Path(resolved_binary).parent
            / "node_modules"
            / "@openai"
            / "codex"
            / "bin"
            / "codex.js"
        )
        if codex_js.exists():
            node_binary = shutil.which("node") or "node"
            argv = [node_binary, str(codex_js)]
        else:
            argv = [resolved_binary]
    else:
        argv = [resolved_binary]
    if ask_for_approval is not None:
        argv.extend(["--ask-for-approval", ask_for_approval])

    argv.extend(
        [
            subcommand,
            "--ignore-user-config",
            "--json",
            "--cd",
            str(workspace_dir),
            "--sandbox",
            sandbox,
        ]
    )
    if ignore_rules:
        argv.append("--ignore-rules")
    if skip_git_repo_check:
        argv.append("--skip-git-repo-check")
    if model is not None:
        argv.extend(["--model", model])
    for override in config_overrides:
        argv.extend(["-c", override])
    argv.extend(["--output-last-message", agent_last_message_path or str(last_message_path)])
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
                    "Could not launch sandbox exec prefix. "
                    f"prefix={prefix!r}"
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
                msg = payload.get("msg")
                if isinstance(msg, dict) and msg.get("type") == "apply_patch_approval_request":
                    saw_apply_patch_approval_request.set()

        reader = threading.Thread(target=_stream_stdout, daemon=True)
        reader.start()

        start = time.monotonic()
        last_auth_scan = start - 1.0
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
                    "(for example ask_for_approval=\"never\"), or\n"
                    "- Run Codex interactively.\n"
                )
                stderr_f.flush()
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

    return CodexExecResult(
        argv=full_argv,
        exit_code=proc.returncode if proc.returncode is not None else 1,
        raw_events_path=raw_events_path,
        last_message_path=last_message_path,
        stderr_path=stderr_path,
    )
