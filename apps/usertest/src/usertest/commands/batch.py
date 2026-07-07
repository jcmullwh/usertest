# ruff: noqa: E501
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from runner_core import RunnerConfig, RunRequest, run_once
from runner_core.catalog import load_catalog_config
from runner_core.python_interpreter_probe import probe_python_interpreters
from runner_core.run_spec import RunSpecError, resolve_effective_run_inputs

from usertest.commands.shared import (
    _EXEC_CACHE_DIR_HELP,
    _EXEC_CACHE_HELP,
    _EXEC_NETWORK_HELP,
    _default_builtin_sandbox_cli_context,
    _load_runner_config,
    _looks_like_local_repo_input,
    _resolve_local_repo_root,
    _resolve_optional_path,
    _resolve_repo_root,
    _serialize_run_request_for_print,
    _warn_legacy_runs_layout,
)


def add_batch_command(sub: argparse._SubParsersAction) -> None:
    batch_p = sub.add_parser(
        "batch",
        help="Run multiple targets from a YAML file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Batch validation runs in phases:\n"
            "  1) Parse/shape validation of targets.yaml (YAML syntax, required fields, types)\n"
            "  2) Catalog/policy/environment validation "
            "(persona/mission resolution, agent/policy checks,\n"
            "     local repo path checks, and optional command responsiveness probes)\n"
            "\n"
            "If validation passes, targets execute in-order unless "
            "--validate-only/--print-requests is set."
        ),
    )
    batch_p.add_argument("--targets", required=True, type=Path, help="YAML file with targets list.")
    batch_p.add_argument(
        "--validate-only",
        action="store_true",
        help=(
            "Validate targets.yaml and exit (no run dirs, no agent execution). "
            "Runs all validation phases described above."
        ),
    )
    batch_p.add_argument(
        "--print-requests",
        action="store_true",
        help=(
            "Print the resolved RunRequest list as deterministic JSON and exit "
            "(implies --validate-only; redacts sensitive KEY=VALUE pairs)."
        ),
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
        help=(
            "Repeatable agent config override (Codex: -c key=value) applied to all "
            "targets (overridable per target)."
        ),
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
        help=(
            "Optional per-command timeout for --verify-command checks "
            "(applied to all targets). When brokered final verification reuse is active, "
            "omitted or non-positive values fall back to the runner's bounded default timeout."
        ),
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
    batch_p.add_argument("--exec-backend", choices=["local", "docker"], default="docker")
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
        help=_EXEC_NETWORK_HELP,
    )
    batch_p.add_argument(
        "--exec-cache",
        choices=["cold", "warm"],
        default="cold",
        help=_EXEC_CACHE_HELP,
    )
    batch_p.add_argument("--exec-cache-dir", type=Path, help=_EXEC_CACHE_DIR_HELP)
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

    batch_p.set_defaults(func=_cmd_batch)

@dataclass(frozen=True)
class _TargetsYamlLocations:
    targets_value: tuple[int, int] | None
    target_entries: dict[int, tuple[int, int]]
    target_fields: dict[tuple[int, str], tuple[int, int]]

