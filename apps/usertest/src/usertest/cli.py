# ruff: noqa: E501
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

try:
    import yaml
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing dependency `pyyaml` (import name: `yaml`). "
        "Fix: `python -m pip install -r requirements-dev.txt`."
    ) from exc
try:
    import jsonschema  # noqa: F401
except ModuleNotFoundError as exc:
    raise SystemExit(
        "Missing dependency `jsonschema`. "
        "Fix: `python -m pip install -r requirements-dev.txt`."
    ) from exc


def _from_source_import_remediation(*, missing_module: str) -> str:
    return (
        f"Missing import `{missing_module}`.\n"
        "This usually means you're running from source without editable installs or PYTHONPATH.\n"
        "\n"
        "Fix (from repo root):\n"
        "  python -m pip install -r requirements-dev.txt\n"
        "  PowerShell: . .\\scripts\\set_pythonpath.ps1\n"
        "  macOS/Linux: source scripts/set_pythonpath.sh\n"
        "\n"
        "Or install editables (recommended):\n"
        "  python -m pip install -e apps/usertest\n"
    )


try:
    from agent_adapters import (
        normalize_claude_events,
        normalize_codex_events,
        normalize_gemini_events,
    )
except ModuleNotFoundError as exc:
    if exc.name == "agent_adapters":
        raise SystemExit(_from_source_import_remediation(missing_module="agent_adapters")) from exc
    raise

try:
    from reporter import (
        analyze_report_history,
        compute_metrics,
        iter_events_jsonl,
        make_event,
        render_report_markdown,
        validate_report,
        write_issue_analysis,
    )
except ModuleNotFoundError as exc:
    if exc.name == "reporter":
        raise SystemExit(_from_source_import_remediation(missing_module="reporter")) from exc
    raise

try:
    from run_artifacts.history import iter_report_history, write_report_history_jsonl
except ModuleNotFoundError as exc:
    if exc.name == "run_artifacts":
        raise SystemExit(_from_source_import_remediation(missing_module="run_artifacts")) from exc
    raise

try:
    from runner_core import RunnerConfig, RunRequest, find_repo_root, run_once
    from runner_core.catalog import discover_missions, discover_personas, load_catalog_config
    from runner_core.pathing import slugify
    from runner_core.run_spec import RunSpecError, resolve_effective_run_inputs
    from runner_core.target_acquire import acquire_target
except ModuleNotFoundError as exc:
    if exc.name == "runner_core":
        raise SystemExit(_from_source_import_remediation(missing_module="runner_core")) from exc
    if exc.name == "sandbox_runner":
        raise SystemExit(_from_source_import_remediation(missing_module="sandbox_runner")) from exc
    raise

_LEGACY_RUN_TIMESTAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
_WINDOWS_ABS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
try:
    from runner_core.python_interpreter_probe import probe_python_interpreters
except ModuleNotFoundError:
    probe_python_interpreters = None  # type: ignore[assignment]


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


_configure_console_output()


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


def _infer_responsiveness_probe_commands(repo_dir: Path) -> set[str]:
    """Infer shell commands to probe for environment responsiveness."""
    commands: set[str] = set()
    if (repo_dir / "package.json").exists():
        commands.update({"node", "npm"})
    return commands


def _probe_command_responsive(*, command: str, timeout_seconds: float) -> str | None:
    """Run a quick command probe and return an error message on failure."""
    if command in {"python", "python3", "py"} and callable(probe_python_interpreters):
        probe = probe_python_interpreters(
            candidate_commands=[command],
            timeout_seconds=max(0.1, timeout_seconds),
        )
        candidate = probe.by_command().get(command)
        if candidate is None or not candidate.present:
            return None
        if candidate.usable:
            return None
        code = candidate.reason_code or "probe_failed"
        reason = candidate.reason or "interpreter health probe failed"
        return (
            f"command {command!r} resolves to an unusable Python interpreter "
            f"({code}): {reason}"
        )

    resolved = shutil.which(command)
    if resolved is None:
        return None
    try:
        subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            f"command {command!r} appears unresponsive (timed out after {timeout_seconds:.1f}s "
            f"running `{command} --version`)."
        )
    except OSError as e:
        return f"command {command!r} probe failed: {e}"
    return None


