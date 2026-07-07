"""Parser wiring for the ``usertest batch`` command."""

from __future__ import annotations

import argparse
from pathlib import Path

from usertest.cli import _EXEC_CACHE_DIR_HELP, _EXEC_CACHE_HELP, _EXEC_NETWORK_HELP, _cmd_batch


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


__all__ = ["add_batch_command"]
