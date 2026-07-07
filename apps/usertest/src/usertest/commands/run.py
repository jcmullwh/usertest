"""Parser wiring for the ``usertest run`` command."""

from __future__ import annotations

import argparse
from pathlib import Path

from usertest.cli import _EXEC_CACHE_DIR_HELP, _EXEC_CACHE_HELP, _EXEC_NETWORK_HELP, _cmd_run


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


__all__ = ["add_run_command"]