def _extract_targets_yaml_locations(text: str) -> _TargetsYamlLocations:
    """Best-effort extraction of 1-based (line, col) for targets.yaml semantic errors."""
    empty = _TargetsYamlLocations(
        targets_value=None,
        target_entries={},
        target_fields={},
    )
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except Exception:  # noqa: BLE001
        return empty
    if node is None:
        return empty
    try:
        from yaml.nodes import MappingNode, ScalarNode, SequenceNode
    except Exception:  # noqa: BLE001
        return empty
    if not isinstance(node, MappingNode):
        return empty

    def _mark_to_line_col(mark: Any) -> tuple[int, int] | None:
        if mark is None:
            return None
        line = getattr(mark, "line", None)
        col = getattr(mark, "column", None)
        if line is None or col is None:
            return None
        try:
            return (int(line) + 1, int(col) + 1)
        except Exception:  # noqa: BLE001
            return None

    targets_node = None
    targets_value_loc: tuple[int, int] | None = None
    for key_node, value_node in node.value:
        if isinstance(key_node, ScalarNode) and key_node.value == "targets":
            targets_node = value_node
            targets_value_loc = _mark_to_line_col(getattr(value_node, "start_mark", None))
            break

    if not isinstance(targets_node, SequenceNode):
        return _TargetsYamlLocations(
            targets_value=targets_value_loc,
            target_entries={},
            target_fields={},
        )

    target_entries: dict[int, tuple[int, int]] = {}
    target_fields: dict[tuple[int, str], tuple[int, int]] = {}

    for idx, item_node in enumerate(targets_node.value):
        item_loc = _mark_to_line_col(getattr(item_node, "start_mark", None))
        if item_loc is not None:
            target_entries[idx] = item_loc

        if not isinstance(item_node, MappingNode):
            continue

        for k_node, v_node in item_node.value:
            if not isinstance(k_node, ScalarNode):
                continue
            key = k_node.value
            value_loc = _mark_to_line_col(getattr(v_node, "start_mark", None))
            if value_loc is not None:
                target_fields[(idx, str(key))] = value_loc

    return _TargetsYamlLocations(
        targets_value=targets_value_loc,
        target_entries=target_entries,
        target_fields=target_fields,
    )