def _prevalidate_batch_requests(
    *,
    cfg: RunnerConfig,
    repo_root: Path,
    targets_path: Path,
    requests: list[tuple[int, RunRequest]],
    probe_timeout_seconds: float,
    skip_command_responsiveness_probes: bool,
    validate_only: bool,
) -> list[str]:
    """Validate batch requests against catalog and policy constraints."""
    errors: list[str] = []
    local_repos: list[Path] = []
    missing_agent_binaries: dict[tuple[str, str, str], list[int]] = {}

    for idx, req in requests:
        if req.agent not in cfg.agents:
            errors.append(
                f"targets[{idx}]: unknown agent {req.agent!r} (defined in configs/agents.yaml)."
            )
        else:
            agent_cfg = cfg.agents.get(req.agent, {})
            binary = req.agent
            if isinstance(agent_cfg, dict):
                binary_raw = agent_cfg.get("binary")
                if isinstance(binary_raw, str) and binary_raw.strip():
                    binary = binary_raw.strip()
            if binary:
                p = Path(binary)
                is_pathish = (
                    p.is_absolute()
                    or any(sep in binary for sep in ("/", "\\"))
                    or (os.name == "nt" and ":" in binary)
                )
                if is_pathish:
                    if not p.exists():
                        missing_agent_binaries.setdefault(
                            (req.agent, binary, "path_missing"), []
                        ).append(idx)
                elif shutil.which(binary) is None:
                    missing_agent_binaries.setdefault((req.agent, binary, "not_on_path"), []).append(
                        idx
                    )

        if req.policy not in cfg.policies:
            errors.append(
                f"targets[{idx}]: unknown policy {req.policy!r} (defined in configs/policies.yaml)."
            )

        local_repo_root = _resolve_local_repo_root(repo_root, req.repo)
        if local_repo_root is None:
            if _looks_like_local_repo_input(req.repo):
                errors.append(
                    f"targets[{idx}]: repo looks like a local path but does not exist: "
                    f"{req.repo!r} (from {targets_path})"
                )
            continue
        if not local_repo_root.is_dir():
            errors.append(
                f"targets[{idx}]: repo must be a directory (got file): {local_repo_root} "
                f"(from {targets_path})"
            )
            continue

        local_repos.append(local_repo_root)

        try:
            catalog_config = load_catalog_config(repo_root, local_repo_root)
            resolved_inputs = resolve_effective_run_inputs(
                runner_repo_root=repo_root,
                target_repo_root=local_repo_root,
                catalog_config=catalog_config,
                persona_id=req.persona_id,
                mission_id=req.mission_id,
            )
            effective_spec = resolved_inputs.effective
            requires_shell = bool(getattr(resolved_inputs.mission, "requires_shell", False))
            requires_edits = bool(getattr(resolved_inputs.mission, "requires_edits", False))

            policy_cfg = cfg.policies.get(req.policy, {})
            policy_cfg = policy_cfg if isinstance(policy_cfg, dict) else {}
            codex_policy = policy_cfg.get("codex", {})
            codex_policy = codex_policy if isinstance(codex_policy, dict) else {}
            claude_policy = policy_cfg.get("claude", {})
            claude_policy = claude_policy if isinstance(claude_policy, dict) else {}
            gemini_policy = policy_cfg.get("gemini", {})
            gemini_policy = gemini_policy if isinstance(gemini_policy, dict) else {}

            allow_edits = False
            if req.agent == "codex":
                allow_edits = bool(codex_policy.get("allow_edits", False))
            elif req.agent == "claude":
                allow_edits = bool(claude_policy.get("allow_edits", False))
            elif req.agent == "gemini":
                allow_edits = bool(gemini_policy.get("allow_edits", False))

            shell_status = "unknown"
            if req.agent == "claude":
                allowed_tools = claude_policy.get("allowed_tools")
                allowed_tools = allowed_tools if isinstance(allowed_tools, list) else []
                shell_status = "allowed" if "Bash" in allowed_tools else "blocked"
            elif req.agent == "gemini":
                allowed_tools = gemini_policy.get("allowed_tools")
                allowed_tools = allowed_tools if isinstance(allowed_tools, list) else []
                shell_enabled = "run_shell_command" in allowed_tools
                has_outer_sandbox = str(req.exec_backend) == "docker"
                gemini_sandbox_enabled = (
                    bool(gemini_policy.get("sandbox", True))
                    if isinstance(gemini_policy.get("sandbox", True), bool)
                    else True
                )
                if has_outer_sandbox:
                    gemini_sandbox_enabled = False
                if os.name == "nt":
                    gemini_sandbox_enabled = False
                shell_available = has_outer_sandbox or gemini_sandbox_enabled
                if shell_enabled and not shell_available:
                    shell_status = "blocked"
                else:
                    shell_status = "allowed" if shell_enabled else "blocked"

            if requires_shell and shell_status == "blocked":
                hint = "use policy=inspect or policy=write"
                if req.agent == "gemini" and os.name == "nt" and str(req.exec_backend) != "docker":
                    hint = (
                        "use --exec-backend docker (Gemini shell is blocked on Windows local backend) "
                        "and policy=write"
                    )
                errors.append(
                    f"targets[{idx}]: mission {effective_spec.mission_id!r} requires shell "
                    f"commands, but policy {req.policy!r} for agent {req.agent!r} blocks shell "
                    f"commands ({hint})."
                )
            if requires_edits and not allow_edits:
                errors.append(
                    f"targets[{idx}]: mission {effective_spec.mission_id!r} requires edits, but "
                    f"policy {req.policy!r} for agent {req.agent!r} has allow_edits=false "
                    "(use policy=write)."
                )
            if (
                (not requires_shell)
                and req.policy in {"inspect", "write"}
                and shell_status == "blocked"
            ):
                errors.append(
                    f"targets[{idx}]: policy {req.policy!r} for agent {req.agent!r} blocks shell "
                    "commands for this backend (use --exec-backend docker for gemini on Windows, "
                    "or fix configs/policies.yaml)."
                )
        except RunSpecError as e:
            parts = [str(e)]
            if isinstance(e.code, str) and e.code.strip():
                parts.append(f"code={e.code.strip()}")
            if isinstance(e.details, dict) and e.details:
                parts.append(f"details={json.dumps(e.details, ensure_ascii=False)}")
            if isinstance(e.hint, str) and e.hint.strip():
                parts.append(f"hint={e.hint.strip()}")
            errors.append(f"targets[{idx}]: {' | '.join(parts)}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"targets[{idx}]: failed to resolve persona/mission: {e}")

    if not validate_only:
        for (agent, binary, kind), indices in sorted(missing_agent_binaries.items()):
            rendered = ", ".join(f"targets[{idx}]" for idx in sorted(indices))
            if kind == "path_missing":
                errors.append(
                    f"env: agent binary path not found: {binary!r} for agent {agent!r} (used by {rendered})."
                )
            else:
                errors.append(
                    f"env: agent binary not on PATH: {binary!r} for agent {agent!r} (used by {rendered})."
                )

    if skip_command_responsiveness_probes:
        return errors

    commands_to_probe: set[str] = set()
    for repo_dir in local_repos:
        commands_to_probe.update(_infer_responsiveness_probe_commands(repo_dir))
    for cmd in sorted(commands_to_probe):
        probe_error = _probe_command_responsive(
            command=cmd, timeout_seconds=max(0.1, probe_timeout_seconds)
        )
        if probe_error:
            errors.append(f"env: {probe_error}")

    return errors


def build_parser() -> argparse.ArgumentParser:
    """Build the usertest CLI argument parser."""
    parser = argparse.ArgumentParser(prog="usertest")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run a single persona exploration against a target repo.")
    run_p.add_argument(
        "--repo",
        required=True,
        help=(
            "Local path, git URL, or `pip:<requirement...>` / `pdm:<requirement...>` to evaluate "
            "an installed package in a synthetic workspace."
        ),
    )
    run_p.add_argument("--ref", help="Branch/tag/SHA to checkout.")
    run_p.add_argument(
        "--agent",
        default="codex",
        help="Agent adapter to use (MVP: codex, claude, gemini).",
    )
    run_p.add_argument(
        "--policy", default="write", help="Execution policy (see configs/policies.yaml)."
    )
    run_p.add_argument(
        "--persona-id",
        help="Persona id to run (defaults from the catalog if omitted).",
    )
    run_p.add_argument(
        "--mission-id",
        help="Mission id to run (defaults from the catalog if omitted).",
    )
    run_p.add_argument(
        "--obfuscate-agent-docs",
        action="store_true",
        help="Hide target-repo agent instruction files (e.g., agents.md) from the agent workspace.",
    )
    run_p.add_argument("--seed", type=int, default=0, help="Seed label (for comparability).")
    run_p.add_argument("--model", help="Override agent model (if supported).")
    run_p.add_argument(
        "--agent-rate-limit-retries",
        type=int,
        default=2,
        help=(
            "Retry count for provider capacity/rate-limit failures "
            "(classification: provider_capacity)."
        ),
    )
    run_p.add_argument(
        "--agent-rate-limit-backoff-seconds",
        type=float,
        default=1.0,
        help="Base delay in seconds for rate-limit retries (exponential backoff).",
    )
    run_p.add_argument(
        "--agent-rate-limit-backoff-multiplier",
        type=float,
        default=2.0,
        help="Multiplier for successive rate-limit retry delays.",
    )
    run_p.add_argument(
        "--agent-followup-attempts",
        type=int,
        default=2,
        help=(
            "Max additional follow-up prompts when agent output parses/validates incorrectly "
            "after a successful run."
        ),
    )
    run_p.add_argument(
        "--agent-config",
        action="append",
        default=[],
        help="Repeatable agent config override (Codex: -c key=value).",
    )
    run_p.add_argument(
        "--agent-system-prompt-file",
        type=Path,
        help=(
            "Path to a file used to override the agent's built-in system prompt/instructions "
            "(mapped per agent: Codex model_instructions_file, Claude --system-prompt-file, "
            "Gemini --agent-system-prompt-file)."
        ),
    )
    run_p.add_argument(
        "--agent-append-system-prompt",
        help=(
            "Text to append to the agent system prompt where supported "
            "(mapped per agent: Codex developer_instructions, Claude --append-system-prompt)."
        ),
    )
    run_p.add_argument(
        "--agent-append-system-prompt-file",
        type=Path,
        help=(
            "Path to a file whose contents are appended to the agent system prompt where supported "
            "(mapped per agent: Codex developer_instructions, Claude --append-system-prompt-file)."
        ),
    )
    run_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )
    run_p.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Keep cloned workspace (may be relocated).",
    )
    run_p.add_argument(
        "--preflight-command",
        action="append",
        dest="preflight_commands",
        default=[],
        help=(
            "Repeatable command name to probe during preflight (e.g., --preflight-command ffmpeg)."
        ),
    )
    run_p.add_argument(
        "--require-preflight-command",
        action="append",
        dest="preflight_required_commands",
        default=[],
        help=(
            "Repeatable command name that must be available and permitted by policy during "
            "preflight (fails fast with structured diagnostics if missing/blocked)."
        ),
    )
    run_p.add_argument(
        "--verify-command",
        action="append",
        dest="verification_commands",
        default=[],
        help=(
            "Repeatable shell command to run as a required verification gate before handing off "
            "(e.g., --verify-command \"python -m pytest -q\"). Fails the run (and may trigger "
            "agent follow-ups) if any command exits non-zero."
        ),
    )
    run_p.add_argument(
        "--verify-timeout-seconds",
        type=float,
        default=None,
        help=(
            "Optional per-command timeout for --verify-command checks. "
            "Non-positive values disable the timeout."
        ),
    )
    run_p.add_argument(
        "--exec-backend",
        choices=["local", "docker"],
        default="local",
        help="Execution backend.",
    )
    run_p.add_argument(
        "--exec-docker-context",
        type=Path,
        help=(
            "Docker image build context directory. "
            "If omitted with --exec-backend docker, defaults to "
            "the built-in sandbox_cli context shipped with sandbox_runner."
        ),
    )
    run_p.add_argument(
        "--exec-dockerfile",
        type=Path,
        help="Optional Dockerfile path (resolved relative to the context dir if relative).",
    )
    run_p.add_argument(
        "--exec-docker-python",
        default="auto",
        help=(
            "Docker sandbox Python selection for sandbox_cli contexts. "
            "auto: derive from target pyproject.toml (project.requires-python) "
            "and only override if needed; "
            "context: use the Dockerfile as-is; "
            "otherwise: a Python version/tag or full base image "
            "(e.g., 3.12, 3.12.8, 3.12-slim-bookworm, python:3.12-slim)."
        ),
    )
    run_p.add_argument(
        "--exec-docker-timeout-seconds",
        type=float,
        help=(
            "Optional timeout (seconds) for Docker CLI operations issued by sandbox_runner. "
            "No default; <=0 disables."
        ),
    )
    run_p.add_argument(
        "--exec-use-target-sandbox-cli-install",
        action="store_true",
        help=(
            "When using --exec-backend docker with a sandbox_cli-shaped context, "
            "merge the target repo's .usertest/sandbox_cli_install.yaml into the per-run "
            "overlay manifests."
        ),
    )
    run_p.add_argument(
        "--exec-network",
        choices=["open", "none"],
        default="open",
        help=(
            "Docker container network mode. Note: the agent CLI runs inside the container in this repo; "
            "`none` will prevent hosted agent CLIs (codex/claude/gemini) from reaching their APIs."
        ),
    )
    run_p.add_argument(
        "--exec-cache",
        choices=["cold", "warm"],
        default="cold",
        help="Cache mode for docker runs.",
    )
    run_p.add_argument(
        "--exec-cache-dir",
        type=Path,
        help="Host cache directory (defaults to runs/_cache/usertest when --exec-cache warm).",
    )
    run_p.add_argument(
        "--exec-env",
        action="append",
        default=[],
        help="Repeatable env var name allowlist to pass into the container (e.g., OPENAI_API_KEY).",
    )
    run_auth_group = run_p.add_mutually_exclusive_group()
    run_auth_group.add_argument(
        "--exec-use-host-agent-login",
        dest="exec_use_host_agent_login",
        action="store_true",
        default=True,
        help=(
            "When using --exec-backend docker, mount the host's existing agent login state "
            "(e.g., ~/.codex, ~/.claude, ~/.gemini) into the container so API keys don't need to "
            "be passed via --exec-env (default)."
        ),
    )
    run_auth_group.add_argument(
        "--exec-use-api-key-auth",
        dest="exec_use_host_agent_login",
        action="store_false",
        help=(
            "Opt into API-key auth for Docker runs instead of host login mounts. "
            "For Codex, provide --exec-env OPENAI_API_KEY and set OPENAI_API_KEY on the host."
        ),
    )
    run_p.add_argument(
        "--exec-keep-container",
        action="store_true",
        help="Keep the Docker container after the run (debugging).",
    )
    run_p.add_argument(
        "--exec-rebuild-image",
        action="store_true",
        help="Force rebuilding the Docker image even if it exists.",
    )

    batch_p = sub.add_parser("batch", help="Run multiple targets from a YAML file.")
    batch_p.add_argument("--targets", required=True, type=Path, help="YAML file with targets list.")
    batch_p.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate targets.yaml and exit (do not create run dirs or execute targets).",
    )
    batch_p.add_argument("--agent", default="codex")
    batch_p.add_argument("--policy", default="write")
    batch_p.add_argument("--seed", type=int, default=0)
    batch_p.add_argument(
        "--model",
        help="Default model override for all targets (overridable per target).",
    )
    batch_p.add_argument(
        "--agent-config",
        action="append",
        default=[],
        help="Repeatable agent config override (Codex: -c key=value) applied to all targets (overridable per target).",
    )
    batch_p.add_argument("--agent-rate-limit-retries", type=int, default=2)
    batch_p.add_argument("--agent-rate-limit-backoff-seconds", type=float, default=1.0)
    batch_p.add_argument("--agent-rate-limit-backoff-multiplier", type=float, default=2.0)
    batch_p.add_argument("--agent-followup-attempts", type=int, default=2)
    batch_p.add_argument(
        "--persona-id",
        help="Default persona id for all targets (overridable per target).",
    )
    batch_p.add_argument(
        "--mission-id",
        help="Default mission id for all targets (overridable per target).",
    )
    batch_p.add_argument(
        "--obfuscate-agent-docs",
        action="store_true",
        help="Hide target-repo agent instruction files (e.g., agents.md) from the agent workspace.",
    )
    batch_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )
    batch_p.add_argument("--keep-workspace", action="store_true")
    batch_p.add_argument(
        "--preflight-command",
        action="append",
        dest="preflight_commands",
        default=[],
        help=(
            "Repeatable command name to probe during preflight (e.g., --preflight-command ffmpeg)."
        ),
    )
    batch_p.add_argument(
        "--require-preflight-command",
        action="append",
        dest="preflight_required_commands",
        default=[],
        help=(
            "Repeatable command name that must be available and permitted by policy during "
            "preflight (fails fast with structured diagnostics if missing/blocked)."
        ),
    )
    batch_p.add_argument(
        "--verify-command",
        action="append",
        dest="verification_commands",
        default=[],
        help="Repeatable verification command applied to all targets (overridable per target).",
    )
    batch_p.add_argument(
        "--verify-timeout-seconds",
        type=float,
        default=None,
        help="Optional per-command timeout for --verify-command checks (applied to all targets).",
    )
    batch_p.add_argument(
        "--agent-system-prompt-file",
        type=Path,
        help="Default agent system prompt override file for all targets (see `run --help`).",
    )
    batch_p.add_argument(
        "--agent-append-system-prompt",
        help="Default agent system prompt append text for all targets (see `run --help`).",
    )
    batch_p.add_argument(
        "--agent-append-system-prompt-file",
        type=Path,
        help="Default agent system prompt append file for all targets (see `run --help`).",
    )
    batch_p.add_argument("--exec-backend", choices=["local", "docker"], default="local")
    batch_p.add_argument("--exec-docker-context", type=Path)
    batch_p.add_argument("--exec-dockerfile", type=Path)
    batch_p.add_argument("--exec-docker-python", default="auto")
    batch_p.add_argument("--exec-docker-timeout-seconds", type=float)
    batch_p.add_argument(
        "--exec-use-target-sandbox-cli-install",
        action="store_true",
        help=(
            "When using --exec-backend docker with a sandbox_cli-shaped context, "
            "merge each target repo's .usertest/sandbox_cli_install.yaml into the per-run "
            "overlay manifests."
        ),
    )
    batch_p.add_argument(
        "--exec-network",
        choices=["open", "none"],
        default="open",
        help=(
            "Docker container network mode. Note: the agent CLI runs inside the container in this repo; "
            "`none` will prevent hosted agent CLIs (codex/claude/gemini) from reaching their APIs."
        ),
    )
    batch_p.add_argument("--exec-cache", choices=["cold", "warm"], default="cold")
    batch_p.add_argument("--exec-cache-dir", type=Path)
    batch_p.add_argument("--exec-env", action="append", default=[])
    batch_auth_group = batch_p.add_mutually_exclusive_group()
    batch_auth_group.add_argument(
        "--exec-use-host-agent-login",
        dest="exec_use_host_agent_login",
        action="store_true",
        default=True,
        help=(
            "When using --exec-backend docker, mount the host's existing agent login state "
            "(e.g., ~/.codex, ~/.claude, ~/.gemini) into the container so API keys don't need to "
            "be passed via --exec-env (default)."
        ),
    )
    batch_auth_group.add_argument(
        "--exec-use-api-key-auth",
        dest="exec_use_host_agent_login",
        action="store_false",
        help=(
            "Opt into API-key auth for Docker batch runs instead of host login mounts. "
            "For Codex, provide --exec-env OPENAI_API_KEY and set OPENAI_API_KEY on the host."
        ),
    )
    batch_p.add_argument("--exec-keep-container", action="store_true")
    batch_p.add_argument("--exec-rebuild-image", action="store_true")
    batch_p.add_argument(
        "--command-probe-timeout-seconds",
        type=float,
        default=5.0,
        help=(
            "Timeout per initial command responsiveness probe (e.g., `npm --version`) before "
            "starting the batch."
        ),
    )
    batch_p.add_argument(
        "--skip-command-probes",
        action="store_true",
        help="Skip initial command responsiveness probes.",
    )

    matrix_p = sub.add_parser(
        "matrix",
        help=(
            "Generate and (optionally) run a matrix of persona/mission x agent/model combinations."
        ),
    )
    matrix_sub = matrix_p.add_subparsers(dest="matrix_cmd", required=True)

    matrix_plan_p = matrix_sub.add_parser(
        "plan",
        help="Expand a matrix spec into batch targets and validate (no execution).",
    )
    matrix_run_p = matrix_sub.add_parser(
        "run",
        help="Validate a matrix spec then execute all combinations.",
    )

    for p in (matrix_plan_p, matrix_run_p):
        p.add_argument(
            "--repo-root",
            type=Path,
            default=Path("."),
            help="Monorepo root (auto-detected when omitted).",
        )
        p.add_argument(
            "--spec",
            type=Path,
            required=True,
            help="Path to a YAML matrix spec.",
        )
        p.add_argument(
            "--out-targets",
            type=Path,
            help=(
                "Write expanded batch targets YAML here (default: runs/usertest/<target>/_compiled/<ts>.matrix.targets.yaml)."
            ),
        )
        p.add_argument(
            "--out-report",
            type=Path,
            help=("Write a JSON validation report (capabilities + requirements per combination)."),
        )
        p.add_argument(
            "--exec-backend",
            choices=["local", "docker"],
            default="local",
            help="Execution backend (affects tool availability, especially for gemini shell access).",
        )
        p.add_argument("--exec-docker-context", type=Path)
        p.add_argument("--exec-dockerfile", type=Path)
        p.add_argument("--exec-docker-python", default="auto")
        p.add_argument("--exec-docker-timeout-seconds", type=float, default=None)
        p.add_argument("--exec-use-target-sandbox-cli-install", action="store_true")
        p.add_argument("--exec-use-host-agent-login", action="store_true")
        p.add_argument(
            "--exec-network",
            choices=["open", "none"],
            default="open",
            help=(
                "Docker container network mode. Note: the agent CLI runs inside the container in this repo; "
                "`none` will prevent hosted agent CLIs (codex/claude/gemini) from reaching their APIs."
            ),
        )
        p.add_argument("--exec-cache", choices=["cold", "warm"], default="cold")
        p.add_argument("--exec-cache-dir", type=Path)
        p.add_argument(
            "--exec-env",
            action="append",
            default=[],
            help="Extra environment variable assignment(s) for sandbox execution (repeatable KEY=VALUE).",
        )
        p.add_argument("--exec-keep-container", action="store_true")
        p.add_argument("--exec-rebuild-image", action="store_true")

        p.add_argument(
            "--skip-command-probes",
            action="store_true",
            help="Skip local command responsiveness probes (faster, less validation).",
        )
        p.add_argument(
            "--command-probe-timeout-seconds",
            type=float,
            default=0.25,
            help="Timeout for each command responsiveness probe.",
        )

    lint_p = sub.add_parser(
        "lint",
        help="Lint missions/policies/catalog configuration (capability contract).",
    )
    lint_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )
    lint_p.add_argument(
        "--repo",
        help="Optional target repo path/git URL to lint catalog overrides (same forms as `run --repo`).",
    )
    lint_p.add_argument("--ref", help="Branch/tag/SHA to checkout when --repo is a git URL.")
    lint_p.add_argument(
        "--strict",
        action="store_true",
        help="Fail (non-zero exit) if any warnings are emitted.",
    )
    lint_p.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Console output format.",
    )
    lint_p.add_argument(
        "--out-json",
        type=Path,
        help="Write full lint report JSON to this path (optional).",
    )
    report_p = sub.add_parser("report", help="(Re)render report.md for an existing run dir.")
    report_p.add_argument("--run-dir", required=True, type=Path, help="Run directory to render.")
    report_p.add_argument(
        "--recompute-metrics",
        action="store_true",
        help="Regenerate normalized_events.jsonl and metrics.json from raw_events.jsonl.",
    )
    report_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    init_p = sub.add_parser(
        "init-usertest",
        help="Initialize target .usertest/ scaffold (catalog.yaml).",
    )
    init_p.add_argument("--repo", required=True, type=Path, help="Path to local target repo.")
    init_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite .usertest scaffold files if they already exist.",
    )
    init_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    personas_p = sub.add_parser("personas", help="Persona catalog commands.")
    personas_sub = personas_p.add_subparsers(dest="personas_cmd", required=True)
    personas_list_p = personas_sub.add_parser("list", help="List discovered personas.")
    personas_list_p.add_argument(
        "--repo",
        help="Optional target repo path/URL (loads .usertest/catalog.yaml if present).",
    )
    personas_list_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    missions_p = sub.add_parser("missions", help="Mission catalog commands.")
    missions_sub = missions_p.add_subparsers(dest="missions_cmd", required=True)
    missions_list_p = missions_sub.add_parser("list", help="List discovered missions.")
    missions_list_p.add_argument(
        "--repo",
        help="Optional target repo path/URL (loads .usertest/catalog.yaml if present).",
    )
    missions_list_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    reports_p = sub.add_parser("reports", help="Report history commands.")
    reports_sub = reports_p.add_subparsers(dest="reports_cmd", required=True)
    reports_compile_p = reports_sub.add_parser(
        "compile",
        help="Compile report.json + metadata across runs into a JSONL history file.",
    )
    reports_compile_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (e.g. tiktok_vids).",
    )
    reports_compile_p.add_argument(
        "--repo-input",
        help="Optional match for target_ref.repo_input (path or git URL).",
    )
    reports_compile_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_compile_p.add_argument(
        "--out",
        type=Path,
        help=(
            "Output JSONL path (defaults under runs/usertest/<target>/_compiled/ "
            "or runs/usertest/_compiled/ when --target is omitted)."
        ),
    )
    reports_compile_p.add_argument(
        "--embed",
        choices=["none", "definitions", "prompt", "all"],
        default="definitions",
        help=(
            "How much extra run context to embed (beyond JSON artifacts). "
            "none: only JSON; definitions: persona/mission/schema/template; "
            "prompt: + prompt.txt; all: + users.md."
        ),
    )
    reports_compile_p.add_argument(
        "--max-embed-bytes",
        type=int,
        default=200_000,
        help="Skip embedding any single text file larger than this many bytes.",
    )
    reports_compile_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    reports_analyze_p = reports_sub.add_parser(
        "analyze",
        help="Analyze run outcomes and cluster recurring issues from batch/historical runs.",
    )
    reports_analyze_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (e.g. tiktok_vids).",
    )
    reports_analyze_p.add_argument(
        "--repo-input",
        help="Optional match for target_ref.repo_input (path or git URL).",
    )
    reports_analyze_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_analyze_p.add_argument(
        "--history",
        type=Path,
        help="Path to a compiled report history JSONL (from `reports compile`).",
    )
    reports_analyze_p.add_argument(
        "--out-json",
        type=Path,
        help=(
            "Output analysis JSON path (defaults under runs/usertest/<target>/_compiled/ "
            "or runs/usertest/_compiled/ when --target is omitted)."
        ),
    )
    reports_analyze_p.add_argument(
        "--out-md",
        type=Path,
        help=("Output markdown summary path (defaults next to --out-json with .md extension)."),
    )
    reports_analyze_p.add_argument(
        "--actions",
        type=Path,
        help=(
            "Optional JSON action registry for addressed comments (date/plan metadata). "
            "Defaults to configs/issue_actions.json when present."
        ),
    )
    reports_analyze_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    return parser


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


