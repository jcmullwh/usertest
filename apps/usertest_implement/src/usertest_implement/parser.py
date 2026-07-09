# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

import argparse
from pathlib import Path

from usertest_implement.ci import _ci_timeout_seconds_arg, _optional_timeout_seconds
from usertest_implement.commands.maintenance_images import (
    _cmd_maintenance_images_cleanup,
    _cmd_maintenance_images_list,
)
from usertest_implement.commands.reports import _cmd_reports_summarize
from usertest_implement.commands.review import (
    _cmd_review_merge,
    _cmd_review_run,
    _cmd_review_status,
)
from usertest_implement.commands.run import _cmd_run
from usertest_implement.commands.tickets import (
    _cmd_tickets_discard,
    _cmd_tickets_list,
    _cmd_tickets_move,
    _cmd_tickets_next,
    _cmd_tickets_run_next,
)
from usertest_implement.shared import *


def _add_settings_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--settings",
        type=Path,
        help=(
            "Optional settings YAML path. Defaults to "
            f"<repo_root>/configs/{_SETTINGS_FILENAME} when present."
        ),
    )
    parser.add_argument(
        "--settings-profile",
        help="Optional settings profile name from the settings YAML.",
    )


def _add_run_execution_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", help="Override target repo input (path or git URL).")
    parser.add_argument("--ref", help="Optional git ref to checkout in the acquired workspace.")

    parser.add_argument("--agent", choices=["claude", "codex", "gemini"], default="codex")
    parser.add_argument("--model", help="Optional model override.")
    parser.add_argument("--policy", default="write")
    parser.add_argument("--persona-id", dest="persona_id", default=_DEFAULT_PERSONA_ID)
    parser.add_argument("--mission-id", dest="mission_id", default=_DEFAULT_MISSION_ID)
    parser.add_argument(
        "--implementation-review-agent",
        dest="implementation_review_agent",
        choices=["claude", "codex", "gemini"],
        help=(
            "Agent CLI used for the automatic post-implementation review after PR creation "
            "(default: settings value, otherwise the implementation agent)."
        ),
    )
    parser.add_argument(
        "--implementation-review-model",
        dest="implementation_review_model",
        help="Optional model override for the automatic post-implementation review.",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--agent-config-override",
        action="append",
        default=[],
        help="Repeatable agent config override strings.",
    )
    parser.add_argument("--keep-workspace", action="store_true", help="Keep workspace directory after run.")

    exec_backend_group = parser.add_mutually_exclusive_group()
    exec_backend_group.add_argument(
        "--exec-backend",
        choices=["docker", "local"],
        default="docker",
        help="Execution backend (default: docker).",
    )
    exec_backend_group.add_argument(
        "--no-docker",
        dest="exec_backend",
        action="store_const",
        const="local",
        help="Opt out of Docker sandboxing (exec_backend=local).",
    )
    run_auth_group = parser.add_mutually_exclusive_group()
    run_auth_group.add_argument(
        "--exec-use-host-agent-login",
        dest="exec_use_host_agent_login",
        action="store_true",
        default=True,
    )
    run_auth_group.add_argument(
        "--exec-use-api-key-auth",
        dest="exec_use_host_agent_login",
        action="store_false",
    )
    parser.add_argument("--exec-use-target-sandbox-cli-install", action="store_true", default=False)
    parser.add_argument(
        "--exec-docker-profile",
        choices=["standard", "maintenance"],
        help=(
            "Docker execution profile. Defaults to maintenance for same-repo maintenance targets "
            "and standard otherwise."
        ),
    )
    parser.add_argument(
        "--exec-keep-container",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep Docker container after the run (default: enabled).",
    )

    parser.add_argument(
        "--exec-cache",
        choices=["cold", "warm"],
        default="warm",
        help=(
            "Docker sandbox cache mode (default: warm). "
            "warm: mount a host directory at /cache (persists across runs; used for pip + PDM caches). "
            "cold: do not mount a persistent host cache (/cache is per-container and discarded)."
        ),
    )
    parser.add_argument(
        "--exec-cache-dir",
        type=Path,
        help=(
            "Host directory mounted at /cache when --exec-cache warm. "
            "Defaults to <repo_root>/runs/_cache/usertest_implement."
        ),
    )
    parser.add_argument(
        "--maintenance-venv-cache",
        dest="maintenance_venv_cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Enable Docker maintenance venv cache reuse for scaffold install tasks when --exec-cache warm "
            "(default: enabled). Use --no-maintenance-venv-cache to force full reinstalls."
        ),
    )
    parser.add_argument(
        "--exec-maintenance-image-metadata",
        dest="exec_maintenance_image_metadata_path",
        type=Path,
        help=(
            "Pre-resolved maintenance Docker image metadata JSON produced by batch preflight. "
            "When provided with --exec-docker-profile maintenance, the run uses its immutable "
            "image ref without repeating maintenance image pull/build/tag resolution."
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print selected ticket, effective settings, and run request; do not run the agent, "
            "commit, push, create PRs, or move tickets. For tickets run-next, the default "
            "backlog refresh still runs unless --no-refresh-backlog is also passed."
        ),
    )

    parser.add_argument(
        "--verify-command",
        action="append",
        dest="verification_commands",
        default=[],
        help=(
            "Repeatable verification command gate that must pass before handing off "
            "(default: run scripts/smoke.{ps1,sh} then scaffold install/lint/test across the repo "
            "(tools/scaffold/scaffold.py run --all ...) "
            "when --commit/--push/--pr)."
        ),
    )
    parser.add_argument(
        "--verify-timeout-seconds",
        dest="verification_timeout_seconds",
        type=float,
        default=None,
        help=(
            "Optional per-command timeout for --verify-command. "
            "When brokered final verification reuse is active, omitted or non-positive "
            "values fall back to the runner's bounded default timeout."
        ),
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Disable default verification gate (useful for debugging).",
    )
    parser.add_argument(
        "--verify-reuse",
        choices=["auto", "off"],
        default="auto",
        help=(
            "Reuse a runner-owned in-session final verification pass when available (default: auto). "
            "Use --verify-reuse off to force a separate post-agent rerun."
        ),
    )
    parser.add_argument(
        "--ci-timeout-seconds",
        type=float,
        default=None,
        help="Optional timeout waiting for GitHub Actions CI before creating a PR.",
    )
    parser.add_argument(
        "--skip-ci-wait",
        action="store_true",
        help="Skip waiting for GitHub Actions CI before creating a PR (not recommended).",
    )
    parser.add_argument(
        "--draft-pr-on-ci-failure",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="If CI does not pass, create a draft PR instead of failing PR creation (default: enabled).",
    )

    parser.add_argument(
        "--commit",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Create branch + commit changes in kept workspace. Parser default is disabled, but "
            "the auto-loaded default settings profile enables it unless --no-commit is passed."
        ),
    )
    parser.add_argument("--branch", help="Branch name override.")
    parser.add_argument("--commit-message", dest="commit_message", help="Commit message override.")
    parser.add_argument(
        "--git-user-name",
        dest="git_user_name",
        help="Git user.name used for commits (default: usertest-implement).",
    )
    parser.add_argument(
        "--git-user-email",
        dest="git_user_email",
        help="Git user.email used for commits (default: usertest-implement@local).",
    )

    parser.add_argument(
        "--push",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Push branch to remote. Parser default is disabled, but the auto-loaded default "
            "settings profile enables it unless --no-push is passed."
        ),
    )
    parser.add_argument("--remote-name", default="origin")
    parser.add_argument("--remote-url")
    parser.add_argument("--force-push", dest="force_push", action="store_true")
    parser.add_argument(
        "--base-branch",
        default="dev",
        help="Base branch for PR creation (default: dev).",
    )
    parser.add_argument(
        "--pr",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Best-effort PR creation via gh. Parser default is disabled, but the auto-loaded "
            "default settings profile enables it unless --no-pr is passed."
        ),
    )

    parser.add_argument(
        "--move-on-start",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Move ticket file to 3 - in_progress if possible (default: enabled).",
    )
    parser.add_argument(
        "--move-on-commit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Move ticket file to 4 - for_review after --commit (default: enabled).",
    )
    parser.add_argument(
        "--ledger",
        nargs="?",
        const=_DEFAULT_LEDGER_PATH,
        type=Path,
        help=(
            "Optional attempt ledger YAML. If provided without a value, defaults to "
            "<repo_root>/.agents/state/backlog_implement_actions.yaml."
        ),
    )