def _format_targets_yaml_location(
    *,
    targets_path: Path,
    locations: _TargetsYamlLocations | None,
    idx: int | None,
    field: str | None = None,
) -> str | None:
    if locations is None:
        return None
    if idx is None:
        if locations.targets_value is None:
            return None
        line, col = locations.targets_value
        return f"{targets_path}:{line}:{col}"
    if field is not None:
        loc = locations.target_fields.get((idx, field))
        if loc is not None:
            line, col = loc
            return f"{targets_path}:{line}:{col}"
    loc = locations.target_entries.get(idx)
    if loc is None:
        return None
    line, col = loc
    return f"{targets_path}:{line}:{col}"

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
        return f"command {command!r} resolves to an unusable Python interpreter ({code}): {reason}"

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
    target_locations: _TargetsYamlLocations | None = None,
) -> list[str]:
    """Validate batch requests against catalog and policy constraints."""
    errors: list[str] = []
    local_repos: list[Path] = []
    missing_agent_binaries: dict[tuple[str, str, str], list[int]] = {}

    def _append_targets_error(message: str, *, idx: int, field: str | None = None) -> None:
        loc = _format_targets_yaml_location(
            targets_path=targets_path,
            locations=target_locations,
            idx=idx,
            field=field,
        )
        if loc:
            message = f"{message} ({loc})"
        errors.append(message)

    for idx, req in requests:
        if req.agent not in cfg.agents:
            _append_targets_error(
                f"targets[{idx}]: unknown agent {req.agent!r} (defined in configs/agents.yaml).",
                idx=idx,
                field="agent",
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
                    missing_agent_binaries.setdefault(
                        (req.agent, binary, "not_on_path"), []
                    ).append(idx)

        if req.policy not in cfg.policies:
            _append_targets_error(
                f"targets[{idx}]: unknown policy {req.policy!r} (defined in configs/policies.yaml).",
                idx=idx,
                field="policy",
            )

        local_repo_root = _resolve_local_repo_root(repo_root, req.repo)
        if local_repo_root is None:
            if _looks_like_local_repo_input(req.repo):
                _append_targets_error(
                    f"targets[{idx}]: repo looks like a local path but does not exist: "
                    f"{req.repo!r} (from {targets_path})",
                    idx=idx,
                    field="repo",
                )
            continue
        if not local_repo_root.is_dir():
            _append_targets_error(
                f"targets[{idx}]: repo must be a directory (got file): {local_repo_root} "
                f"(from {targets_path})",
                idx=idx,
                field="repo",
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
                _append_targets_error(
                    f"targets[{idx}]: mission {effective_spec.mission_id!r} requires shell "
                    f"commands, but policy {req.policy!r} for agent {req.agent!r} blocks shell "
                    f"commands ({hint}).",
                    idx=idx,
                    field="mission_id",
                )
            if requires_edits and not allow_edits:
                _append_targets_error(
                    f"targets[{idx}]: mission {effective_spec.mission_id!r} requires edits, but "
                    f"policy {req.policy!r} for agent {req.agent!r} has allow_edits=false "
                    "(use policy=write).",
                    idx=idx,
                    field="mission_id",
                )
            if (
                (not requires_shell)
                and req.policy in {"inspect", "write"}
                and shell_status == "blocked"
            ):
                _append_targets_error(
                    f"targets[{idx}]: policy {req.policy!r} for agent {req.agent!r} blocks shell "
                    "commands for this backend (use --exec-backend docker for gemini on Windows, "
                    "or fix configs/policies.yaml).",
                    idx=idx,
                    field="policy",
                )
        except RunSpecError as e:
            parts = [str(e)]
            if isinstance(e.code, str) and e.code.strip():
                parts.append(f"code={e.code.strip()}")
            if isinstance(e.details, dict) and e.details:
                parts.append(f"details={json.dumps(e.details, ensure_ascii=False)}")
            if isinstance(e.hint, str) and e.hint.strip():
                parts.append(f"hint={e.hint.strip()}")
            _append_targets_error(
                f"targets[{idx}]: {' | '.join(parts)}",
                idx=idx,
            )
        except Exception as e:  # noqa: BLE001
            _append_targets_error(
                f"targets[{idx}]: failed to resolve persona/mission: {e}",
                idx=idx,
            )

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
        exec_docker_context = _default_builtin_sandbox_cli_context(repo_root)
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
    targets_text = ""
    target_locations: _TargetsYamlLocations | None = None
    try:
        targets_text = targets_path.read_text(encoding="utf-8")
        data = yaml.safe_load(targets_text)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError(f"Expected mapping in {targets_path}, got {type(data).__name__}")
        target_locations = _extract_targets_yaml_locations(targets_text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None) or getattr(e, "context_mark", None)
        location = str(targets_path)
        snippet = None
        if mark is not None and hasattr(mark, "line") and hasattr(mark, "column"):
            try:
                line = int(mark.line) + 1
                col = int(mark.column) + 1
            except Exception:  # noqa: BLE001
                line = None
                col = None
            if line is not None and col is not None:
                location = f"{targets_path}:{line}:{col}"
                try:
                    lines = targets_text.splitlines()
                    if 1 <= line <= len(lines):
                        snippet = lines[line - 1].rstrip("\r\n")
                    else:
                        snippet = ""
                    if not snippet.strip():
                        for cand in reversed(lines[: min(line - 1, len(lines))]):
                            cand = cand.rstrip("\r\n")
                            if cand.strip():
                                snippet = cand
                                break
                except Exception:  # noqa: BLE001
                    snippet = None
        summary = str(e).splitlines()[0].strip() or type(e).__name__
        print("Batch validation failed; no targets were executed.", file=sys.stderr)
        print(f"- YAML parse error in {location}: {summary}", file=sys.stderr)
        if isinstance(snippet, str) and snippet.strip():
            print(f"  > {snippet}", file=sys.stderr)
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

    def _with_targets_loc(message: str, *, idx: int | None, field: str | None = None) -> str:
        loc = _format_targets_yaml_location(
            targets_path=targets_path,
            locations=target_locations,
            idx=idx,
            field=field,
        )
        if loc:
            return f"{message} ({loc})"
        return message

    def _append_arg_list_errors(values: Any, *, flag: str) -> list[str]:
        if values is None:
            return []
        if not isinstance(values, list):
            parse_errors.append(
                f"args: {flag} must be repeatable strings; got {type(values).__name__}."
            )
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
            _with_targets_loc(
                f"targets: expected a list (YAML sequence) in {targets_path}; got {type(targets_raw).__name__}.",
                idx=None,
            )
        )
        targets: list[Any] = []
    else:
        targets = targets_raw
    requests: list[tuple[int, RunRequest]] = []
    for idx, item in enumerate(targets):
        target_errors: list[str] = []

        if not isinstance(item, dict):
            parse_errors.append(
                _with_targets_loc(
                    f"targets[{idx}]: must be a mapping (YAML object); got {type(item).__name__}.",
                    idx=idx,
                )
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
                _target_errors.append(
                    _with_targets_loc(
                        f"targets[{_idx}].{field} is required.",
                        idx=_idx,
                    )
                )
                return None
            if not isinstance(raw, str) or not raw.strip():
                _target_errors.append(
                    _with_targets_loc(
                        f"targets[{_idx}].{field} must be a non-empty string; got {raw!r}.",
                        idx=_idx,
                        field=field,
                    )
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
                _with_targets_loc(
                    f"targets[{idx}]: uses legacy keys ({legacy_list}). "
                    "Update to persona_id / mission_id and remove legacy fields.",
                    idx=idx,
                )
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
                    _with_targets_loc(
                        f"targets[{_idx}].{field} must be a string if present; got {type(raw).__name__}.",
                        idx=_idx,
                        field=field,
                    )
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
                    _with_targets_loc(
                        f"targets[{_idx}].{field} must be an integer; got bool.",
                        idx=_idx,
                        field=field,
                    )
                )
                return None
            if isinstance(raw, int):
                return raw
            if isinstance(raw, str):
                try:
                    return int(raw.strip())
                except ValueError:
                    _target_errors.append(
                        _with_targets_loc(
                            f"targets[{_idx}].{field} must be an integer; got {raw!r}.",
                            idx=_idx,
                            field=field,
                        )
                    )
                    return None
            _target_errors.append(
                _with_targets_loc(
                    f"targets[{_idx}].{field} must be an integer; got {type(raw).__name__}.",
                    idx=_idx,
                    field=field,
                )
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
                    _with_targets_loc(
                        f"targets[{_idx}].{field} must be a number; got bool.",
                        idx=_idx,
                        field=field,
                    )
                )
                return None
            if isinstance(raw, (int, float)):
                return float(raw)
            if isinstance(raw, str):
                try:
                    return float(raw.strip())
                except ValueError:
                    _target_errors.append(
                        _with_targets_loc(
                            f"targets[{_idx}].{field} must be a number; got {raw!r}.",
                            idx=_idx,
                            field=field,
                        )
                    )
                    return None
            _target_errors.append(
                _with_targets_loc(
                    f"targets[{_idx}].{field} must be a number; got {type(raw).__name__}.",
                    idx=_idx,
                    field=field,
                )
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
                    _with_targets_loc(
                        f"targets[{_idx}].{field} must be a number; got bool.",
                        idx=_idx,
                        field=field,
                    )
                )
                return None
            if isinstance(raw, (int, float)):
                return float(raw)
            if isinstance(raw, str):
                try:
                    return float(raw.strip())
                except ValueError:
                    _target_errors.append(
                        _with_targets_loc(
                            f"targets[{_idx}].{field} must be a number; got {raw!r}.",
                            idx=_idx,
                            field=field,
                        )
                    )
                    return None
            _target_errors.append(
                _with_targets_loc(
                    f"targets[{_idx}].{field} must be a number; got {type(raw).__name__}.",
                    idx=_idx,
                    field=field,
                )
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
                    _with_targets_loc(
                        f"targets[{idx}].agent_config must be a list of strings if present.",
                        idx=idx,
                        field="agent_config",
                    )
                )
            for jdx, override in enumerate(raw_agent_config):
                if not isinstance(override, str) or not override.strip():
                    target_errors.append(
                        _with_targets_loc(
                            f"targets[{idx}].agent_config[{jdx}] must be a non-empty string; got {override!r}.",
                            idx=idx,
                            field="agent_config",
                        )
                    )
                else:
                    agent_config_overrides.append(override.strip())
        raw_preflight_commands = item.get("preflight_commands")
        if raw_preflight_commands is not None:
            if not isinstance(raw_preflight_commands, list):
                target_errors.append(
                    _with_targets_loc(
                        f"targets[{idx}].preflight_commands must be a list of strings if present.",
                        idx=idx,
                        field="preflight_commands",
                    )
                )
            for jdx, cmd in enumerate(raw_preflight_commands):
                if not isinstance(cmd, str) or not cmd.strip():
                    target_errors.append(
                        _with_targets_loc(
                            f"targets[{idx}].preflight_commands[{jdx}] must be a non-empty string; "
                            f"got {cmd!r}.",
                            idx=idx,
                            field="preflight_commands",
                        )
                    )
                else:
                    preflight_commands.append(cmd.strip())

        raw_preflight_required = item.get("preflight_required_commands")
        if raw_preflight_required is not None:
            if not isinstance(raw_preflight_required, list):
                target_errors.append(
                    _with_targets_loc(
                        f"targets[{idx}].preflight_required_commands must be a list of strings "
                        f"if present.",
                        idx=idx,
                        field="preflight_required_commands",
                    )
                )
            for jdx, cmd in enumerate(raw_preflight_required):
                if not isinstance(cmd, str) or not cmd.strip():
                    target_errors.append(
                        _with_targets_loc(
                            f"targets[{idx}].preflight_required_commands[{jdx}] "
                            f"must be a non-empty string; got {cmd!r}.",
                            idx=idx,
                            field="preflight_required_commands",
                        )
                    )
                else:
                    preflight_required_commands.append(cmd.strip())

        raw_verification_commands = item.get("verification_commands")
        if raw_verification_commands is not None:
            if not isinstance(raw_verification_commands, list):
                target_errors.append(
                    _with_targets_loc(
                        f"targets[{idx}].verification_commands must be a list of strings if present.",
                        idx=idx,
                        field="verification_commands",
                    )
                )
            for jdx, cmd in enumerate(raw_verification_commands):
                if not isinstance(cmd, str) or not cmd.strip():
                    target_errors.append(
                        _with_targets_loc(
                            f"targets[{idx}].verification_commands[{jdx}] must be a non-empty string; "
                            f"got {cmd!r}.",
                            idx=idx,
                            field="verification_commands",
                        )
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
                retries_value if retries_value is not None else int(args.agent_rate_limit_retries)
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

    print_requests = bool(getattr(args, "print_requests", False))
    validate_only = bool(getattr(args, "validate_only", False)) or print_requests

    validation_errors = _prevalidate_batch_requests(
        cfg=cfg,
        repo_root=repo_root,
        targets_path=targets_path,
        requests=requests,
        probe_timeout_seconds=float(args.command_probe_timeout_seconds),
        skip_command_responsiveness_probes=bool(args.skip_command_probes),
        validate_only=validate_only,
        target_locations=target_locations,
    )
    all_errors = [*parse_errors, *validation_errors]
    if all_errors:
        print("Batch validation failed; no targets were executed.", file=sys.stderr)
        for e in all_errors:
            print(f"- {e}", file=sys.stderr)
        print("- See docs/reference/targets-yaml.md for targets.yaml format.", file=sys.stderr)
        return 2
    if print_requests:
        payload = [
            {"index": idx, "request": _serialize_run_request_for_print(req)}
            for idx, req in requests
        ]
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        print(
            "Batch validation passed; no targets were executed (--print-requests).", file=sys.stderr
        )
        return 0
    if validate_only:
        print("Batch validation passed; no targets were executed (validate-only).", file=sys.stderr)
        return 0

    exit_code = 0
    for _idx, req in requests:
        result = run_once(cfg, req)
        print(str(result.run_dir))
        if result.exit_code != 0 or result.report_validation_errors:
            exit_code = 2
    return exit_code

__all__ = ['add_batch_command', '_cmd_batch']