def _cmd_run(args: argparse.Namespace) -> int:
    """Execute the run subcommand."""
    repo_root = _resolve_repo_root(args.repo_root)
    _warn_legacy_runs_layout(repo_root)
    cfg = _load_runner_config(repo_root)

    exec_docker_context = _resolve_optional_path(repo_root, args.exec_docker_context)
    exec_cache_dir = _resolve_optional_path(repo_root, args.exec_cache_dir)
    exec_docker_timeout_seconds = args.exec_docker_timeout_seconds
    if exec_docker_timeout_seconds is not None and exec_docker_timeout_seconds <= 0:
        exec_docker_timeout_seconds = None

    if args.exec_backend == "docker" and exec_docker_context is None:
        exec_docker_context = (
            repo_root
            / "packages"
            / "sandbox_runner"
            / "builtins"
            / "docker"
            / "contexts"
            / "sandbox_cli"
        ).resolve()
        print(
            f"No --exec-docker-context provided; using built-in context: {exec_docker_context}",
            file=sys.stderr,
        )
    if args.exec_backend == "docker" and (
        exec_docker_context is None
        or not exec_docker_context.exists()
        or not exec_docker_context.is_dir()
    ):
        raise FileNotFoundError(f"Missing --exec-docker-context directory: {exec_docker_context}")

    if args.exec_cache == "warm" and exec_cache_dir is None:
        exec_cache_dir = (repo_root / "runs" / "_cache" / "usertest").resolve()
        print(
            f"No --exec-cache-dir provided; using default: {exec_cache_dir}",
            file=sys.stderr,
        )

    preflight_commands: list[str] = []
    for cmd in args.preflight_commands or []:
        if not isinstance(cmd, str) or not cmd.strip():
            raise ValueError(f"--preflight-command entries must be non-empty strings; got {cmd!r}.")
        preflight_commands.append(cmd.strip())

    preflight_required_commands: list[str] = []
    for cmd in args.preflight_required_commands or []:
        if not isinstance(cmd, str) or not cmd.strip():
            raise ValueError(
                f"--require-preflight-command entries must be non-empty strings; got {cmd!r}."
            )
        preflight_required_commands.append(cmd.strip())

    verification_commands: list[str] = []
    for cmd in getattr(args, "verification_commands", None) or []:
        if not isinstance(cmd, str) or not cmd.strip():
            raise ValueError(f"--verify-command entries must be non-empty strings; got {cmd!r}.")
        verification_commands.append(cmd.strip())

    verification_timeout_seconds = getattr(args, "verification_timeout_seconds", None)
    if verification_timeout_seconds is not None and verification_timeout_seconds <= 0:
        verification_timeout_seconds = None

    result = run_once(
        cfg,
        RunRequest(
            repo=args.repo,
            ref=args.ref,
            agent=args.agent,
            policy=args.policy,
            persona_id=args.persona_id,
            mission_id=args.mission_id,
            obfuscate_agent_docs=bool(args.obfuscate_agent_docs),
            seed=args.seed,
            model=args.model,
            agent_config_overrides=tuple(args.agent_config),
            agent_system_prompt_file=args.agent_system_prompt_file,
            agent_append_system_prompt=args.agent_append_system_prompt,
            agent_append_system_prompt_file=args.agent_append_system_prompt_file,
            keep_workspace=bool(args.keep_workspace),
            preflight_commands=tuple(preflight_commands),
            preflight_required_commands=tuple(preflight_required_commands),
            verification_commands=tuple(verification_commands),
            verification_timeout_seconds=verification_timeout_seconds,
            exec_backend=str(args.exec_backend),
            exec_docker_context=exec_docker_context,
            exec_dockerfile=args.exec_dockerfile,
            exec_docker_python=str(args.exec_docker_python),
            exec_docker_timeout_seconds=exec_docker_timeout_seconds,
            exec_use_target_sandbox_cli_install=bool(args.exec_use_target_sandbox_cli_install),
            exec_use_host_agent_login=bool(args.exec_use_host_agent_login),
            exec_network=str(args.exec_network),
            exec_cache=str(args.exec_cache),
            exec_cache_dir=exec_cache_dir,
            exec_env=tuple(str(x) for x in (args.exec_env or []) if str(x).strip()),
            exec_keep_container=bool(args.exec_keep_container),
            exec_rebuild_image=bool(args.exec_rebuild_image),
            agent_rate_limit_retries=int(args.agent_rate_limit_retries),
            agent_rate_limit_backoff_seconds=float(args.agent_rate_limit_backoff_seconds),
            agent_rate_limit_backoff_multiplier=float(args.agent_rate_limit_backoff_multiplier),
            agent_followup_attempts=int(args.agent_followup_attempts),
        ),
    )

    print(str(result.run_dir))
    if result.exit_code != 0:
        print("Run failed:")
        if result.report_validation_errors:
            for e in result.report_validation_errors:
                print(f"- {e}")
        else:
            print(f"- exit_code={result.exit_code} (see agent_stderr.txt and error.json)")
    elif result.report_validation_errors:
        print("Report validation errors:")
        for e in result.report_validation_errors:
            print(f"- {e}")
    return 0 if result.exit_code == 0 and not result.report_validation_errors else 2


