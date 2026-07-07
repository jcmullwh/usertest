# ruff: noqa: E501
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from runner_core import RunRequest, run_once

from usertest.commands.shared import (
    _EXEC_CACHE_DIR_HELP,
    _EXEC_CACHE_HELP,
    _EXEC_NETWORK_HELP,
    _default_builtin_sandbox_cli_context,
    _load_runner_config,
    _resolve_optional_path,
    _resolve_repo_root,
    _warn_legacy_runs_layout,
)


def add_run_command(sub: argparse._SubParsersAction) -> None:
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
            '(e.g., --verify-command "python -m pytest -q"). Fails the run (and may trigger '
            "agent follow-ups) if any command exits non-zero."
        ),
    )
    run_p.add_argument(
        "--verify-timeout-seconds",
        type=float,
        default=None,
        help=(
            "Optional per-command timeout for --verify-command checks. "
            "When brokered final verification reuse is active, omitted or non-positive "
            "values fall back to the runner's bounded default timeout."
        ),
    )
    run_p.add_argument(
        "--exec-backend",
        choices=["local", "docker"],
        default="docker",
        help="Execution backend (default: docker).",
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
        help=_EXEC_NETWORK_HELP,
    )
    run_p.add_argument(
        "--exec-cache",
        choices=["cold", "warm"],
        default="cold",
        help=_EXEC_CACHE_HELP,
    )
    run_p.add_argument(
        "--exec-cache-dir",
        type=Path,
        help=_EXEC_CACHE_DIR_HELP,
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

    run_p.set_defaults(func=_cmd_run)

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

__all__ = ['add_run_command', '_cmd_run']