def _add_review_execution_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--agent", choices=["claude", "codex", "gemini"], default="codex")
    parser.add_argument("--model", help="Optional model override.")
    parser.add_argument("--policy", default="write")
    parser.add_argument("--persona-id", dest="persona_id", default=_DEFAULT_REVIEW_PERSONA_ID)
    parser.add_argument("--mission-id", dest="mission_id", default=_DEFAULT_REVIEW_MISSION_ID)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--agent-config-override",
        action="append",
        default=[],
        help="Repeatable agent config override strings.",
    )
    parser.add_argument("--keep-workspace", action="store_true", help="Keep workspace directory after run.")

    exec_backend_group = parser.add_mutually_exclusive_group()
    exec_backend_group.add_argument(
        "--exec-backend",
        choices=["docker", "local"],
        default="docker",
        help="Execution backend (default: docker).",
    )
    exec_backend_group.add_argument(
        "--no-docker",
        dest="exec_backend",
        action="store_const",
        const="local",
        help="Opt out of Docker sandboxing (exec_backend=local).",
    )
    run_auth_group = parser.add_mutually_exclusive_group()
    run_auth_group.add_argument(
        "--exec-use-host-agent-login",
        dest="exec_use_host_agent_login",
        action="store_true",
        default=True,
    )
    run_auth_group.add_argument(
        "--exec-use-api-key-auth",
        dest="exec_use_host_agent_login",
        action="store_false",
    )
    parser.add_argument("--exec-use-target-sandbox-cli-install", action="store_true", default=False)
    parser.add_argument(
        "--exec-docker-profile",
        choices=["standard", "maintenance"],
        help=(
            "Docker execution profile. Defaults to maintenance for same-repo maintenance targets "
            "and standard otherwise."
        ),
    )
    parser.add_argument(
        "--exec-keep-container",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep Docker container after the run (default: enabled).",
    )
    parser.add_argument(
        "--exec-cache",
        choices=["cold", "warm"],
        default="warm",
        help="Docker sandbox cache mode (default: warm).",
    )
    parser.add_argument(
        "--exec-cache-dir",
        type=Path,
        help="Host directory mounted at /cache when --exec-cache warm.",
    )
    parser.add_argument(
        "--maintenance-venv-cache",
        dest="maintenance_venv_cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable Docker maintenance venv cache reuse (default: enabled).",
    )
    parser.add_argument(
        "--exec-maintenance-image-metadata",
        dest="exec_maintenance_image_metadata_path",
        type=Path,
        help="Pre-resolved maintenance Docker image metadata JSON produced by batch preflight.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--ledger",
        nargs="?",
        const=_DEFAULT_LEDGER_PATH,
        type=Path,
        help=(
            "Optional attempt ledger YAML. If provided without a value, defaults to "
            "<repo_root>/.agents/state/backlog_implement_actions.yaml."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="usertest-implement")
    parser.add_argument("--repo-root", type=Path, help="Path to the usertest runner repo root.")

    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run one ticket implementation.")
    ticket_group = run_p.add_mutually_exclusive_group(required=True)
    ticket_group.add_argument(
        "--tickets-export",
        dest="tickets_export",
        type=Path,
        help="Tickets export JSON path.",
    )
    ticket_group.add_argument("--ticket-path", dest="ticket_path", type=Path, help="Ticket markdown path.")
    run_p.add_argument(
        "--fingerprint",
        required=False,
        help="Ticket fingerprint selector (requires --tickets-export).",
    )
    _add_settings_args(run_p)
    _add_run_execution_args(run_p)

    run_p.set_defaults(func=_cmd_run)

    review_p = sub.add_parser("review", help="Review and merge PR-backed implementation tickets.")
    review_sub = review_p.add_subparsers(dest="review_cmd", required=True)

    review_run_p = review_sub.add_parser("run", help="Run an implementation review for a PR-backed ticket.")
    review_run_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    review_run_group = review_run_p.add_mutually_exclusive_group(required=True)
    review_run_group.add_argument("--ticket-path", dest="ticket_path", type=Path)
    review_run_group.add_argument("--fingerprint")
    _add_review_execution_args(review_run_p)
    review_run_p.set_defaults(func=_cmd_review_run)

    review_status_p = review_sub.add_parser("status", help="Show the latest review summary for a ticket.")
    review_status_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    review_status_group = review_status_p.add_mutually_exclusive_group(required=True)
    review_status_group.add_argument("--ticket-path", dest="ticket_path", type=Path)
    review_status_group.add_argument("--fingerprint")
    review_status_p.add_argument(
        "--ledger",
        nargs="?",
        const=_DEFAULT_LEDGER_PATH,
        type=Path,
        help=(
            "Optional attempt ledger YAML. If provided without a value, defaults to "
            "<repo_root>/.agents/state/backlog_implement_actions.yaml."
        ),
    )
    review_status_p.set_defaults(func=_cmd_review_status)

    review_merge_p = review_sub.add_parser("merge", help="Merge a reviewed PR when review + CI are green.")
    review_merge_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    review_merge_group = review_merge_p.add_mutually_exclusive_group(required=True)
    review_merge_group.add_argument("--ticket-path", dest="ticket_path", type=Path)
    review_merge_group.add_argument("--fingerprint")
    review_merge_p.add_argument(
        "--ledger",
        nargs="?",
        const=_DEFAULT_LEDGER_PATH,
        type=Path,
        help=(
            "Optional attempt ledger YAML. If provided without a value, defaults to "
            "<repo_root>/.agents/state/backlog_implement_actions.yaml."
        ),
    )
    review_merge_p.set_defaults(func=_cmd_review_merge)

    maintenance_images_p = sub.add_parser(
        "maintenance-images",
        help="Inspect and prune local maintenance-image tags.",
    )
    maintenance_images_sub = maintenance_images_p.add_subparsers(
        dest="maintenance_images_cmd",
        required=True,
    )

    maintenance_images_list_p = maintenance_images_sub.add_parser(
        "list",
        help="List local maintenance-image tags retained on the Docker host.",
    )
    maintenance_images_list_p.add_argument(
        "--timeout-seconds",
        type=float,
        default=None,
        help="Timeout for Docker inventory commands.",
    )
    maintenance_images_list_p.set_defaults(func=_cmd_maintenance_images_list)

    maintenance_images_cleanup_p = maintenance_images_sub.add_parser(
        "cleanup",
        help="Prune old local maintenance-image tags using the configured retention policy.",
    )
    maintenance_images_cleanup_p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=None,
        help="Show what would be deleted without deleting it.",
    )
    maintenance_images_cleanup_p.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="Delete tags selected by the retention policy.",
    )
    maintenance_images_cleanup_p.add_argument(
        "--timeout-seconds",
        type=float,
        default=None,
        help="Timeout for Docker cleanup commands.",
    )
    maintenance_images_cleanup_p.set_defaults(func=_cmd_maintenance_images_cleanup)

    reports_p = sub.add_parser("reports", help="Reporting utilities.")
    reports_sub = reports_p.add_subparsers(dest="reports_cmd", required=True)
    summarize_p = reports_sub.add_parser("summarize", help="Summarize implementation runs into JSONL.")
    summarize_p.add_argument("--runs-dir", type=Path, help="Runs directory (default: runs/usertest_implement).")
    summarize_p.add_argument("--out", type=Path, help="Output JSONL path.")
    summarize_p.add_argument("--target", help="Optional target slug filter.")
    summarize_p.add_argument("--repo-input", help="Optional repo_input filter.")
    summarize_p.add_argument(
        "--test-command-regex",
        action="append",
        default=[],
        help="Override/extend test command regex patterns.",
    )
    summarize_p.set_defaults(func=_cmd_reports_summarize)

    tickets_p = sub.add_parser("tickets", help="Local ticket queue helpers (from .agents/plans).")
    tickets_sub = tickets_p.add_subparsers(dest="tickets_cmd", required=True)

    tickets_list_p = tickets_sub.add_parser("list", help="List tickets in .agents/plans.")
    tickets_list_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    tickets_list_p.set_defaults(func=_cmd_tickets_list)

    tickets_next_p = tickets_sub.add_parser("next", help="Select the next ticket by bucket priority.")
    tickets_next_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    tickets_next_p.add_argument("--bucket-priority", action="append", default=[])
    tickets_next_p.set_defaults(func=_cmd_tickets_next)

    tickets_run_next_p = tickets_sub.add_parser(
        "run-next",
        help=(
            "Refresh the backlog + ticket exports, then implement the next local plan ticket "
            "(implementation-only by default; commits/pushes/opens a PR unless disabled)."
        ),
    )
    tickets_run_next_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    tickets_run_next_p.add_argument("--bucket-priority", action="append", default=[])
    tickets_run_next_p.add_argument(
        "--kind-priority",
        action="append",
        default=[],
        help=(
            "Ticket kind ordering derived from markdown (repeatable). "
            "Defaults to: implementation."
        ),
    )
    tickets_run_next_p.add_argument(
        "--no-refresh-backlog",
        action="store_false",
        dest="refresh_backlog",
        default=True,
        help="Skip running usertest-backlog refresh steps before selecting the next ticket.",
    )
    tickets_run_next_p.add_argument("--backlog-target", help="Target slug for usertest-backlog refresh.")
    tickets_run_next_p.add_argument(
        "--backlog-runs-dir",
        type=Path,
        help="Runs directory for usertest-backlog refresh (default: <repo_root>/runs/usertest).",
    )
    tickets_run_next_p.add_argument(
        "--backlog-agent",
        choices=["claude", "codex", "gemini"],
        help="Agent CLI used for `usertest-backlog reports backlog`.",
    )
    tickets_run_next_p.add_argument(
        "--backlog-model",
        help="Optional model override for `usertest-backlog reports backlog`.",
    )
    tickets_run_next_p.add_argument(
        "--review-agent",
        choices=["claude", "codex", "gemini"],
        help="Agent CLI used for `usertest-backlog reports review-ux` (default: --backlog-agent).",
    )
    tickets_run_next_p.add_argument(
        "--review-model",
        help="Optional model override for `usertest-backlog reports review-ux`.",
    )
    _add_settings_args(tickets_run_next_p)
    _add_run_execution_args(tickets_run_next_p)
    tickets_run_next_p.set_defaults(commit=True, push=True, pr=True)
    tickets_run_next_p.set_defaults(func=_cmd_tickets_run_next)

    tickets_move_p = tickets_sub.add_parser("move", help="Move a ticket file between plan buckets.")
    tickets_move_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    tickets_move_p.add_argument("--fingerprint", required=True)
    tickets_move_p.add_argument("--to-bucket", required=True)
    tickets_move_p.add_argument("--dry-run", action="store_true")
    tickets_move_p.set_defaults(func=_cmd_tickets_move)

    tickets_discard_p = tickets_sub.add_parser(
        "discard",
        help="Move a generated ticket to the non-actioned discarded bucket.",
    )
    tickets_discard_p.add_argument("--owner-root", type=Path, default=Path.cwd())
    tickets_discard_p.add_argument("--fingerprint", required=True)
    tickets_discard_p.add_argument(
        "--reason",
        required=True,
        choices=["bad_solution", "duplicate", "obsolete", "not_repro", "other"],
    )
    tickets_discard_p.add_argument("--note")
    tickets_discard_p.add_argument(
        "--actions-yaml",
        type=Path,
        help="Backlog action ledger path (defaults to configs/backlog_actions.yaml).",
    )
    tickets_discard_p.add_argument(
        "--atom-actions-yaml",
        type=Path,
        help="Atom action ledger path (defaults to configs/backlog_atom_actions.yaml).",
    )
    tickets_discard_p.set_defaults(func=_cmd_tickets_discard)

    add_batch_subcommands(sub)

    return parser




__all__ = ["build_parser"]