def _cmd_batch(args: argparse.Namespace) -> int:
    """Execute the batch subcommand."""
    repo_root = _resolve_repo_root(args.repo_root)
    _warn_legacy_runs_layout(repo_root)
    cfg = _load_runner_config(repo_root)

    exec_docker_context = _resolve_optional_path(repo_root, args.exec_docker_context)
    exec_cache_dir = _resolve_optional_path(repo_root, args.exec_cache_dir)
    exec_docker_timeout_seconds = args.exec_docker_timeout_seconds
    if exec_docker_timeout_seconds is not None and exec_docker_timeout_seconds <= 0:
        exec_docker_timeout_seconds = None
    if args.exec_backend == "docker" and exec_docker_context is None:
        exec_docker_context = (
            repo_root
            / "packages"
            / "sandbox_runner"
            / "builtins"
            / "docker"
            / "contexts"
            / "sandbox_cli"
        ).resolve()
        print(
            f"No --exec-docker-context provided; using built-in context: {exec_docker_context}",
            file=sys.stderr,
        )
    if args.exec_backend == "docker" and (
        exec_docker_context is None
        or not exec_docker_context.exists()
        or not exec_docker_context.is_dir()
    ):
        raise FileNotFoundError(f"Missing --exec-docker-context directory: {exec_docker_context}")
    if args.exec_cache == "warm" and exec_cache_dir is None:
        exec_cache_dir = (repo_root / "runs" / "_cache" / "usertest").resolve()
        print(
            f"No --exec-cache-dir provided; using default: {exec_cache_dir}",
            file=sys.stderr,
        )

    targets_path: Path = args.targets
    if not targets_path.is_absolute() and not targets_path.exists():
        targets_path = repo_root / targets_path
    try:
        data = _load_yaml(targets_path)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None) or getattr(e, "context_mark", None)
        location = str(targets_path)
        if mark is not None and hasattr(mark, "line") and hasattr(mark, "column"):
            try:
                line = int(mark.line) + 1
                col = int(mark.column) + 1
            except Exception:  # noqa: BLE001
                line = None
                col = None
            if line is not None and col is not None:
                location = f"{targets_path}:{line}:{col}"
        summary = str(e).splitlines()[0].strip() or type(e).__name__
        print("Batch validation failed; no targets were executed.", file=sys.stderr)
        print(f"- YAML parse error in {location}: {summary}", file=sys.stderr)
        return 2
    except ValueError as e:
        print("Batch validation failed; no targets were executed.", file=sys.stderr)
        print(f"- Invalid targets YAML {targets_path}: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print("Batch validation failed; no targets were executed.", file=sys.stderr)
        print(f"- Failed to read targets YAML {targets_path}: {e}", file=sys.stderr)
        return 2
    parse_errors: list[str] = []

    def _append_arg_list_errors(values: Any, *, flag: str) -> list[str]:
        if values is None:
            return []
        if not isinstance(values, list):
            parse_errors.append(f"args: {flag} must be repeatable strings; got {type(values).__name__}.")
            return []
        normalized: list[str] = []
        for vidx, value in enumerate(values):
            if not isinstance(value, str) or not value.strip():
                parse_errors.append(
                    f"args: {flag}[{vidx}] must be a non-empty string; got {value!r}."
                )
                continue
            normalized.append(value.strip())
        return normalized

    base_preflight_commands = _append_arg_list_errors(
        getattr(args, "preflight_commands", None),
        flag="--preflight-command",
    )
    base_preflight_required_commands = _append_arg_list_errors(
        getattr(args, "preflight_required_commands", None),
        flag="--require-preflight-command",
    )
    base_verification_commands = _append_arg_list_errors(
        getattr(args, "verification_commands", None),
        flag="--verify-command",
    )
    base_verification_timeout_seconds = getattr(args, "verification_timeout_seconds", None)
    if base_verification_timeout_seconds is not None and base_verification_timeout_seconds <= 0:
        base_verification_timeout_seconds = None
    base_agent_config_overrides = _append_arg_list_errors(
        getattr(args, "agent_config", None),
        flag="--agent-config",
    )

    targets_raw = data.get("targets", [])
    if targets_raw is None:
        targets_raw = []
    if not isinstance(targets_raw, list):
        parse_errors.append(
            f"targets: expected a list (YAML sequence) in {targets_path}; got {type(targets_raw).__name__}."
        )
        targets: list[Any] = []
    else:
        targets = targets_raw
    requests: list[tuple[int, RunRequest]] = []
    for idx, item in enumerate(targets):
        target_errors: list[str] = []

        if not isinstance(item, dict):
            parse_errors.append(
                f"targets[{idx}]: must be a mapping (YAML object); got {type(item).__name__}."
            )
            continue

        def _require_non_empty_str(
            field: str,
            *,
            _item=item,
            _idx=idx,
            _target_errors=target_errors,
        ) -> str | None:
            raw = _item.get(field)
            if raw is None:
                _target_errors.append(f"targets[{_idx}].{field} is required.")
                return None
            if not isinstance(raw, str) or not raw.strip():
                _target_errors.append(
                    f"targets[{_idx}].{field} must be a non-empty string; got {raw!r}."
                )
                return None
            return raw

        repo_value = _require_non_empty_str("repo")
        if repo_value is None:
            parse_errors.extend(target_errors)
            continue

        legacy_keys = {
            "persona",
            "mission",
            "persona_file",
            "mission_file",
            "use_builtin_context",
        } & set(item)
        if legacy_keys:
            legacy_list = ", ".join(sorted(legacy_keys))
            parse_errors.append(
                f"targets[{idx}]: uses legacy keys ({legacy_list}). "
                "Update to persona_id / mission_id and remove legacy fields."
            )

        def _optional_str(
            field: str,
            default: str | None,
            *,
            _item=item,
            _idx=idx,
            _target_errors=target_errors,
        ) -> str | None:
            if field not in _item:
                return default
            raw = _item.get(field)
            if raw is None:
                return None
            if not isinstance(raw, str):
                _target_errors.append(
                    f"targets[{_idx}].{field} must be a string if present; got {type(raw).__name__}."
                )
                return None
            return raw

        def _optional_int(
            field: str,
            default: int,
            *,
            _item=item,
            _idx=idx,
            _target_errors=target_errors,
        ) -> int | None:
            raw = _item.get(field, default)
            if raw is None:
                return default
            if isinstance(raw, bool):
                _target_errors.append(
                    f"targets[{_idx}].{field} must be an integer; got bool."
                )
                return None
            if isinstance(raw, int):
                return raw
            if isinstance(raw, str):
                try:
                    return int(raw.strip())
                except ValueError:
                    _target_errors.append(
                        f"targets[{_idx}].{field} must be an integer; got {raw!r}."
                    )
                    return None
            _target_errors.append(
                f"targets[{_idx}].{field} must be an integer; got {type(raw).__name__}."
            )
            return None

        def _optional_float(
            field: str,
            default: float,
            *,
            _item=item,
            _idx=idx,
            _target_errors=target_errors,
        ) -> float | None:
            raw = _item.get(field, default)
            if raw is None:
                return default
            if isinstance(raw, bool):
                _target_errors.append(
                    f"targets[{_idx}].{field} must be a number; got bool."
                )
                return None
            if isinstance(raw, (int, float)):
                return float(raw)
            if isinstance(raw, str):
                try:
                    return float(raw.strip())
                except ValueError:
                    _target_errors.append(
                        f"targets[{_idx}].{field} must be a number; got {raw!r}."
                    )
                    return None
            _target_errors.append(
                f"targets[{_idx}].{field} must be a number; got {type(raw).__name__}."
            )
            return None

        def _optional_nullable_float(
            field: str,
            default: float | None,
            *,
            _item=item,
            _idx=idx,
            _target_errors=target_errors,
        ) -> float | None:
            raw = _item.get(field, default)
            if raw is None:
                return default
            if isinstance(raw, bool):
                _target_errors.append(
                    f"targets[{_idx}].{field} must be a number; got bool."
                )
                return None
            if isinstance(raw, (int, float)):
                return float(raw)
            if isinstance(raw, str):
                try:
                    return float(raw.strip())
                except ValueError:
                    _target_errors.append(
                        f"targets[{_idx}].{field} must be a number; got {raw!r}."
                    )
                    return None
            _target_errors.append(
                f"targets[{_idx}].{field} must be a number; got {type(raw).__name__}."
            )
            return None

        preflight_commands: list[str] = list(base_preflight_commands)
        preflight_required_commands: list[str] = list(base_preflight_required_commands)
        verification_commands: list[str] = list(base_verification_commands)
        verification_timeout_seconds = base_verification_timeout_seconds
        agent_config_overrides: list[str] = list(base_agent_config_overrides)

        raw_agent_config = item.get("agent_config")
        if raw_agent_config is None:
            raw_agent_config = item.get("agent_config_overrides")
        if raw_agent_config is not None:
            if not isinstance(raw_agent_config, list):
                target_errors.append(
                    f"targets[{idx}].agent_config must be a list of strings if present."
                )
            for jdx, override in enumerate(raw_agent_config):
                if not isinstance(override, str) or not override.strip():
                    target_errors.append(
                        f"targets[{idx}].agent_config[{jdx}] must be a non-empty string; got {override!r}."
                    )
                else:
                    agent_config_overrides.append(override.strip())
        raw_preflight_commands = item.get("preflight_commands")
        if raw_preflight_commands is not None:
            if not isinstance(raw_preflight_commands, list):
                target_errors.append(
                    f"targets[{idx}].preflight_commands must be a list of strings if present."
                )
            for jdx, cmd in enumerate(raw_preflight_commands):
                if not isinstance(cmd, str) or not cmd.strip():
                    target_errors.append(
                        f"targets[{idx}].preflight_commands[{jdx}] must be a non-empty string; "
                        f"got {cmd!r}."
                    )
                else:
                    preflight_commands.append(cmd.strip())

        raw_preflight_required = item.get("preflight_required_commands")
        if raw_preflight_required is not None:
            if not isinstance(raw_preflight_required, list):
                target_errors.append(
                    f"targets[{idx}].preflight_required_commands must be a list of strings "
                    f"if present."
                )
            for jdx, cmd in enumerate(raw_preflight_required):
                if not isinstance(cmd, str) or not cmd.strip():
                    target_errors.append(
                        f"targets[{idx}].preflight_required_commands[{jdx}] "
                        f"must be a non-empty string; got {cmd!r}."
                    )
                else:
                    preflight_required_commands.append(cmd.strip())

        raw_verification_commands = item.get("verification_commands")
        if raw_verification_commands is not None:
            if not isinstance(raw_verification_commands, list):
                target_errors.append(
                    f"targets[{idx}].verification_commands must be a list of strings if present."
                )
            for jdx, cmd in enumerate(raw_verification_commands):
                if not isinstance(cmd, str) or not cmd.strip():
                    target_errors.append(
                        f"targets[{idx}].verification_commands[{jdx}] must be a non-empty string; "
                        f"got {cmd!r}."
                    )
                else:
                    verification_commands.append(cmd.strip())

        verification_timeout_seconds = _optional_nullable_float(
            "verification_timeout_seconds", verification_timeout_seconds
        )
        if verification_timeout_seconds is not None and verification_timeout_seconds <= 0:
            verification_timeout_seconds = None

        ref_value = _optional_str("ref", None)
        agent_value = _optional_str("agent", str(args.agent))
        policy_value = _optional_str("policy", str(args.policy))
        persona_id_value = _optional_str("persona_id", args.persona_id)
        mission_id_value = _optional_str("mission_id", args.mission_id)
        model_value = _optional_str(
            "model",
            str(args.model) if getattr(args, "model", None) else None,
        )

        seed_value = _optional_int("seed", int(args.seed))
        retries_value = _optional_int(
            "agent_rate_limit_retries",
            int(args.agent_rate_limit_retries),
        )
        backoff_seconds_value = _optional_float(
            "agent_rate_limit_backoff_seconds",
            float(args.agent_rate_limit_backoff_seconds),
        )
        backoff_multiplier_value = _optional_float(
            "agent_rate_limit_backoff_multiplier",
            float(args.agent_rate_limit_backoff_multiplier),
        )
        followup_attempts_value = _optional_int(
            "agent_followup_attempts",
            int(args.agent_followup_attempts),
        )

        if target_errors:
            parse_errors.extend(target_errors)
            continue

        req = RunRequest(
            repo=repo_value,
            ref=ref_value,
            agent=agent_value if agent_value is not None else str(args.agent),
            policy=policy_value if policy_value is not None else str(args.policy),
            persona_id=persona_id_value,
            mission_id=mission_id_value,
            obfuscate_agent_docs=bool(args.obfuscate_agent_docs),
            seed=seed_value if seed_value is not None else int(args.seed),
            model=model_value,
            agent_config_overrides=tuple(agent_config_overrides),
            agent_system_prompt_file=args.agent_system_prompt_file,
            agent_append_system_prompt=args.agent_append_system_prompt,
            agent_append_system_prompt_file=args.agent_append_system_prompt_file,
            keep_workspace=bool(args.keep_workspace),
            preflight_commands=tuple(preflight_commands),
            preflight_required_commands=tuple(preflight_required_commands),
            verification_commands=tuple(verification_commands),
            verification_timeout_seconds=verification_timeout_seconds,
            exec_backend=str(args.exec_backend),
            exec_docker_context=exec_docker_context,
            exec_dockerfile=args.exec_dockerfile,
            exec_docker_python=str(args.exec_docker_python),
            exec_docker_timeout_seconds=exec_docker_timeout_seconds,
            exec_use_target_sandbox_cli_install=bool(args.exec_use_target_sandbox_cli_install),
            exec_use_host_agent_login=bool(args.exec_use_host_agent_login),
            exec_network=str(args.exec_network),
            exec_cache=str(args.exec_cache),
            exec_cache_dir=exec_cache_dir,
            exec_env=tuple(str(x) for x in (args.exec_env or []) if str(x).strip()),
            exec_keep_container=bool(args.exec_keep_container),
            exec_rebuild_image=bool(args.exec_rebuild_image),
            agent_rate_limit_retries=(
                retries_value
                if retries_value is not None
                else int(args.agent_rate_limit_retries)
            ),
            agent_rate_limit_backoff_seconds=(
                backoff_seconds_value
                if backoff_seconds_value is not None
                else float(args.agent_rate_limit_backoff_seconds)
            ),
            agent_rate_limit_backoff_multiplier=(
                backoff_multiplier_value
                if backoff_multiplier_value is not None
                else float(args.agent_rate_limit_backoff_multiplier)
            ),
            agent_followup_attempts=(
                followup_attempts_value
                if followup_attempts_value is not None
                else int(args.agent_followup_attempts)
            ),
        )

        requests.append((idx, req))

    validation_errors = _prevalidate_batch_requests(
        cfg=cfg,
        repo_root=repo_root,
        targets_path=targets_path,
        requests=requests,
        probe_timeout_seconds=float(args.command_probe_timeout_seconds),
        skip_command_responsiveness_probes=bool(args.skip_command_probes),
        validate_only=bool(getattr(args, "validate_only", False)),
    )
    all_errors = [*parse_errors, *validation_errors]
    if all_errors:
        print("Batch validation failed; no targets were executed.", file=sys.stderr)
        for e in all_errors:
            print(f"- {e}", file=sys.stderr)
        print("- See docs/reference/targets-yaml.md for targets.yaml format.", file=sys.stderr)
        return 2
    if bool(getattr(args, "validate_only", False)):
        print("Batch validation passed; no targets were executed (validate-only).", file=sys.stderr)
        return 0

    exit_code = 0
    for _idx, req in requests:
        result = run_once(cfg, req)
        print(str(result.run_dir))
        if result.exit_code != 0 or result.report_validation_errors:
            exit_code = 2
    return exit_code


def _matrix__coerce_bool(value: Any) -> bool | None:
    """Coerce matrix spec values into optional booleans."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "y", "1", "on"}:
            return True
        if lowered in {"false", "no", "n", "0", "off"}:
            return False
    return None


def _matrix__parse_mission_entries(
    raw: Any,
    *,
    spec_path: Path,
) -> tuple[list[str | None], dict[str, dict[str, bool]]]:
    """
    Returns (mission_ids, overrides_by_mission_id).

    Missions can be specified as:
      - "mission_id" (string)
      - {id: "mission_id", requires_shell: true, requires_edits: false}

    If missions is missing/empty, returns [None] meaning "use catalog default".
    """

    if raw is None:
        return ([None], {})

    if isinstance(raw, str):
        raw = [raw]

    if not isinstance(raw, list):
        raise ValueError(
            f"matrix spec missions must be a list (or string); got {type(raw).__name__} in {spec_path}"
        )

    mission_ids: list[str | None] = []
    overrides: dict[str, dict[str, bool]] = {}

    for idx, item in enumerate(raw):
        if isinstance(item, str):
            mid = item.strip()
            if not mid:
                raise ValueError(f"matrix spec missions[{idx}] is empty in {spec_path}")
            mission_ids.append(mid)
            continue

        if not isinstance(item, dict):
            raise ValueError(
                f"matrix spec missions[{idx}] must be a string or mapping; got {type(item).__name__} in {spec_path}"
            )

        mid = _coerce_string(item.get("id")) or _coerce_string(item.get("mission_id"))
        if mid is None:
            raise ValueError(f"matrix spec missions[{idx}] missing id in {spec_path}")
        mission_ids.append(mid)

        rs = _matrix__coerce_bool(item.get("requires_shell"))
        re_ = _matrix__coerce_bool(item.get("requires_edits"))
        if rs is not None or re_ is not None:
            overrides[mid] = {
                **({"requires_shell": rs} if rs is not None else {}),
                **({"requires_edits": re_} if re_ is not None else {}),
            }

    if not mission_ids:
        mission_ids = [None]

    return (mission_ids, overrides)


def _matrix__parse_persona_ids(raw: Any, *, spec_path: Path) -> list[str | None]:
    """Parse and validate persona identifiers from matrix spec input."""
    if raw is None:
        return [None]

    if isinstance(raw, str):
        raw = [raw]

    if not isinstance(raw, list):
        raise ValueError(
            f"matrix spec personas must be a list (or string); got {type(raw).__name__} in {spec_path}"
        )

    persona_ids: list[str | None] = []
    for idx, item in enumerate(raw):
        if item is None:
            persona_ids.append(None)
            continue
        if not isinstance(item, str):
            raise ValueError(
                f"matrix spec personas[{idx}] must be a string (or null for default); got {type(item).__name__} in {spec_path}"
            )
        pid = item.strip()
        if not pid:
            raise ValueError(f"matrix spec personas[{idx}] is empty in {spec_path}")
        persona_ids.append(pid)

    return persona_ids or [None]


def _matrix__parse_seeds(raw: Any, *, spec_path: Path) -> list[int]:
    """Parse and validate seed values from matrix spec input."""
    if raw is None:
        return [0]

    if isinstance(raw, int):
        return [int(raw)]

    if isinstance(raw, str) and raw.strip().isdigit():
        return [int(raw.strip())]

    if not isinstance(raw, list):
        raise ValueError(
            f"matrix spec seeds must be a list (or int); got {type(raw).__name__} in {spec_path}"
        )

    seeds: list[int] = []
    for idx, item in enumerate(raw):
        if isinstance(item, bool):
            raise ValueError(f"matrix spec seeds[{idx}] must be int; got bool in {spec_path}")
        if isinstance(item, int):
            seeds.append(int(item))
            continue
        if isinstance(item, str) and item.strip().isdigit():
            seeds.append(int(item.strip()))
            continue
        raise ValueError(
            f"matrix spec seeds[{idx}] must be int; got {item!r} ({type(item).__name__}) in {spec_path}"
        )

    return seeds or [0]


def _matrix__parse_agent_entries(raw: Any, *, spec_path: Path) -> list[dict[str, Any]]:
    """Parse the providers/models axis.

    Agents can be specified as:
      - "codex"
      - {agent: "codex", models: ["GPT-5.3-Codex"], policy: "inspect", agent_config: ["k=v"]}

    Returns a list of normalized dicts with keys: agent, models, policy, agent_config.
    """

    if raw is None:
        return [{"agent": "codex", "models": [None], "policy": "auto", "agent_config": []}]

    if isinstance(raw, str):
        raw = [raw]

    if not isinstance(raw, list):
        raise ValueError(
            f"matrix spec agents must be a list (or string); got {type(raw).__name__} in {spec_path}"
        )

    entries: list[dict[str, Any]] = []

    for idx, item in enumerate(raw):
        if isinstance(item, str):
            agent = item.strip()
            if not agent:
                raise ValueError(f"matrix spec agents[{idx}] is empty in {spec_path}")
            entries.append({"agent": agent, "models": [None], "policy": "auto", "agent_config": []})
            continue

        if not isinstance(item, dict):
            raise ValueError(
                f"matrix spec agents[{idx}] must be a string or mapping; got {type(item).__name__} in {spec_path}"
            )

        agent = _coerce_string(item.get("agent")) or _coerce_string(item.get("id"))
        if agent is None:
            raise ValueError(f"matrix spec agents[{idx}] missing agent in {spec_path}")

        policy = (_coerce_string(item.get("policy")) or "auto").strip()
        if not policy:
            policy = "auto"

        models_raw = item.get("models")
        if models_raw is None:
            models_raw = item.get("model")
        models: list[str | None] = []
        if models_raw is None:
            models = [None]
        elif isinstance(models_raw, str):
            models = [models_raw.strip()]
        elif isinstance(models_raw, list):
            for jdx, m in enumerate(models_raw):
                if m is None:
                    models.append(None)
                    continue
                if not isinstance(m, str) or not m.strip():
                    raise ValueError(
                        f"matrix spec agents[{idx}].models[{jdx}] must be a non-empty string or null; got {m!r}"
                    )
                models.append(m.strip())
        else:
            raise ValueError(
                f"matrix spec agents[{idx}].models must be a list (or string); got {type(models_raw).__name__}"
            )
        if not models:
            models = [None]

        agent_config_raw = item.get("agent_config")
        if agent_config_raw is None:
            agent_config_raw = item.get("agent_config_overrides")
        agent_config: list[str] = []
        if agent_config_raw is None:
            agent_config = []
        elif isinstance(agent_config_raw, list):
            for jdx, ov in enumerate(agent_config_raw):
                if not isinstance(ov, str) or not ov.strip():
                    raise ValueError(
                        f"matrix spec agents[{idx}].agent_config[{jdx}] must be a non-empty string; got {ov!r}"
                    )
                agent_config.append(ov.strip())
        else:
            raise ValueError(
                f"matrix spec agents[{idx}].agent_config must be a list; got {type(agent_config_raw).__name__}"
            )

        entries.append(
            {"agent": agent, "models": models, "policy": policy, "agent_config": agent_config}
        )

    if not entries:
        entries = [{"agent": "codex", "models": [None], "policy": "auto", "agent_config": []}]

    return entries


def _matrix__infer_allow_edits_and_shell_status(
    *,
    cfg: RunnerConfig,
    request: RunRequest,
) -> tuple[bool, str]:
    """Infer (allow_edits, shell_status) for request's agent/policy/backend.

    shell_status is one of: allowed | blocked | unknown
    """

    policy_cfg = cfg.policies.get(request.policy, {})
    policy_cfg = policy_cfg if isinstance(policy_cfg, dict) else {}

    codex_policy = policy_cfg.get("codex", {})
    codex_policy = codex_policy if isinstance(codex_policy, dict) else {}
    claude_policy = policy_cfg.get("claude", {})
    claude_policy = claude_policy if isinstance(claude_policy, dict) else {}
    gemini_policy = policy_cfg.get("gemini", {})
    gemini_policy = gemini_policy if isinstance(gemini_policy, dict) else {}

    allow_edits = False
    if request.agent == "codex":
        allow_edits = bool(codex_policy.get("allow_edits", False))
    elif request.agent == "claude":
        allow_edits = bool(claude_policy.get("allow_edits", False))
    elif request.agent == "gemini":
        allow_edits = bool(gemini_policy.get("allow_edits", False))

    shell_status = "unknown"
    if request.agent == "claude":
        allowed_tools = claude_policy.get("allowed_tools")
        allowed_tools = allowed_tools if isinstance(allowed_tools, list) else []
        shell_status = "allowed" if "Bash" in allowed_tools else "blocked"
    elif request.agent == "gemini":
        allowed_tools = gemini_policy.get("allowed_tools")
        allowed_tools = allowed_tools if isinstance(allowed_tools, list) else []
        shell_enabled = "run_shell_command" in allowed_tools
        has_outer_sandbox = str(request.exec_backend) == "docker"
        gemini_sandbox_enabled = (
            bool(gemini_policy.get("sandbox", True))
            if isinstance(gemini_policy.get("sandbox", True), bool)
            else True
        )
        if has_outer_sandbox:
            gemini_sandbox_enabled = False
        if os.name == "nt":
            gemini_sandbox_enabled = False
        shell_available = has_outer_sandbox or gemini_sandbox_enabled
        if shell_enabled and not shell_available:
            shell_status = "blocked"
        else:
            shell_status = "allowed" if shell_enabled else "blocked"

    return (allow_edits, shell_status)


def _matrix__choose_policy_auto(
    *,
    cfg: RunnerConfig,
    agent: str,
    exec_backend: str,
    requires_shell: bool,
    requires_edits: bool,
) -> str:
    """Choose the least-permissive policy that satisfies requirements.

    Uses the conventional ordering: safe < inspect < write.
    """

    candidates = ["safe", "inspect", "write"]
    for policy in candidates:
        if policy not in cfg.policies:
            continue
        req = RunRequest(repo=".", agent=agent, policy=policy, exec_backend=exec_backend)
        allow_edits, shell_status = _matrix__infer_allow_edits_and_shell_status(
            cfg=cfg, request=req
        )
        if requires_edits and not allow_edits:
            continue
        if requires_shell and shell_status == "blocked":
            continue
        return policy

    # Fall back to any available policy if nothing matches (will be caught by validation).
    for policy in candidates:
        if policy in cfg.policies:
            return policy
    return next(iter(cfg.policies.keys()))


def _cmd_matrix_plan(args: argparse.Namespace) -> int:
    """Execute matrix planning without launching runs."""
    return _cmd_matrix(args, execute=False)


def _cmd_matrix_run(args: argparse.Namespace) -> int:
    """Execute matrix planning and run generated targets."""
    return _cmd_matrix(args, execute=True)


def _cmd_matrix(args: argparse.Namespace, *, execute: bool) -> int:
    """Execute matrix command flow for planning or execution."""
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)

    spec_path = Path(args.spec)
    if not spec_path.is_absolute():
        spec_path = (repo_root / spec_path).resolve()

    spec_raw = _load_yaml(spec_path)
    if not isinstance(spec_raw, dict):
        raise ValueError(f"Matrix spec must be a mapping (YAML object): {spec_path}")

    repo_input = _coerce_string(spec_raw.get("repo"))
    if repo_input is None:
        raise ValueError(f"Matrix spec missing required field 'repo': {spec_path}")

    ref = _coerce_string(spec_raw.get("ref"))
    default_policy = (_coerce_string(spec_raw.get("policy")) or "auto").strip() or "auto"

    persona_ids = _matrix__parse_persona_ids(
        spec_raw.get("personas") if "personas" in spec_raw else spec_raw.get("persona_ids"),
        spec_path=spec_path,
    )

    mission_ids, mission_overrides = _matrix__parse_mission_entries(
        spec_raw.get("missions") if "missions" in spec_raw else spec_raw.get("mission_ids"),
        spec_path=spec_path,
    )

    seeds = _matrix__parse_seeds(
        spec_raw.get("seeds") if "seeds" in spec_raw else spec_raw.get("seed"), spec_path=spec_path
    )

    agent_entries = _matrix__parse_agent_entries(
        spec_raw.get("agents") if "agents" in spec_raw else spec_raw.get("providers"),
        spec_path=spec_path,
    )

    # Load catalog once (best-effort: local repo if available).
    target_repo_root = _resolve_local_repo_root(repo_root, repo_input)
    catalog_config = load_catalog_config(repo_root, target_repo_root)
    persona_by_id = discover_personas(catalog_config)
    mission_by_id = discover_missions(catalog_config)

    # Resolve defaults if caller used null persona/mission.
    resolved_persona_ids: list[str] = []
    for pid in persona_ids:
        if pid is None:
            if catalog_config.defaults_persona_id is None:
                raise ValueError("No default persona_id configured (matrix spec used null).")
            resolved_persona_ids.append(catalog_config.defaults_persona_id)
        else:
            resolved_persona_ids.append(pid)

    resolved_mission_ids: list[str] = []
    for mid in mission_ids:
        if mid is None:
            if catalog_config.defaults_mission_id is None:
                raise ValueError("No default mission_id configured (matrix spec used null).")
            resolved_mission_ids.append(catalog_config.defaults_mission_id)
        else:
            resolved_mission_ids.append(mid)

    # Expand cartesian product.
    run_targets: list[dict[str, Any]] = []
    requests: list[tuple[int, RunRequest]] = []
    validation_report: list[dict[str, Any]] = []

    exec_backend = str(getattr(args, "exec_backend", "local"))

    # Prepare execution backend args shared across requests.
    exec_docker_context = _resolve_optional_path(
        repo_root, getattr(args, "exec_docker_context", None)
    )
    exec_cache_dir = _resolve_optional_path(repo_root, getattr(args, "exec_cache_dir", None))
    if exec_cache_dir is None and str(getattr(args, "exec_cache", "cold")) == "warm":
        exec_cache_dir = repo_root / "runs" / "_cache" / "usertest"
        if not bool(getattr(args, "skip_command_probes", False)):
            print(
                f"No --exec-cache-dir provided; using default: {exec_cache_dir}",
                file=sys.stderr,
            )

    exec_docker_timeout_seconds = getattr(args, "exec_docker_timeout_seconds", None)
    if exec_docker_timeout_seconds is not None and float(exec_docker_timeout_seconds) <= 0:
        exec_docker_timeout_seconds = None

    base_exec_env = tuple(
        str(x) for x in (getattr(args, "exec_env", None) or []) if isinstance(x, str) and x.strip()
    )

    # Expand runs.
    idx_counter = 0
    for pid in resolved_persona_ids:
        for mid in resolved_mission_ids:
            mission_spec = mission_by_id.get(mid)
            if mission_spec is None:
                # We still include it so the user sees the error in validation.
                base_requires_shell = False
                base_requires_edits = False
            else:
                base_requires_shell = bool(getattr(mission_spec, "requires_shell", False))
                base_requires_edits = bool(getattr(mission_spec, "requires_edits", False))

            overrides = mission_overrides.get(mid, {})
            requires_shell = bool(overrides.get("requires_shell", base_requires_shell))
            requires_edits = bool(overrides.get("requires_edits", base_requires_edits))

            for agent_entry in agent_entries:
                agent = str(agent_entry.get("agent"))
                policy_raw = str(agent_entry.get("policy") or default_policy or "auto")
                agent_config = [
                    x for x in (agent_entry.get("agent_config") or []) if isinstance(x, str)
                ]

                for model in agent_entry.get("models") or [None]:
                    for seed in seeds:
                        policy = policy_raw
                        if policy == "auto":
                            policy = _matrix__choose_policy_auto(
                                cfg=cfg,
                                agent=agent,
                                exec_backend=exec_backend,
                                requires_shell=requires_shell,
                                requires_edits=requires_edits,
                            )

                        req = RunRequest(
                            repo=repo_input,
                            ref=ref,
                            agent=agent,
                            policy=policy,
                            persona_id=pid,
                            mission_id=mid,
                            seed=int(seed),
                            model=(
                                str(model) if isinstance(model, str) and model.strip() else None
                            ),
                            agent_config_overrides=tuple(agent_config),
                            exec_backend=exec_backend,
                            exec_docker_context=exec_docker_context,
                            exec_dockerfile=getattr(args, "exec_dockerfile", None),
                            exec_docker_python=str(getattr(args, "exec_docker_python", "auto")),
                            exec_docker_timeout_seconds=(
                                float(exec_docker_timeout_seconds)
                                if exec_docker_timeout_seconds is not None
                                else None
                            ),
                            exec_use_target_sandbox_cli_install=bool(
                                getattr(args, "exec_use_target_sandbox_cli_install", False)
                            ),
                            exec_use_host_agent_login=bool(
                                getattr(args, "exec_use_host_agent_login", False)
                            ),
                            exec_network=str(getattr(args, "exec_network", "open")),
                            exec_cache=str(getattr(args, "exec_cache", "cold")),
                            exec_cache_dir=exec_cache_dir,
                            exec_env=base_exec_env,
                            exec_keep_container=bool(getattr(args, "exec_keep_container", False)),
                            exec_rebuild_image=bool(getattr(args, "exec_rebuild_image", False)),
                        )

                        # Record plan entry.
                        run_targets.append(
                            {
                                "repo": repo_input,
                                **({"ref": ref} if ref is not None else {}),
                                "agent": agent,
                                "policy": policy,
                                **({"model": req.model} if req.model is not None else {}),
                                "persona_id": pid,
                                "mission_id": mid,
                                "seed": int(seed),
                                **({"agent_config": agent_config} if agent_config else {}),
                                **(
                                    {
                                        "mission_requirements_override": {
                                            "requires_shell": requires_shell,
                                            "requires_edits": requires_edits,
                                        }
                                    }
                                    if overrides
                                    else {}
                                ),
                            }
                        )

                        # Validation entry.
                        errors: list[str] = []
                        warnings: list[str] = []

                        if agent not in cfg.agents:
                            errors.append(
                                f"unknown agent {agent!r} (defined in configs/agents.yaml)."
                            )
                        else:
                            agent_cfg = cfg.agents.get(agent, {})
                            adapter = (
                                agent_cfg.get("adapter") if isinstance(agent_cfg, dict) else None
                            )
                            if isinstance(adapter, str) and adapter.endswith("_cli"):
                                binary = (
                                    agent_cfg.get("binary") if isinstance(agent_cfg, dict) else None
                                )
                                binary = str(binary).strip() if binary is not None else agent
                                # Best-effort: verify the CLI exists on PATH (or the configured absolute path exists).
                                if binary:
                                    p = Path(binary)
                                    is_pathish = (
                                        p.is_absolute()
                                        or any(sep in binary for sep in ("/", "\\"))
                                        or (os.name == "nt" and ":" in binary)
                                    )
                                    if is_pathish and not p.exists():
                                        errors.append(
                                            f"agent binary not found: {binary!r} for agent {agent!r}"
                                        )
                                    elif not is_pathish and shutil.which(binary) is None:
                                        errors.append(
                                            f"agent binary not on PATH: {binary!r} for agent {agent!r}"
                                        )
                        if policy not in cfg.policies:
                            errors.append(
                                f"unknown policy {policy!r} (defined in configs/policies.yaml)."
                            )
                        if pid not in persona_by_id:
                            errors.append(
                                f"unknown persona_id {pid!r} (available: {', '.join(sorted(persona_by_id.keys()))})."
                            )
                        if mid not in mission_by_id:
                            errors.append(
                                f"unknown mission_id {mid!r} (available: {', '.join(sorted(mission_by_id.keys()))})."
                            )

                        allow_edits = False
                        shell_status = "unknown"
                        if not errors:
                            allow_edits, shell_status = _matrix__infer_allow_edits_and_shell_status(
                                cfg=cfg, request=req
                            )
                            if requires_shell and shell_status == "blocked":
                                errors.append(
                                    "requires shell commands, but this agent/policy/backend blocks shell commands"
                                )
                            if requires_edits and not allow_edits:
                                errors.append(
                                    "requires edits, but this policy has allow_edits=false"
                                )
                            if (
                                (not requires_shell)
                                and policy in {"inspect", "write"}
                                and shell_status == "blocked"
                            ):
                                warnings.append(
                                    "policy suggests shell should be available, but backend blocks it (gemini on Windows typically needs --exec-backend docker)"
                                )

                        validation_report.append(
                            {
                                "index": idx_counter,
                                "repo": repo_input,
                                "ref": ref,
                                "agent": agent,
                                "model": req.model,
                                "policy": policy,
                                "persona_id": pid,
                                "mission_id": mid,
                                "seed": int(seed),
                                "requirements": {
                                    "requires_shell": requires_shell,
                                    "requires_edits": requires_edits,
                                    "overrides": overrides,
                                },
                                "capabilities": {
                                    "allow_edits": allow_edits,
                                    "shell_status": shell_status,
                                    "exec_backend": exec_backend,
                                },
                                "errors": errors,
                                "warnings": warnings,
                            }
                        )

                        requests.append((idx_counter, req))
                        idx_counter += 1

    # Determine default output paths.
    target_slug = slugify(repo_input)
    compiled_dir = repo_root / "runs" / "usertest" / target_slug / "_compiled"
    compiled_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    out_targets = getattr(args, "out_targets", None)
    if out_targets is None:
        out_targets = compiled_dir / f"{timestamp}.matrix.targets.yaml"
    if not Path(out_targets).is_absolute():
        out_targets = (repo_root / Path(out_targets)).resolve()

    out_report = getattr(args, "out_report", None)
    if out_report is None:
        out_report = compiled_dir / f"{timestamp}.matrix.validation.json"
    if not Path(out_report).is_absolute():
        out_report = (repo_root / Path(out_report)).resolve()

    # Write expanded targets YAML.
    targets_doc = {
        "meta": {
            "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "spec_path": str(spec_path),
            "repo": repo_input,
            "ref": ref,
            "exec_backend": exec_backend,
        },
        "targets": run_targets,
    }
    Path(out_targets).parent.mkdir(parents=True, exist_ok=True)
    Path(out_targets).write_text(yaml.safe_dump(targets_doc, sort_keys=False), encoding="utf-8")

    # Write validation report.
    Path(out_report).parent.mkdir(parents=True, exist_ok=True)
    Path(out_report).write_text(
        json.dumps(
            {
                "meta": targets_doc["meta"],
                "totals": {
                    "combinations": len(validation_report),
                    "errors": sum(1 for r in validation_report if r.get("errors")),
                    "warnings": sum(1 for r in validation_report if r.get("warnings")),
                },
                "results": validation_report,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # Surface validation errors.
    error_count = sum(1 for r in validation_report if r.get("errors"))
    warning_count = sum(1 for r in validation_report if r.get("warnings"))

    print(str(out_targets))
    print(str(out_report))
    print(f"matrix combinations: {len(validation_report)}")
    print(f"validation errors: {error_count}")
    print(f"validation warnings: {warning_count}")

    if error_count:
        # Print a short, grouped error summary.
        print("Matrix validation failed; no runs were executed.", file=sys.stderr)
        shown = 0
        for entry in validation_report:
            errs = entry.get("errors") or []
            if not errs:
                continue
            shown += 1
            if shown <= 25:
                ident = (
                    f"[{entry.get('index')}] agent={entry.get('agent')} model={entry.get('model')} "
                    f"policy={entry.get('policy')} persona={entry.get('persona_id')} mission={entry.get('mission_id')} seed={entry.get('seed')}"
                )
                print(f"- {ident}", file=sys.stderr)
                for err in errs:
                    print(f"    - {err}", file=sys.stderr)
        if shown > 25:
            print(f"... and {shown - 25} more", file=sys.stderr)
        return 2

    # Run additional environment probes via the existing batch validator (it also checks local repo paths).
    batch_errors = _prevalidate_batch_requests(
        cfg=cfg,
        repo_root=repo_root,
        targets_path=spec_path,
        requests=requests,
        probe_timeout_seconds=float(getattr(args, "command_probe_timeout_seconds", 0.25)),
        skip_command_responsiveness_probes=bool(getattr(args, "skip_command_probes", False)),
        validate_only=not execute,
    )
    if batch_errors:
        print("Matrix environment validation failed; no runs were executed.", file=sys.stderr)
        for e in batch_errors:
            print(f"- {e}", file=sys.stderr)
        return 2

    if not execute:
        return 0

    exit_code = 0
    for _idx, req in requests:
        result = run_once(cfg, req)
        print(str(result.run_dir))
        if result.exit_code != 0 or result.report_validation_errors:
            exit_code = 2

    return exit_code


def _lint__add_issue(
    issues: list[dict[str, Any]],
    *,
    severity: str,
    code: str,
    message: str,
    path: Path | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Append a structured lint issue to the issue list."""
    if severity not in {"error", "warning"}:
        severity = "warning"
    issue: dict[str, Any] = {
        "severity": severity,
        "code": code,
        "message": message,
    }
    if path is not None:
        issue["path"] = str(path)
    if details:
        issue["details"] = details
    issues.append(issue)


def _lint__parse_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """
    Parse a markdown file with leading YAML frontmatter.

    Returns (frontmatter_dict, body_md).

    Linting intentionally re-parses the source files so it can detect
    whether keys were explicitly declared vs implicitly defaulted.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"Missing YAML frontmatter in {path} (expected leading '---').")

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"Invalid YAML frontmatter start in {path} (expected '---').")

    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise ValueError(f"Unterminated YAML frontmatter in {path} (missing closing '---').")

    fm_text = "\n".join(lines[1:end_idx]).strip()
    body_text = "\n".join(lines[end_idx + 1 :]).strip()

    fm_raw = yaml.safe_load(fm_text) if fm_text else {}
    if fm_raw is None:
        fm_raw = {}
    if not isinstance(fm_raw, dict):
        raise ValueError(f"Expected YAML frontmatter mapping in {path}.")
    return fm_raw, body_text


_LINT_EXECUTION_HINT_RE = re.compile(
    r"\b(execute|run(?!book)|install|build|compile|test|start(?!\s+conditions)|launch|serve|cli\s+command)\b",
    re.IGNORECASE,
)
_LINT_EDIT_HINT_RE = re.compile(
    r"\b(edit|modify|patch|update|change\s+config|apply\s+change|fix\s+by\s+editing)\b",
    re.IGNORECASE,
)


def _lint__lint_policies(*, cfg: RunnerConfig, issues: list[dict[str, Any]]) -> None:
    """Validate policy configuration semantics for lint output."""
    policies = cfg.policies or {}
    if not isinstance(policies, dict):
        _lint__add_issue(
            issues,
            severity="error",
            code="policies_not_mapping",
            message="configs/policies.yaml did not parse into a 'policies' mapping.",
        )
        return

    expected = ("safe", "inspect", "write")
    for name in expected:
        if name not in policies:
            _lint__add_issue(
                issues,
                severity="warning",
                code="policy_missing",
                message=f"Policy '{name}' is missing from configs/policies.yaml.",
                details={"policy": name},
            )

    def _get_agent_section(policy_name: str, agent: str) -> dict[str, Any]:
        policy = policies.get(policy_name)
        if not isinstance(policy, dict):
            return {}
        section = policy.get(agent)
        return section if isinstance(section, dict) else {}

    def _bool_field(section: dict[str, Any], key: str, default: bool) -> bool:
        raw = section.get(key, default)
        return bool(raw) if isinstance(raw, bool) else default

    def _claude_shell(section: dict[str, Any]) -> bool:
        tools = section.get("allowed_tools")
        if not isinstance(tools, list):
            return False
        return any(isinstance(x, str) and x == "Bash" for x in tools)

    def _gemini_shell(section: dict[str, Any]) -> bool:
        tools = section.get("allowed_tools")
        if not isinstance(tools, list):
            return False
        return any(isinstance(x, str) and x == "run_shell_command" for x in tools)

    def _codex_sandbox(section: dict[str, Any]) -> str | None:
        raw = section.get("sandbox")
        return raw if isinstance(raw, str) else None

    # Enforce the core contract described in configs/policies.yaml comments:
    # safe: read-only, no shell; inspect: read-only + shell; write: edits (+ shell).
    checks = [
        ("safe", False, False),
        ("inspect", True, False),
        ("write", True, True),
    ]
    for policy_name, should_have_shell, _should_allow_edits in checks:
        for agent in ("claude", "gemini", "codex"):
            section = _get_agent_section(policy_name, agent)
            if not section:
                _lint__add_issue(
                    issues,
                    severity="warning",
                    code="policy_agent_section_missing",
                    message=f"Policy '{policy_name}' missing section for agent '{agent}'.",
                    details={"policy": policy_name, "agent": agent},
                )
                continue

            allow_edits = _bool_field(section, "allow_edits", False)

            if agent == "claude":
                has_shell = _claude_shell(section)
            elif agent == "gemini":
                has_shell = _gemini_shell(section)
            else:
                # Codex shell allowlist is not reliably inferable; only enforce edit/sandbox basics.
                has_shell = should_have_shell

            if policy_name in {"safe", "inspect"} and allow_edits:
                _lint__add_issue(
                    issues,
                    severity="error",
                    code="policy_allows_edits_in_readonly_mode",
                    message=(
                        f"Policy '{policy_name}' for agent '{agent}' has allow_edits=true, "
                        "but this policy is documented as read-only."
                    ),
                    details={"policy": policy_name, "agent": agent, "allow_edits": allow_edits},
                )

            if policy_name == "write" and not allow_edits:
                _lint__add_issue(
                    issues,
                    severity="error",
                    code="policy_write_disallows_edits",
                    message=f"Policy 'write' for agent '{agent}' has allow_edits=false.",
                    details={"policy": policy_name, "agent": agent, "allow_edits": allow_edits},
                )

            if agent in {"claude", "gemini"}:
                if should_have_shell and not has_shell:
                    _lint__add_issue(
                        issues,
                        severity="error",
                        code="policy_missing_shell_tools",
                        message=(
                            f"Policy '{policy_name}' for agent '{agent}' is expected to allow shell, "
                            "but the configured tool allowlist does not include shell."
                        ),
                        details={"policy": policy_name, "agent": agent},
                    )
                if (not should_have_shell) and has_shell:
                    _lint__add_issue(
                        issues,
                        severity="error",
                        code="policy_unexpected_shell_tools",
                        message=(
                            f"Policy '{policy_name}' for agent '{agent}' is expected to block shell, "
                            "but the configured tool allowlist enables shell."
                        ),
                        details={"policy": policy_name, "agent": agent},
                    )

            if agent == "codex":
                sandbox = _codex_sandbox(section)
                if policy_name == "write":
                    if sandbox not in {None, "workspace-write"}:
                        _lint__add_issue(
                            issues,
                            severity="warning",
                            code="codex_write_sandbox_unexpected",
                            message=(
                                "Codex policy 'write' typically uses sandbox='workspace-write'. "
                                f"Found sandbox={sandbox!r}."
                            ),
                            details={"policy": policy_name, "sandbox": sandbox},
                        )
                if policy_name in {"safe", "inspect"}:
                    if sandbox not in {None, "read-only"}:
                        _lint__add_issue(
                            issues,
                            severity="warning",
                            code="codex_readonly_sandbox_unexpected",
                            message=(
                                f"Codex policy '{policy_name}' typically uses sandbox='read-only'. "
                                f"Found sandbox={sandbox!r}."
                            ),
                            details={"policy": policy_name, "sandbox": sandbox},
                        )


def _lint__lint_catalog(
    *,
    repo_root: Path,
    target_repo_root: Path | None,
    issues: list[dict[str, Any]],
) -> None:
    """Validate catalog personas, missions, and templates for lint output."""
    catalog_config = load_catalog_config(repo_root, target_repo_root)

    try:
        personas = discover_personas(catalog_config)
        missions = discover_missions(catalog_config)
    except Exception as e:  # noqa: BLE001
        _lint__add_issue(
            issues,
            severity="error",
            code="catalog_discover_failed",
            message=str(e),
        )
        return

    # Validate defaults actually exist (load_catalog_config doesn't resolve IDs).
    if catalog_config.defaults_persona_id and catalog_config.defaults_persona_id not in personas:
        _lint__add_issue(
            issues,
            severity="error",
            code="catalog_default_persona_missing",
            message=f"defaults.persona_id={catalog_config.defaults_persona_id!r} not found in discovered personas.",
            details={"defaults.persona_id": catalog_config.defaults_persona_id},
        )
    if catalog_config.defaults_mission_id and catalog_config.defaults_mission_id not in missions:
        _lint__add_issue(
            issues,
            severity="error",
            code="catalog_default_mission_missing",
            message=f"defaults.mission_id={catalog_config.defaults_mission_id!r} not found in discovered missions.",
            details={"defaults.mission_id": catalog_config.defaults_mission_id},
        )

    # Validate prompt templates and schemas exist for every mission (prevents runtime failures).
    prompt_dir = catalog_config.prompt_templates_dir
    schema_dir = catalog_config.report_schemas_dir

    # Track explicit declaration of requirement keys per mission source.
    declared: dict[str, dict[str, Any]] = {}

    for mid, spec in missions.items():
        try:
            fm, body = _lint__parse_frontmatter(spec.source_path)
        except Exception as e:  # noqa: BLE001
            _lint__add_issue(
                issues,
                severity="error",
                code="mission_frontmatter_parse_failed",
                message=str(e),
                path=spec.source_path,
                details={"mission_id": mid},
            )
            continue

        declared[mid] = {
            "declares_requires_shell": "requires_shell" in fm,
            "declares_requires_edits": "requires_edits" in fm,
            "body": body,
            "tags": list(spec.tags),
            "extends": spec.extends,
            "source_path": spec.source_path,
        }

        # Check referenced prompt template.
        pt_rel = spec.prompt_template
        pt_path = Path(pt_rel)
        if not pt_path.is_absolute():
            pt_path = (prompt_dir / pt_path).resolve()
        if not pt_path.exists():
            _lint__add_issue(
                issues,
                severity="error",
                code="mission_prompt_template_missing",
                message=f"Mission '{mid}' references missing prompt template: {pt_path}",
                path=spec.source_path,
                details={"mission_id": mid, "prompt_template": pt_rel},
            )

        schema_rel = spec.report_schema
        schema_path = Path(schema_rel)
        if not schema_path.is_absolute():
            schema_path = (schema_dir / schema_path).resolve()
        if not schema_path.exists():
            _lint__add_issue(
                issues,
                severity="error",
                code="mission_report_schema_missing",
                message=f"Mission '{mid}' references missing report schema: {schema_path}",
                path=spec.source_path,
                details={"mission_id": mid, "report_schema": schema_rel},
            )

    # Now that we've parsed all missions, enforce explicit requirement declaration
    # somewhere in the extends chain (prevents silent default=false that breaks preflight validation).
    for mid, spec in missions.items():
        meta = declared.get(mid)
        if meta is None:
            continue

        def _chain_declares(flag_key: str, *, start_mid: str = mid) -> bool:
            cur: str | None = start_mid
            seen: set[str] = set()
            while cur and cur not in seen:
                seen.add(cur)
                m = declared.get(cur)
                if m and bool(m.get(flag_key)):
                    return True
                cur = missions[cur].extends
            return False

        has_shell_decl = _chain_declares("declares_requires_shell")
        has_edits_decl = _chain_declares("declares_requires_edits")

        # Escalate to error when the mission text strongly implies execution/editing.
        body = str(meta.get("body") or "")
        tags = set(str(t) for t in (meta.get("tags") or []))

        implies_shell = (
            bool(_LINT_EXECUTION_HINT_RE.search(body)) or ("p0" in tags) or ("onboarding" in tags)
        )
        implies_edits = bool(_LINT_EDIT_HINT_RE.search(body))

        if not has_shell_decl:
            _lint__add_issue(
                issues,
                severity=("error" if implies_shell else "warning"),
                code="mission_requires_shell_undeclared",
                message=(
                    f"Mission '{mid}' does not explicitly declare requires_shell (defaults to false). "
                    "Add `requires_shell: true|false` to mission YAML frontmatter so preflight/matrix validation is reliable."
                ),
                path=spec.source_path,
                details={
                    "mission_id": mid,
                    "extends": spec.extends,
                    "implies_shell": implies_shell,
                },
            )

        if not has_edits_decl:
            _lint__add_issue(
                issues,
                severity=("error" if implies_edits else "warning"),
                code="mission_requires_edits_undeclared",
                message=(
                    f"Mission '{mid}' does not explicitly declare requires_edits (defaults to false). "
                    "Add `requires_edits: true|false` to mission YAML frontmatter so preflight/matrix validation is reliable."
                ),
                path=spec.source_path,
                details={
                    "mission_id": mid,
                    "extends": spec.extends,
                    "implies_edits": implies_edits,
                },
            )

        # Heuristic sanity-check: explicit false + strong implication => warn.
        if has_shell_decl and not bool(getattr(spec, "requires_shell", False)) and implies_shell:
            _lint__add_issue(
                issues,
                severity="warning",
                code="mission_requires_shell_maybe_wrong",
                message=(
                    f"Mission '{mid}' has requires_shell=false, but the mission text/tags suggests execution. "
                    "Confirm that the mission is intended to work in --policy safe (no shell)."
                ),
                path=spec.source_path,
                details={"mission_id": mid},
            )
        if has_edits_decl and not bool(getattr(spec, "requires_edits", False)) and implies_edits:
            _lint__add_issue(
                issues,
                severity="warning",
                code="mission_requires_edits_maybe_wrong",
                message=(
                    f"Mission '{mid}' has requires_edits=false, but the mission text suggests editing. "
                    "Confirm whether it should require --policy write."
                ),
                path=spec.source_path,
                details={"mission_id": mid},
            )


def _cmd_lint(args: argparse.Namespace) -> int:
    """Execute the lint subcommand."""
    repo_root = _resolve_repo_root(getattr(args, "repo_root", None))
    cfg = _load_runner_config(repo_root)

    target_repo_root: Path | None = None
    temp_dir_obj: tempfile.TemporaryDirectory[str] | None = None

    repo_input = _coerce_string(getattr(args, "repo", None))
    if repo_input is not None:
        # Prefer linting the real local repo if it exists (no cloning/copying needed).
        local = _resolve_local_repo_root(repo_root, repo_input)
        if local is not None:
            target_repo_root = local
        else:
            temp_dir_obj = tempfile.TemporaryDirectory(prefix="usertest_lint_")
            dest = Path(temp_dir_obj.name) / "target"
            acquired = acquire_target(
                repo=repo_input, dest_dir=dest, ref=_coerce_string(getattr(args, "ref", None))
            )
            target_repo_root = acquired.workspace_dir

    issues: list[dict[str, Any]] = []
    _lint__lint_policies(cfg=cfg, issues=issues)
    _lint__lint_catalog(repo_root=repo_root, target_repo_root=target_repo_root, issues=issues)

    # Sort issues for stable output.
    severity_rank = {"error": 0, "warning": 1}
    issues.sort(
        key=lambda x: (
            severity_rank.get(str(x.get("severity")), 9),
            str(x.get("code")),
            str(x.get("path") or ""),
        )
    )

    totals = {
        "errors": sum(1 for i in issues if i.get("severity") == "error"),
        "warnings": sum(1 for i in issues if i.get("severity") == "warning"),
        "issues": len(issues),
    }

    report = {
        "meta": {
            "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "repo_root": str(repo_root),
            "target_repo_root": str(target_repo_root) if target_repo_root is not None else None,
            "repo": repo_input,
            "ref": _coerce_string(getattr(args, "ref", None)),
        },
        "totals": totals,
        "issues": issues,
    }

    out_json = getattr(args, "out_json", None)
    if out_json is not None:
        out_path = out_json
        if not out_path.is_absolute():
            out_path = (repo_root / out_path).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(str(out_path))

    fmt = str(getattr(args, "format", "text"))
    if fmt == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"lint errors: {totals['errors']}")
        print(f"lint warnings: {totals['warnings']}")
        if issues:
            for issue in issues:
                sev = issue.get("severity")
                code = issue.get("code")
                path = issue.get("path")
                msg = issue.get("message")
                loc = f" ({path})" if path else ""
                print(f"- [{sev}] {code}{loc}: {msg}")

    if temp_dir_obj is not None:
        temp_dir_obj.cleanup()

    fail_on_warn = bool(getattr(args, "strict", False))
    if totals["errors"] > 0:
        return 2
    if fail_on_warn and totals["warnings"] > 0:
        return 2
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    """Execute the report subcommand for a run directory."""
    repo_root = _resolve_repo_root(args.repo_root)
    _warn_legacy_runs_layout(repo_root)

    run_dir: Path = args.run_dir
    if not run_dir.is_absolute() and not run_dir.exists():
        run_dir = repo_root / run_dir
    run_dir = run_dir.resolve()

    if args.recompute_metrics:
        raw_events_path = run_dir / "raw_events.jsonl"
        if not raw_events_path.exists():
            raise FileNotFoundError(f"Missing {raw_events_path}")

        agent_name: str | None = None
        target_ref_path = run_dir / "target_ref.json"
        if target_ref_path.exists():
            target_ref_raw = json.loads(target_ref_path.read_text(encoding="utf-8"))
            if isinstance(target_ref_raw, dict):
                agent_name_raw = target_ref_raw.get("agent")
                agent_name = agent_name_raw if isinstance(agent_name_raw, str) else None

        workspace_root: Path | None = None
        if agent_name == "codex":
            try:
                with raw_events_path.open("r", encoding="utf-8") as f:
                    for _ in range(20):
                        line = f.readline()
                        if not line:
                            break
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        workdir = obj.get("workdir")
                        if isinstance(workdir, str) and workdir:
                            wd = workdir[4:] if workdir.startswith("\\\\?\\") else workdir
                            wd_path = Path(wd)
                            workspace_root = wd_path if wd_path.exists() else None
                            break
            except OSError:
                workspace_root = None

        normalized_events_path = run_dir / "normalized_events.jsonl"
        ts_iter: Iterator[str] | None = None
        raw_ts_f = None
        raw_ts_iter: Iterator[str] | None = None
        raw_events_ts_path = raw_events_path.with_suffix(".ts.jsonl")
        if raw_events_ts_path.exists():
            try:
                raw_ts_f = raw_events_ts_path.open("r", encoding="utf-8")
                raw_ts_iter = (line.strip() for line in raw_ts_f if line.strip())
            except OSError:
                raw_ts_f = None
                raw_ts_iter = None
        elif normalized_events_path.exists():
            try:
                ts_values: list[str] = []
                for event in iter_events_jsonl(normalized_events_path):
                    ts = event.get("ts")
                    if isinstance(ts, str) and ts.strip():
                        ts_values.append(ts.strip())
                if ts_values:
                    ts_iter = iter(ts_values)
            except Exception:  # noqa: BLE001
                ts_iter = None
        try:
            if agent_name == "codex":
                normalize_codex_events(
                    raw_events_path=raw_events_path,
                    normalized_events_path=normalized_events_path,
                    ts_iter=ts_iter,
                    raw_ts_iter=raw_ts_iter,
                    workspace_root=workspace_root,
                )
            elif agent_name == "claude":
                normalize_claude_events(
                    raw_events_path=raw_events_path,
                    normalized_events_path=normalized_events_path,
                    ts_iter=ts_iter,
                    raw_ts_iter=raw_ts_iter,
                    workspace_root=workspace_root,
                )
            elif agent_name == "gemini":
                normalize_gemini_events(
                    raw_events_path=raw_events_path,
                    normalized_events_path=normalized_events_path,
                    ts_iter=ts_iter,
                    raw_ts_iter=raw_ts_iter,
                    workspace_root=workspace_root,
                )
            else:
                raise ValueError(
                    "Cannot recompute metrics: could not determine agent type from target_ref.json."
                )
        finally:
            if raw_ts_f is not None:
                raw_ts_f.close()

        diff_numstat: list[dict[str, Any]] = []
        diff_numstat_path = run_dir / "diff_numstat.json"
        if diff_numstat_path.exists():
            try:
                diff_raw = json.loads(diff_numstat_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                diff_raw = None

            if isinstance(diff_raw, list):
                diff_numstat = [x for x in diff_raw if isinstance(x, dict)]
                if diff_numstat:
                    with normalized_events_path.open("a", encoding="utf-8", newline="\n") as out_f:
                        for item in diff_numstat:
                            path = item.get("path")
                            lines_added = item.get("lines_added")
                            lines_removed = item.get("lines_removed")
                            if not isinstance(path, str):
                                continue
                            if not isinstance(lines_added, int) or not isinstance(
                                lines_removed, int
                            ):
                                continue
                            out_f.write(
                                json.dumps(
                                    make_event(
                                        "write_file",
                                        {
                                            "path": path,
                                            "lines_added": lines_added,
                                            "lines_removed": lines_removed,
                                        },
                                        ts=next(ts_iter, None) if ts_iter is not None else None,
                                    ),
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )

        recomputed_metrics = compute_metrics(iter_events_jsonl(normalized_events_path))
        if diff_numstat:
            recomputed_metrics["diff_numstat"] = diff_numstat
        metrics_path = run_dir / "metrics.json"
        metrics_path.write_text(
            json.dumps(recomputed_metrics, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    report_path = run_dir / "report.json"
    if not report_path.exists():
        raise FileNotFoundError(f"Missing {report_path}. Did the run succeed?")

    report_raw = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report_raw, dict):
        raise ValueError(f"{report_path} must contain a JSON object.")

    schema: dict[str, Any] | None = None
    schema_path = run_dir / "report.schema.json"
    if schema_path.exists():
        schema_raw = json.loads(schema_path.read_text(encoding="utf-8"))
        schema = schema_raw if isinstance(schema_raw, dict) else None
        if schema is None:
            print(
                f"WARNING: {schema_path} is not a JSON object; skipping validation.",
                file=sys.stderr,
            )
    else:
        fallback = repo_root / "configs" / "report.schema.json"
        if fallback.exists():
            print(f"WARNING: Missing {schema_path}; falling back to {fallback}.", file=sys.stderr)
            schema_raw = json.loads(fallback.read_text(encoding="utf-8"))
            schema = schema_raw if isinstance(schema_raw, dict) else None

    errors = validate_report(report_raw, schema) if schema is not None else []

    metrics: dict[str, Any] | None = None
    metrics_path = run_dir / "metrics.json"
    if metrics_path.exists():
        metrics_raw = json.loads(metrics_path.read_text(encoding="utf-8"))
        metrics = cast(dict[str, Any], metrics_raw) if isinstance(metrics_raw, dict) else None

    target_ref: dict[str, Any] | None = None
    target_ref_path = run_dir / "target_ref.json"
    if target_ref_path.exists():
        target_ref_raw = json.loads(target_ref_path.read_text(encoding="utf-8"))
        target_ref = target_ref_raw if isinstance(target_ref_raw, dict) else None

    md = render_report_markdown(report=report_raw, metrics=metrics, target_ref=target_ref)
    (run_dir / "report.md").write_text(md, encoding="utf-8", newline="\n")

    if errors:
        (run_dir / "report_validation_errors.json").write_text(
            json.dumps(errors, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    print(str(run_dir / "report.md"))
    if errors:
        print("Report validation errors:")
        for e in errors:
            print(f"- {e}")
    return 0 if not errors else 2


def _render_target_catalog_yaml(*, persona_id: str | None, mission_id: str | None) -> str:
    """Render target-level catalog override YAML content."""
    resolved_persona = persona_id or "quickstart_sprinter"
    resolved_mission = mission_id or "first_output_smoke"
    return "\n".join(
        [
            "version: 1",
            "",
            "# Target-local usertest overrides.",
            "#",
            "# Path semantics:",
            "# - `personas_dirs` / `missions_dirs` entries are resolved relative to the *target repo root*",
            "#   (the directory passed to `init-usertest --repo`), not relative to this file.",
            "# - These lists are additive: they are appended to the base catalog's directories.",
            "# - Duplicate persona/mission ids across directories are an error; use unique ids or `extends`.",
            "",
            "defaults:",
            f"  persona_id: {resolved_persona}",
            f"  mission_id: {resolved_mission}",
            "",
            "personas_dirs:",
            "  - .usertest/personas",
            "",
            "missions_dirs:",
            "  - .usertest/missions",
            "",
            "meta:",
            "  note: Put local `*.persona.md` and `*.mission.md` files under the directories above.",
            "",
        ]
    )


def _cmd_init_users(args: argparse.Namespace) -> int:
    """Execute init-users to scaffold target user directories."""
    repo_root = _resolve_repo_root(args.repo_root)

    target_dir: Path = args.repo
    if not target_dir.is_absolute():
        target_dir = target_dir.resolve()

    if not target_dir.exists() or not target_dir.is_dir():
        raise FileNotFoundError(f"Target repo directory not found: {target_dir}")

    base_catalog = load_catalog_config(repo_root, None)

    usertest_dir = target_dir / ".usertest"
    catalog_dest = usertest_dir / "catalog.yaml"
    install_manifest_dest = usertest_dir / "sandbox_cli_install.yaml"

    existing_paths = [p for p in (catalog_dest, install_manifest_dest) if p.exists()]
    if existing_paths and not args.force:
        first = existing_paths[0]
        print(f"{first} already exists; use --force to overwrite .usertest scaffold.")
        return 2

    usertest_dir.mkdir(parents=True, exist_ok=True)
    personas_dir = usertest_dir / "personas"
    missions_dir = usertest_dir / "missions"
    personas_dir.mkdir(parents=True, exist_ok=True)
    missions_dir.mkdir(parents=True, exist_ok=True)

    # Ensure empty directories survive a git commit when the scaffold is checked in.
    (personas_dir / ".gitkeep").write_text("", encoding="utf-8", newline="\n")
    (missions_dir / ".gitkeep").write_text("", encoding="utf-8", newline="\n")

    catalog_dest.write_text(
        _render_target_catalog_yaml(
            persona_id=base_catalog.defaults_persona_id,
            mission_id=base_catalog.defaults_mission_id,
        ),
        encoding="utf-8",
    )

    install_manifest_dest.write_text(
        "\n".join(
            [
                "version: 1",
                "sandbox_cli_install:",
                "  apt: []",
                "  pip: []",
                "  npm_global: []",
                "",
                "meta:",
                "  note: Optional sandbox package/tooling installs for docker sandbox runs.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print(str(usertest_dir))
    return 0


def _cmd_personas_list(args: argparse.Namespace) -> int:
    """Execute personas list and print discovered personas."""
    repo_root = _resolve_repo_root(args.repo_root)
    repo_arg = args.repo if isinstance(args.repo, str) and args.repo.strip() else None

    try:
        if repo_arg is not None:
            with tempfile.TemporaryDirectory(prefix="usertest_catalog_") as tmp_dir:
                dest_dir = Path(tmp_dir) / "workspace"
                acquired = acquire_target(repo=repo_arg, dest_dir=dest_dir, ref=None)
                try:
                    catalog_cfg = load_catalog_config(repo_root, acquired.workspace_dir)
                    personas = discover_personas(catalog_cfg)
                finally:
                    shutil.rmtree(acquired.workspace_dir, ignore_errors=True)
        else:
            catalog_cfg = load_catalog_config(repo_root, None)
            personas = discover_personas(catalog_cfg)

        for persona_id, spec in sorted(personas.items(), key=lambda kv: kv[0]):
            print(f"{persona_id}\t{spec.name}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(str(e), file=sys.stderr)
        return 2


def _cmd_missions_list(args: argparse.Namespace) -> int:
    """Execute missions list and print discovered missions."""
    repo_root = _resolve_repo_root(args.repo_root)
    repo_arg = args.repo if isinstance(args.repo, str) and args.repo.strip() else None

    try:
        if repo_arg is not None:
            with tempfile.TemporaryDirectory(prefix="usertest_catalog_") as tmp_dir:
                dest_dir = Path(tmp_dir) / "workspace"
                acquired = acquire_target(repo=repo_arg, dest_dir=dest_dir, ref=None)
                try:
                    catalog_cfg = load_catalog_config(repo_root, acquired.workspace_dir)
                    missions = discover_missions(catalog_cfg)
                finally:
                    shutil.rmtree(acquired.workspace_dir, ignore_errors=True)
        else:
            catalog_cfg = load_catalog_config(repo_root, None)
            missions = discover_missions(catalog_cfg)

        for mission_id, spec in sorted(missions.items(), key=lambda kv: kv[0]):
            print(f"{mission_id}\t{spec.name}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(str(e), file=sys.stderr)
        return 2


def _cmd_reports_compile(args: argparse.Namespace) -> int:
    """Execute reports compile to build report history artifacts."""
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)

    runs_dir = args.runs_dir.resolve() if args.runs_dir is not None else cfg.runs_dir
    target_slug: str | None = None
    if isinstance(args.target, str) and args.target.strip():
        target_slug = str(args.target).strip()
    repo_input = (
        str(args.repo_input).strip()
        if isinstance(args.repo_input, str) and args.repo_input.strip()
        else None
    )

    out_path: Path
    if args.out is not None:
        out_path = _resolve_optional_path(repo_root, args.out) or args.out.resolve()
    else:
        default_name = slugify(repo_input) if repo_input is not None else (target_slug or "all")
        if target_slug is not None:
            out_path = runs_dir / target_slug / "_compiled" / f"{default_name}.report_history.jsonl"
        else:
            out_path = runs_dir / "_compiled" / f"{default_name}.report_history.jsonl"

    counts = write_report_history_jsonl(
        runs_dir,
        out_path=out_path,
        target_slug=target_slug,
        repo_input=repo_input,
        embed=str(args.embed),
        max_embed_bytes=int(args.max_embed_bytes),
    )

    print(str(out_path))
    print(json.dumps(counts, indent=2, ensure_ascii=False))
    return 0


def _cmd_reports_analyze(args: argparse.Namespace) -> int:
    """Execute reports analyze to generate issue analysis outputs."""
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)

    runs_dir = args.runs_dir.resolve() if args.runs_dir is not None else cfg.runs_dir
    history_path: Path | None
    if args.history is not None:
        history_path = _resolve_optional_path(repo_root, args.history) or args.history.resolve()
    else:
        history_path = None

    target_slug: str | None = None
    if isinstance(args.target, str) and args.target.strip():
        target_slug = str(args.target).strip()
    repo_input = (
        str(args.repo_input).strip()
        if isinstance(args.repo_input, str) and args.repo_input.strip()
        else None
    )

    default_name = slugify(repo_input) if repo_input is not None else (target_slug or "all")

    if args.out_json is not None:
        out_json = _resolve_optional_path(repo_root, args.out_json) or args.out_json.resolve()
    else:
        if history_path is not None:
            out_json = history_path.with_name(f"{history_path.stem}.issue_analysis.json")
        elif target_slug is not None:
            out_json = runs_dir / target_slug / "_compiled" / f"{default_name}.issue_analysis.json"
        else:
            out_json = runs_dir / "_compiled" / f"{default_name}.issue_analysis.json"

    if args.out_md is not None:
        out_md = _resolve_optional_path(repo_root, args.out_md) or args.out_md.resolve()
    else:
        out_md = out_json.with_suffix(".md")

    actions_path: Path | None
    if args.actions is not None:
        actions_path = _resolve_optional_path(repo_root, args.actions) or args.actions.resolve()
    else:
        default_actions = repo_root / "configs" / "issue_actions.json"
        actions_path = default_actions if default_actions.exists() else None

    history_source = history_path if history_path is not None else runs_dir
    records = list(
        iter_report_history(
            history_source,
            target_slug=target_slug,
            repo_input=repo_input,
            embed="none",
        )
    )
    summary = analyze_report_history(
        records,
        repo_root=repo_root,
        issue_actions_path=actions_path,
    )

    scope_bits = []
    if target_slug is not None:
        scope_bits.append(f"target={target_slug}")
    if repo_input is not None:
        scope_bits.append(f"repo_input={repo_input}")
    title_suffix = f" ({', '.join(scope_bits)})" if scope_bits else ""
    write_issue_analysis(
        summary,
        out_json_path=out_json,
        out_md_path=out_md,
        title=f"Usertest Issue Analysis{title_suffix}",
    )

    print(str(out_json))
    print(str(out_md))
    print(json.dumps(summary.get("totals", {}), indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> None:
    """Run the CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "run":
        raise SystemExit(_cmd_run(args))
    if args.cmd == "batch":
        raise SystemExit(_cmd_batch(args))
    if args.cmd == "matrix":
        if args.matrix_cmd == "plan":
            raise SystemExit(_cmd_matrix_plan(args))
        if args.matrix_cmd == "run":
            raise SystemExit(_cmd_matrix_run(args))
        raise SystemExit(2)
    if args.cmd == "lint":
        raise SystemExit(_cmd_lint(args))
    if args.cmd == "report":
        raise SystemExit(_cmd_report(args))
    if args.cmd == "init-usertest":
        raise SystemExit(_cmd_init_users(args))
    if args.cmd == "personas":
        if args.personas_cmd == "list":
            raise SystemExit(_cmd_personas_list(args))
        raise SystemExit(2)
    if args.cmd == "missions":
        if args.missions_cmd == "list":
            raise SystemExit(_cmd_missions_list(args))
        raise SystemExit(2)
    if args.cmd == "reports":
        if args.reports_cmd == "compile":
            raise SystemExit(_cmd_reports_compile(args))
        if args.reports_cmd == "analyze":
            raise SystemExit(_cmd_reports_analyze(args))
        raise SystemExit(2)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
