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
    model_messages is absent.
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
        if (
            _override_key_matches_suffix(key=key, suffix="personality")
            or _override_key_matches_suffix(key=key, suffix="model_personality")
        ):
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
                "Codex will warn and fall back to base instructions."
            ),
            hint=(
                "Add model_messages in configs/agents.yaml agents.codex.config_overrides "
                "or pass --agent-config model_messages=... alongside personality/model_personality "
                "to make the personality take effect."
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
    skip_git_repo_check: bool = False,
    command_prefix: Iterable[str] = (),
    env_overrides: dict[str, str] | None = None,
    agent_last_message_path: str | None = None,
) -> CodexExecResult:
    raw_events_path.parent.mkdir(parents=True, exist_ok=True)
    last_message_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    prefix = [p for p in command_prefix if isinstance(p, str) and p]

    resolved_binary = binary if prefix else _resolve_executable(binary)
    argv: list[str] = [
        resolved_binary,
    ]
    if ask_for_approval is not None:
        argv.extend(["--ask-for-approval", ask_for_approval])

    argv.extend(
        [
            subcommand,
            "--json",
            "--cd",
            str(workspace_dir),
            "--sandbox",
            sandbox,
        ]
    )
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
    with raw_events_path.open("w", encoding="utf-8", newline="\n") as stdout_f, stderr_path.open(
        "w", encoding="utf-8", newline="\n"
    ) as stderr_f:
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
                    if stderr_path.exists():
                        tail = ""
                        try:
                            size = stderr_path.stat().st_size
                        except OSError:
                            size = 0
                        if size > 0:
                            with stderr_path.open("rb") as stderr_reader:
                                if size > 8192:
                                    stderr_reader.seek(size - 8192)
                                tail = stderr_reader.read(8192).decode("utf-8", errors="replace")
                        if _CODEX_REFRESH_TOKEN_REUSED_SUBSTRING in tail:
                            saw_refresh_token_reused = True
                            stderr_f.write(
                                f"\n{_CODEX_REFRESH_TOKEN_REUSED_MARKER}\n"
                                "Codex returned a non-retriable auth error. Terminating early.\n"
                            )
                            stderr_f.flush()
                            proc.kill()
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
                proc.kill()
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
                proc.kill()
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
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # Keep moving and return a failure result; avoid hanging here.
                    pass

        try:
            reader.join(timeout=5)
        except Exception:
            pass

    if saw_refresh_token_reused:
        _rewrite_refresh_token_reused_stderr(stderr_path)

    return CodexExecResult(
        argv=full_argv,
        exit_code=proc.returncode if proc.returncode is not None else 1,
        raw_events_path=raw_events_path,
        last_message_path=last_message_path,
        stderr_path=stderr_path,
    )
