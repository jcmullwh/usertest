# ruff: noqa: E501
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import warnings
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml
from backlog_core import (
    build_backlog_document,
    extract_backlog_atoms,
    write_backlog,
    write_backlog_atoms,
)
from backlog_core.aggregate_metrics import build_aggregate_metrics_atoms
from backlog_core.backlog_policy import BacklogPolicyConfig, apply_backlog_policy
from backlog_miner import (
    load_prompt_manifest,
    run_backlog_ensemble,
    run_backlog_prompt,
    run_labeler_jobs,
)
from backlog_repo import (
    canonicalize_failure_atom_id as _canonicalize_failure_atom_id,
)
from backlog_repo import (
    load_atom_actions_yaml as _load_atom_actions_yaml,
)
from backlog_repo import (
    load_backlog_actions_yaml as _load_backlog_actions_yaml,
)
from backlog_repo import (
    normalize_atom_status as _normalize_atom_status,
)
from backlog_repo import (
    promote_atom_status as _promote_atom_status,
)
from backlog_repo import (
    scan_plan_ticket_index as _scan_plan_ticket_index,
)
from backlog_repo import (
    sorted_unique_strings as _sorted_unique_strings,
)
from backlog_repo import (
    sync_atom_actions_from_plan_folders as _sync_atom_actions_from_plan_folders,
)
from backlog_repo import (
    write_atom_actions_yaml as _write_atom_actions_yaml,
)
from backlog_repo.export import ticket_export_fingerprint
from reporter import (
    analyze_report_history,
    write_issue_analysis,
)
from run_artifacts.history import iter_report_history, write_report_history_jsonl
from runner_core import RunnerConfig, RunRequest, find_repo_root
from runner_core.catalog import (
    CatalogError,
    load_catalog_config,
)
from runner_core.pathing import slugify
from runner_core.run_spec import RunSpecError, resolve_effective_run_inputs
from triage_engine import cluster_items, extract_path_anchors_from_chunks

from usertest_backlog.triage_backlog import (
    load_issue_items,
    triage_issues,
    write_triage_xlsx,
)
from usertest_backlog.triage_backlog import (
    render_triage_markdown as render_backlog_triage_markdown,
)

_LEGACY_RUN_TIMESTAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
_EXPORT_SEVERITY_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "blocker": 3}
_EXPORT_PATH_LIKE_RE = re.compile(r"(?:[A-Za-z]:[\\/])?[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+){1,}")
_EXPORT_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_MONOREPO_OWNER_COMPONENTS: set[str] = {"runner_core", "agent_adapters", "sandbox_runner"}
_ATOM_STATUS_ORDER: dict[str, int] = {"new": 0, "ticketed": 1, "queued": 2, "actioned": 3}
_WINDOWS_ABS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
try:
    from runner_core.python_interpreter_probe import probe_python_interpreters
except ModuleNotFoundError:
    probe_python_interpreters = None  # type: ignore[assignment]


def _enable_console_backslashreplace(stream: Any) -> None:
    """Handle enable console backslashreplace processing.

    Parameters
    ----------
    stream:
        Console stream to configure.

    Returns
    -------
    None
        None.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        if str(getattr(stream, "errors", "")).lower() == "backslashreplace":
            return
        reconfigure(errors="backslashreplace")
    except (OSError, ValueError):
        return


def _configure_console_output() -> None:
    """Handle configure console output processing.

    Returns
    -------
    None
        None.
    """
    _enable_console_backslashreplace(sys.stdout)
    _enable_console_backslashreplace(sys.stderr)


_configure_console_output()


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load yaml from disk or config inputs.

    Parameters
    ----------
    path:
        Filesystem path input.

    Returns
    -------
    dict[str, Any]
        Structured mapping result.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping in {path}, got {type(data).__name__}")
    return data


def _load_runner_config(repo_root: Path) -> RunnerConfig:
    """Load runner config from disk or config inputs.

    Parameters
    ----------
    repo_root:
        Repository root path.

    Returns
    -------
    RunnerConfig
        Computed return value.
    """
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
    """Return whether input looks like local repo input.

    Parameters
    ----------
    repo:
        Repository input string.

    Returns
    -------
    bool
        Boolean decision result.
    """
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
    """Resolve local repo root from provided inputs.

    Parameters
    ----------
    repo_root:
        Repository root path.
    repo:
        Repository input string.

    Returns
    -------
    Path | None
        Resolved filesystem path value.
    """
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
    """Infer responsiveness probe commands from available context.

    Parameters
    ----------
    repo_dir:
        Repository directory path.

    Returns
    -------
    set[str]
        Computed return value.
    """
    commands: set[str] = set()
    if (repo_dir / "pdm.lock").exists():
        commands.add("pdm")
    if (repo_dir / "package.json").exists():
        commands.update({"node", "npm"})
    return commands


def _probe_command_responsive(*, command: str, timeout_seconds: float) -> str | None:
    """Probe command responsive availability.

    Parameters
    ----------
    command:
        Input parameter.
    timeout_seconds:
        Input parameter.

    Returns
    -------
    str | None
        Computed return value.
    """
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
) -> list[str]:
    """Handle prevalidate batch requests processing.

    Parameters
    ----------
    cfg:
        Input parameter.
    repo_root:
        Repository root path.
    targets_path:
        Filesystem path input.
    requests:
        Input parameter.
    probe_timeout_seconds:
        Input parameter.
    skip_command_responsiveness_probes:
        Input parameter.

    Returns
    -------
    list[str]
        Normalized list result.
    """
    errors: list[str] = []
    local_repos: list[Path] = []

    for idx, req in requests:
        if req.agent not in cfg.agents:
            errors.append(
                f"targets[{idx}]: unknown agent {req.agent!r} (defined in configs/agents.yaml)."
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
                errors.append(
                    f"targets[{idx}]: mission {effective_spec.mission_id!r} requires shell "
                    f"commands, but policy {req.policy!r} for agent {req.agent!r} blocks shell "
                    "commands (use policy=inspect or policy=write)."
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
        except (CatalogError, OSError, RuntimeError, ValueError) as e:
            errors.append(f"targets[{idx}]: failed to resolve persona/mission: {e}")

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
    """Build the `usertest_backlog` CLI parser.

    Returns
    -------
    argparse.ArgumentParser
        Computed return value.
    """
    parser = argparse.ArgumentParser(prog="usertest-backlog")
    sub = parser.add_subparsers(dest="cmd", required=True)

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

    reports_backlog_p = reports_sub.add_parser(
        "backlog",
        help="Generate an actionable backlog using ensemble ticket miners over run artifacts.",
    )
    reports_backlog_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (e.g. tiktok_vids).",
    )
    reports_backlog_p.add_argument(
        "--repo-input",
        help="Optional match for target_ref.repo_input (path or git URL).",
    )
    reports_backlog_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_backlog_p.add_argument(
        "--out-json",
        type=Path,
        help=(
            "Output backlog JSON path (defaults under runs/usertest/<target>/_compiled/ "
            "or runs/usertest/_compiled/ when --target is omitted)."
        ),
    )
    reports_backlog_p.add_argument(
        "--out-md",
        type=Path,
        help="Output markdown summary path (defaults next to --out-json with .md extension).",
    )
    reports_backlog_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )
    reports_backlog_p.add_argument(
        "--prompts-dir",
        type=Path,
        help="Optional prompt template directory (defaults to configs/backlog_prompts).",
    )
    reports_backlog_p.add_argument(
        "--agent",
        choices=["claude", "codex", "gemini"],
        default="claude",
        help="Agent CLI used for ticket miner prompts.",
    )
    reports_backlog_p.add_argument(
        "--model",
        help="Optional model override for backlog miner prompts.",
    )
    reports_backlog_p.add_argument(
        "--miners",
        type=int,
        default=10,
        help="Total number of miner passes to run.",
    )
    reports_backlog_p.add_argument(
        "--sample-size",
        type=int,
        default=120,
        help="Atom sample size per miner pass (use 0 for uncapped/all-atoms sampling).",
    )
    reports_backlog_p.add_argument(
        "--coverage-miners",
        type=int,
        default=3,
        help="How many miners use partitioned coverage slices.",
    )
    reports_backlog_p.add_argument(
        "--bagging-miners",
        type=int,
        default=None,
        help="How many miners use weighted bagging (default: miners - coverage_miners).",
    )
    reports_backlog_p.add_argument(
        "--max-tickets-per-miner",
        type=int,
        default=12,
        help="Upper bound requested from each miner output.",
    )
    reports_backlog_p.add_argument(
        "--force",
        action="store_true",
        help="Rerun miners even when cached outputs exist.",
    )
    reports_backlog_p.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="Reuse cached miner outputs when available (default).",
    )
    reports_backlog_p.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Disable cache reuse and rerun missing stages.",
    )
    reports_backlog_p.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Random seed for miner sampling.",
    )
    reports_backlog_p.add_argument(
        "--no-merge",
        action="store_true",
        help="Skip merge-judge passes.",
    )
    reports_backlog_p.add_argument(
        "--orphan-pass",
        type=int,
        default=1,
        help="Number of additional miner passes for uncovered high-severity atoms.",
    )
    reports_backlog_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Only extract/write atoms and prompts; skip LLM mining.",
    )
    reports_backlog_p.add_argument(
        "--labelers",
        type=int,
        default=3,
        help=(
            "Run N labeler passes per ticket to classify change surface "
            "(default: 3; use 0 to disable). Labeling requires an agent CLI unless "
            "cached outputs exist."
        ),
    )
    reports_backlog_p.add_argument(
        "--policy-config",
        type=Path,
        help=(
            "Optional backlog policy YAML path. Defaults to "
            "configs/backlog_policy.yaml when present. "
            "Policy uses only structured fields (no regex, no text mining)."
        ),
    )
    reports_backlog_p.add_argument(
        "--no-policy",
        action="store_true",
        help="Disable applying the backlog policy engine.",
    )
    reports_backlog_p.add_argument(
        "--atom-actions-yaml",
        type=Path,
        help=(
            "Atom lifecycle ledger YAML path (defaults to configs/backlog_atom_actions.yaml). "
            "Backlog updates atom status to `new` or `ticketed` each run."
        ),
    )
    reports_backlog_p.add_argument(
        "--exclude-atom-status",
        action="append",
        choices=sorted(_ATOM_STATUS_ORDER.keys()),
        default=None,
        help=(
            "Atom statuses to exclude from backlog mining (repeatable). "
            "Default: ticketed + queued + actioned."
        ),
    )
    reports_backlog_p.add_argument(
        "--skip-plan-folder-sync",
        action="store_true",
        help=(
            "Skip syncing atom statuses from `.agents/plans/*` folder locations before filtering. "
            "Default behavior infers `queued`/`actioned` from ticket file locations."
        ),
    )

    reports_intent_snapshot_p = reports_sub.add_parser(
        "intent-snapshot",
        help=(
            "Generate a repo intent snapshot artifact (command surface + docs index; "
            "optional LLM summary)."
        ),
    )
    reports_intent_snapshot_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (controls output directory scope).",
    )
    reports_intent_snapshot_p.add_argument(
        "--repo-input",
        help="Optional repo_input label used for output naming (path or git URL).",
    )
    reports_intent_snapshot_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_intent_snapshot_p.add_argument(
        "--out-json",
        type=Path,
        help=(
            "Output intent snapshot JSON path (defaults under runs/usertest/<target>/_compiled/ "
            "or runs/usertest/_compiled/ when --target is omitted)."
        ),
    )
    reports_intent_snapshot_p.add_argument(
        "--out-md",
        type=Path,
        help="Output markdown summary path (defaults next to --out-json with .md extension).",
    )
    reports_intent_snapshot_p.add_argument(
        "--repo-intent-md",
        type=Path,
        help="Path to human-owned intent doc (defaults to configs/repo_intent.md).",
    )
    reports_intent_snapshot_p.add_argument(
        "--readme-md",
        type=Path,
        help="Path to README (defaults to README.md at repo root).",
    )
    reports_intent_snapshot_p.add_argument(
        "--docs-dir",
        type=Path,
        help="Docs directory to index (defaults to repo_root/docs when present).",
    )
    reports_intent_snapshot_p.add_argument(
        "--max-readme-bytes",
        type=int,
        default=40_000,
        help="Maximum bytes to embed from README in the snapshot (excerpt).",
    )
    reports_intent_snapshot_p.add_argument(
        "--max-doc-bytes",
        type=int,
        default=8_000,
        help="Maximum bytes to read from each docs file when extracting headings.",
    )
    reports_intent_snapshot_p.add_argument(
        "--with-summary",
        action="store_true",
        help=(
            "Run an optional cached LLM summary pass using "
            "configs/backlog_prompts/intent_snapshot.md."
        ),
    )
    reports_intent_snapshot_p.add_argument(
        "--prompts-dir",
        type=Path,
        help="Optional prompt template directory (defaults to configs/backlog_prompts).",
    )
    reports_intent_snapshot_p.add_argument(
        "--agent",
        choices=["claude", "codex", "gemini"],
        default="claude",
        help="Agent CLI used for the optional summary pass (only when --with-summary is set).",
    )
    reports_intent_snapshot_p.add_argument(
        "--model",
        help="Optional model override for the optional summary pass.",
    )
    reports_intent_snapshot_p.add_argument(
        "--force",
        action="store_true",
        help="Rerun summary generation even when a cached output exists for the same prompt hash.",
    )
    reports_intent_snapshot_p.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="Reuse cached summary outputs when available (default).",
    )
    reports_intent_snapshot_p.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Disable cache reuse for the optional summary pass.",
    )
    reports_intent_snapshot_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Write prompt artifacts for the optional summary pass but do not call any agent.",
    )
    reports_intent_snapshot_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    reports_review_ux_p = reports_sub.add_parser(
        "review-ux",
        help=(
            "Run a UX/intent review stage over research_required backlog tickets "
            "(optional cached LLM pass)."
        ),
    )
    reports_review_ux_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (controls output directory scope).",
    )
    reports_review_ux_p.add_argument(
        "--repo-input",
        help="Optional repo_input label used for output naming (path or git URL).",
    )
    reports_review_ux_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_review_ux_p.add_argument(
        "--backlog-json",
        type=Path,
        help=(
            "Backlog JSON path (defaults to <compiled_dir>/<scope>.backlog.json). "
            "This must contain tickets with `stage` fields."
        ),
    )
    reports_review_ux_p.add_argument(
        "--intent-snapshot-json",
        type=Path,
        help="Intent snapshot JSON path (defaults to <compiled_dir>/<scope>.intent_snapshot.json).",
    )
    reports_review_ux_p.add_argument(
        "--allow-missing-intent-snapshot",
        action="store_true",
        help="Allow running without an intent snapshot (recorded loudly in output metadata).",
    )
    reports_review_ux_p.add_argument(
        "--repo-intent-md",
        type=Path,
        help="Path to human-owned intent doc (defaults to configs/repo_intent.md).",
    )
    reports_review_ux_p.add_argument(
        "--out-json",
        type=Path,
        help="Output UX review JSON path (defaults under the compiled directory).",
    )
    reports_review_ux_p.add_argument(
        "--out-md",
        type=Path,
        help="Output UX review markdown path (defaults next to --out-json with .md extension).",
    )
    reports_review_ux_p.add_argument(
        "--prompts-dir",
        type=Path,
        help="Optional prompt template directory (defaults to configs/backlog_prompts).",
    )
    reports_review_ux_p.add_argument(
        "--agent",
        choices=["claude", "codex", "gemini"],
        default="claude",
        help="Agent CLI used for the optional reviewer pass (skipped if cached or --dry-run).",
    )
    reports_review_ux_p.add_argument(
        "--model",
        help="Optional model override for the optional reviewer pass.",
    )
    reports_review_ux_p.add_argument(
        "--force",
        action="store_true",
        help="Rerun reviewer generation even when a cached output exists for the same prompt hash.",
    )
    reports_review_ux_p.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="Reuse cached reviewer outputs when available (default).",
    )
    reports_review_ux_p.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help="Disable cache reuse for the reviewer pass.",
    )
    reports_review_ux_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Write reviewer prompt artifacts but do not call any agent.",
    )
    reports_review_ux_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    reports_export_tickets_p = reports_sub.add_parser(
        "export-tickets",
        help=(
            "Export staged backlog items as external ticket templates "
            "(with stage gates + action ledger)."
        ),
    )
    reports_export_tickets_p.add_argument(
        "--target",
        help="Optional target slug under runs/usertest (controls output directory scope).",
    )
    reports_export_tickets_p.add_argument(
        "--repo-input",
        help="Optional repo_input label used for output naming (path or git URL).",
    )
    reports_export_tickets_p.add_argument(
        "--runs-dir",
        type=Path,
        help="Runs directory (defaults to <repo_root>/runs/usertest).",
    )
    reports_export_tickets_p.add_argument(
        "--backlog-json",
        type=Path,
        help="Backlog JSON path (defaults to <compiled_dir>/<scope>.backlog.json).",
    )
    reports_export_tickets_p.add_argument(
        "--actions-yaml",
        type=Path,
        help="Action ledger YAML path (defaults to configs/backlog_actions.yaml).",
    )
    reports_export_tickets_p.add_argument(
        "--atom-actions-yaml",
        type=Path,
        help=(
            "Atom lifecycle ledger YAML path (defaults to configs/backlog_atom_actions.yaml). "
            "Export updates referenced atoms to `queued`."
        ),
    )
    reports_export_tickets_p.add_argument(
        "--policy-config",
        type=Path,
        help=(
            "Backlog policy config YAML path (defaults to configs/backlog_policy.yaml when "
            "present). Used to gate high-surface user-visible changes to research/design export."
        ),
    )
    reports_export_tickets_p.add_argument(
        "--stage",
        action="append",
        default=[],
        help=(
            "Stage filter (repeatable). Defaults to exporting `triage`, "
            "`ready_for_ticket`, and `research_required` when omitted."
        ),
    )
    reports_export_tickets_p.add_argument(
        "--min-severity",
        choices=["low", "medium", "high", "blocker"],
        default="low",
        help="Minimum severity to export (default: low).",
    )
    reports_export_tickets_p.add_argument(
        "--include-actioned",
        action="store_true",
        help="Include tickets already present in the action ledger (default: skip).",
    )
    reports_export_tickets_p.add_argument(
        "--skip-plan-folder-dedupe",
        action="store_true",
        help=(
            "Skip de-duplicating exports by scanning existing `.agents/plans/*` ticket files for "
            "matching fingerprints (default: skip duplicates)."
        ),
    )
    reports_export_tickets_p.add_argument(
        "--out-json",
        type=Path,
        help="Output export JSON path (defaults under the compiled directory).",
    )
    reports_export_tickets_p.add_argument(
        "--out-md",
        type=Path,
        help="Output export markdown path (defaults next to --out-json with .md extension).",
    )
    reports_export_tickets_p.add_argument(
        "--repo-root",
        type=Path,
        help="Path to monorepo root (auto-detected by default).",
    )

    triage_prs_p = sub.add_parser(
        "triage-prs",
        help="Cluster existing pull requests from a JSON input artifact.",
    )
    triage_prs_p.add_argument(
        "--in",
        dest="input_json",
        type=Path,
        required=True,
        help="Path to PR JSON input (list or object containing pullRequests).",
    )
    triage_prs_p.add_argument(
        "--out-json",
        type=Path,
        help="Output JSON path (default: <input>.triage_prs.json).",
    )
    triage_prs_p.add_argument(
        "--out-md",
        type=Path,
        help="Output markdown path (default: <input>.triage_prs.md).",
    )
    triage_prs_p.add_argument(
        "--title-threshold",
        type=float,
        default=0.55,
        help="Title token Jaccard threshold for similarity edges.",
    )

    triage_backlog_p = sub.add_parser(
        "triage-backlog",
        help="Cluster issue-like backlog items by dedupe + functional theme similarity.",
    )
    triage_backlog_p.add_argument(
        "--in",
        dest="input_json",
        type=Path,
        required=True,
        help="Path to issue JSON input (list, or object with a `tickets` list).",
    )
    triage_backlog_p.add_argument(
        "--group-key",
        type=str,
        help="Optional field name used to compute cross-group coverage (defaults to `package`).",
    )
    triage_backlog_p.add_argument(
        "--out-json",
        type=Path,
        help="Output JSON path (default: <input>.triage_backlog.json).",
    )
    triage_backlog_p.add_argument(
        "--out-md",
        type=Path,
        help="Output markdown path (default: <input>.triage_backlog.md).",
    )
    triage_backlog_p.add_argument(
        "--out-xlsx",
        type=Path,
        help="Optional XLSX output path.",
    )
    triage_backlog_p.add_argument(
        "--dedupe-overall-threshold",
        type=float,
        default=0.90,
        help="Overall similarity threshold used for strict dedupe clustering.",
    )
    triage_backlog_p.add_argument(
        "--theme-overall-threshold",
        type=float,
        default=0.78,
        help="Overall similarity threshold used for theme clustering edges.",
    )
    triage_backlog_p.add_argument(
        "--theme-k",
        type=int,
        default=10,
        help="Top-K neighbor count per item in the theme graph.",
    )
    triage_backlog_p.add_argument(
        "--theme-representative-threshold",
        type=float,
        default=0.75,
        help="Minimum similarity to theme representative during refinement.",
    )

    return parser


def _resolve_repo_root(arg: Path | None) -> Path:
    """Resolve repo root from provided inputs.

    Parameters
    ----------
    arg:
        Input parameter.

    Returns
    -------
    Path
        Resolved filesystem path value.
    """
    if arg is not None:
        return arg.resolve()
    return find_repo_root()


def _resolve_optional_path(repo_root: Path, arg: Path | None) -> Path | None:
    """Resolve optional path from provided inputs.

    Parameters
    ----------
    repo_root:
        Repository root path.
    arg:
        Input parameter.

    Returns
    -------
    Path | None
        Resolved filesystem path value.
    """
    if arg is None:
        return None
    path = arg
    if not path.is_absolute() and not path.exists():
        path = repo_root / path
    return path.resolve()


def _coerce_string(value: Any) -> str | None:
    """Coerce input into string form.

    Parameters
    ----------
    value:
        Input value to normalize.

    Returns
    -------
    str | None
        Computed return value.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _coerce_string_list(value: Any) -> list[str]:
    """Coerce input into string list form.

    Parameters
    ----------
    value:
        Input value to normalize.

    Returns
    -------
    list[str]
        Normalized list result.
    """
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _ticket_owner_component(ticket: dict[str, Any]) -> str | None:
    """
    Return normalized owner/component label used for routing decisions.
    """

    owner = _coerce_string(ticket.get("suggested_owner")) or _coerce_string(ticket.get("component"))
    if owner is None:
        return None
    return owner.strip().lower()


def _severity_rank(value: str) -> int:
    """Handle severity rank processing.

    Parameters
    ----------
    value:
        Input value to normalize.

    Returns
    -------
    int
        Process exit code.
    """
    return _EXPORT_SEVERITY_ORDER.get(value, _EXPORT_SEVERITY_ORDER["medium"])


def _safe_relpath(path: Path, root: Path) -> str:
    """
    Return a stable forward-slash relative path for JSON artifacts.

    Parameters
    ----------
    path:
        Filesystem path to represent.
    root:
        Root directory to relativize against.

    Returns
    -------
    str
        Relative path (preferred) or a best-effort stringified path.
    """

    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except (OSError, RuntimeError, ValueError):
        return str(path).replace("\\", "/")


def _is_remote_repo_input(value: str) -> bool:
    """Return whether the value is remote repo input.

    Parameters
    ----------
    value:
        Input value to normalize.

    Returns
    -------
    bool
        Boolean decision result.
    """
    candidate = value.strip()
    if not candidate:
        return False
    if "://" in candidate:
        return True
    return candidate.startswith("git@")


def _resolve_local_repo_input_root(*, repo_input: str | None, repo_root: Path) -> Path | None:
    """
    Resolve a local filesystem repo_input to an existing directory, if possible.
    """

    if repo_input is None:
        return None
    if _is_remote_repo_input(repo_input):
        return None
    root_candidate = Path(repo_input)
    if not root_candidate.is_absolute():
        root_candidate = (repo_root / root_candidate).resolve()
    else:
        root_candidate = root_candidate.resolve()
    if not root_candidate.exists() or not root_candidate.is_dir():
        return None
    return root_candidate


def _resolve_owner_repo_root(
    *,
    ticket: dict[str, Any],
    scope_repo_input: str | None,
    cli_repo_input: str | None,
    repo_root: Path,
) -> tuple[Path, str | None, str]:
    """
    Resolve the owner repository root for a ticket export.

    Resolution precedence:
    1) Monorepo component owner (`runner_core`, `agent_adapters`, `sandbox_runner`)
       -> route to `--repo-root`.
    2) `ticket["repo_inputs_citing"]` (single unique entry)
    3) backlog scope repo_input
    4) CLI `--repo-input`
    5) `--repo-root` fallback (loud, explicit)
    """

    owner_component = _ticket_owner_component(ticket)
    if owner_component in _MONOREPO_OWNER_COMPONENTS:
        return repo_root, str(repo_root), f"suggested_owner:{owner_component}"

    ticket_repo_inputs = sorted(set(_coerce_string_list(ticket.get("repo_inputs_citing"))))
    source_label = "ticket_repo_inputs_citing"
    chosen: str | None = None

    if ticket_repo_inputs:
        if len(ticket_repo_inputs) > 1:
            # Some historical runs captured Windows paths with redundant separators
            # (e.g., `I:\\\\code\\\\...`) that show up as distinct strings. If all
            # candidates resolve to the same local dir, treat them as one owner.
            resolved_owner_keys: dict[str, str] = {}
            all_local = True
            for raw in ticket_repo_inputs:
                root = _resolve_local_repo_input_root(repo_input=raw, repo_root=repo_root)
                if root is None:
                    all_local = False
                    break
                try:
                    key = os.path.normcase(str(root.resolve()))
                except (OSError, RuntimeError):
                    key = os.path.normcase(str(root))
                resolved_owner_keys[key] = str(root)

            if all_local and len(resolved_owner_keys) == 1:
                chosen = next(iter(resolved_owner_keys.values()))
                source_label = "ticket_repo_inputs_citing_normalized"
            else:
                ticket_id = _coerce_string(ticket.get("ticket_id")) or "unknown"
                raise ValueError(
                    "Ticket has multiple owning repo candidates; "
                    "split backlog by repo_input first. "
                    f"ticket_id={ticket_id} repo_inputs={ticket_repo_inputs}"
                )
        if chosen is None:
            chosen = ticket_repo_inputs[0]
    elif scope_repo_input is not None:
        source_label = "backlog_scope_repo_input"
        chosen = scope_repo_input
    elif cli_repo_input is not None:
        source_label = "cli_repo_input"
        chosen = cli_repo_input

    if chosen is None:
        ticket_id = _coerce_string(ticket.get("ticket_id")) or "unknown"
        print(
            "WARNING: ticket has no repo_input context; "
            f"defaulting owner repo to --repo-root for ticket {ticket_id}.",
            file=sys.stderr,
        )
        return repo_root, None, "repo_root_fallback"

    if _is_remote_repo_input(chosen):
        ticket_id = _coerce_string(ticket.get("ticket_id")) or "unknown"
        raise ValueError(
            "Cannot write idea file for remote repo_input. "
            f"ticket_id={ticket_id} repo_input={chosen}"
        )

    root_candidate = Path(chosen)
    if not root_candidate.is_absolute():
        root_candidate = (repo_root / root_candidate).resolve()
    else:
        root_candidate = root_candidate.resolve()

    if not root_candidate.exists() or not root_candidate.is_dir():
        ticket_id = _coerce_string(ticket.get("ticket_id")) or "unknown"
        raise ValueError(
            "Owning repo path does not exist or is not a directory. "
            f"ticket_id={ticket_id} repo_input={chosen} resolved={root_candidate}"
        )
    return root_candidate, chosen, source_label


def _write_ticket_idea_file(
    *,
    ticket: dict[str, Any],
    issue_title: str,
    fingerprint: str,
    body_markdown: str,
    owner_repo_root: Path,
) -> Path:
    """
    Write a single exported ticket as an idea markdown file in owner repo plans.
    """

    stage = (_coerce_string(ticket.get("stage")) or "triage").strip().lower()
    queue_dir = _ticket_queue_dir_for_stage(owner_repo_root=owner_repo_root, stage=stage)
    queue_dir.mkdir(parents=True, exist_ok=True)

    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
    ticket_id = _coerce_string(ticket.get("ticket_id")) or "ticket"
    ticket_id_slug = slugify(ticket_id) or "ticket"
    title_slug = slugify(issue_title) or "untitled"
    filename = f"{date_tag}_{ticket_id_slug}_{fingerprint}_{title_slug[:64]}.md"

    lines: list[str] = []
    lines.append(f"# {issue_title}")
    lines.append("")
    lines.append(
        f"Generated by `python -m usertest_backlog.cli reports export-tickets` on "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}."
    )
    lines.append(f"- Fingerprint: `{fingerprint}`")
    lines.append(f"- Source ticket: `{ticket_id}`")
    lines.append("")
    lines.append(body_markdown.rstrip())
    lines.append("")

    out_path = queue_dir / filename
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def _ticket_queue_dir_for_stage(*, owner_repo_root: Path, stage: str) -> Path:
    """Return ticket queue dir for stage data.

    Parameters
    ----------
    owner_repo_root:
        Root directory path.
    stage:
        Input parameter.

    Returns
    -------
    Path
        Resolved filesystem path value.
    """
    normalized = stage.strip().lower()
    if normalized == "triage":
        return owner_repo_root / ".agents" / "plans" / "0.5 - to_triage"
    return owner_repo_root / ".agents" / "plans" / "1 - ideas"


def _ticket_queue_dirs(owner_repo_root: Path) -> list[Path]:
    """Return ticket queue dirs data.

    Parameters
    ----------
    owner_repo_root:
        Root directory path.

    Returns
    -------
    list[Path]
        Normalized list result.
    """
    return [
        owner_repo_root / ".agents" / "plans" / "1 - ideas",
        owner_repo_root / ".agents" / "plans" / "0.5 - to_triage",
        # Legacy triage queue path retained for stale-file cleanup compatibility.
        owner_repo_root / ".agents" / "plans" / "1.5 - to_plan",
    ]


def _cleanup_stale_ticket_idea_files(
    *,
    ticket: dict[str, Any],
    fingerprint: str,
    owner_repo_root: Path,
    repo_root: Path,
    scope_repo_input: str | None,
    cli_repo_input: str | None,
    keep_path: Path | None = None,
) -> None:
    """
    Remove stale duplicate idea files for this ticket fingerprint from non-owner queues.
    """

    ticket_id = _coerce_string(ticket.get("ticket_id"))
    if ticket_id is None:
        return
    ticket_id_slug = slugify(ticket_id)
    if not ticket_id_slug:
        return
    pattern = f"*_{ticket_id_slug}_{fingerprint}_*.md"

    candidate_roots: set[Path] = {repo_root.resolve()}
    for candidate in (
        _resolve_local_repo_input_root(repo_input=scope_repo_input, repo_root=repo_root),
        _resolve_local_repo_input_root(repo_input=cli_repo_input, repo_root=repo_root),
    ):
        if candidate is not None:
            candidate_roots.add(candidate.resolve())

    for repo_input in _coerce_string_list(ticket.get("repo_inputs_citing")):
        candidate = _resolve_local_repo_input_root(repo_input=repo_input, repo_root=repo_root)
        if candidate is not None:
            candidate_roots.add(candidate.resolve())

    keep_path_resolved = keep_path.resolve() if keep_path is not None else None
    for candidate_root in candidate_roots:
        for queue_dir in _ticket_queue_dirs(candidate_root):
            if not queue_dir.exists() or not queue_dir.is_dir():
                continue
            for stale in queue_dir.glob(pattern):
                stale_resolved = stale.resolve()
                if keep_path_resolved is not None and stale_resolved == keep_path_resolved:
                    continue
                stale.unlink(missing_ok=True)


def _read_text_excerpt(path: Path, *, max_bytes: int) -> str:
    """
    Read up to `max_bytes` bytes from a UTF-8-ish text file and return a decoded excerpt.

    Parameters
    ----------
    path:
        File path to read.
    max_bytes:
        Maximum number of bytes to read.

    Returns
    -------
    str
        Decoded excerpt (may be truncated).

    Raises
    ------
    FileNotFoundError
        If `path` does not exist.
    OSError
        If reading fails.
    """

    max_bytes = max(1, int(max_bytes))
    with path.open("rb") as handle:
        data = handle.read(max_bytes)
    return data.decode("utf-8", errors="replace")


def _extract_markdown_title(text: str) -> str | None:
    """Extract markdown title from input content.

    Parameters
    ----------
    text:
        Input text payload.

    Returns
    -------
    str | None
        Computed return value.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            return title if title else None
    return None


def _index_docs(*, repo_root: Path, docs_dir: Path, max_doc_bytes: int) -> list[dict[str, Any]]:
    """
    Create a lightweight index of markdown files under `docs_dir`.

    Parameters
    ----------
    repo_root:
        Monorepo root directory.
    docs_dir:
        Directory containing docs (often `<repo_root>/docs`).
    max_doc_bytes:
        Maximum bytes to read from each file when extracting a title.

    Returns
    -------
    list[dict[str, Any]]
        List of docs entries with `path`, `size_bytes`, and `title` when available.
    """

    if not docs_dir.exists() or not docs_dir.is_dir():
        return []

    entries: list[dict[str, Any]] = []
    try:
        paths = sorted(p for p in docs_dir.rglob("*.md") if p.is_file())
    except OSError:
        return []

    for path in paths:
        try:
            size_bytes = int(path.stat().st_size)
        except OSError:
            continue
        try:
            excerpt = _read_text_excerpt(path, max_bytes=max_doc_bytes)
        except OSError:
            excerpt = ""
        title = _extract_markdown_title(excerpt) if excerpt else None
        entries.append(
            {
                "path": _safe_relpath(path, repo_root),
                "size_bytes": size_bytes,
                "title": title,
            }
        )
    return entries


def _parser_option_strings(parser: argparse.ArgumentParser) -> list[str]:
    """
    Extract a sorted list of option strings (flags) from an argparse parser.

    Parameters
    ----------
    parser:
        The parser to introspect.

    Returns
    -------
    list[str]
        Sorted unique option strings for this command parser.
    """

    options: set[str] = set()
    for action in getattr(parser, "_actions", []):
        option_strings = getattr(action, "option_strings", None)
        if not isinstance(option_strings, list):
            continue
        for opt in option_strings:
            if isinstance(opt, str) and opt.startswith("-") and opt not in {"-h", "--help"}:
                options.add(opt)
    return sorted(options)


def _extract_cli_commands(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    """
    Machine-extract the CLI command surface from an argparse parser tree.

    Parameters
    ----------
    parser:
        Root argparse parser (typically from `build_parser()`).

    Returns
    -------
    list[dict[str, Any]]
        Command entries, including intermediate groups and leaf commands.
    """

    prog = _coerce_string(getattr(parser, "prog", None)) or "usertest"
    commands: list[dict[str, Any]] = []

    def _walk(current: argparse.ArgumentParser, words: list[str], help_text: str | None) -> None:
        """Walk parser/action trees and collect lint findings.

        Parameters
        ----------
        current:
            Current parser/action node.
        words:
            Collected name words.
        help_text:
            Help text string for parser action.

        Returns
        -------
        None
            None.
        """
        sub_actions = [
            action
            for action in getattr(current, "_actions", [])
            if isinstance(action, argparse._SubParsersAction)
        ]
        has_subcommands = bool(sub_actions)

        if words:
            commands.append(
                {
                    "command": " ".join([prog, *words]),
                    "help": help_text,
                    "is_group": has_subcommands,
                    "options": _parser_option_strings(current),
                }
            )

        for sub_action in sub_actions:
            for name, subparser in sorted(sub_action.choices.items(), key=lambda kv: kv[0]):
                if not isinstance(name, str) or not isinstance(subparser, argparse.ArgumentParser):
                    continue
                sub_help = _coerce_string(getattr(subparser, "description", None))
                _walk(subparser, [*words, name], sub_help)

    _walk(parser, [], None)
    return commands


def _render_template(template: str, replacements: dict[str, str]) -> str:
    """Render template output text.

    Parameters
    ----------
    template:
        Template text input.
    replacements:
        Template replacement mapping.

    Returns
    -------
    str
        Normalized string result.
    """
    out = template
    for key, value in replacements.items():
        out = out.replace("{{" + key + "}}", value)
    return out


def _parse_first_json_object(raw_text: str) -> dict[str, Any] | None:
    """Parse first json object from input text.

    Parameters
    ----------
    raw_text:
        Raw text payload.

    Returns
    -------
    dict[str, Any] | None
        Structured mapping result.
    """
    text = raw_text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate
    return None


def _ticket_export_anchors(ticket: dict[str, Any]) -> set[str]:
    """Return ticket export anchors data.

    Parameters
    ----------
    ticket:
        Ticket payload mapping.

    Returns
    -------
    set[str]
        Computed return value.
    """
    chunks: list[str] = []
    for key in ("title", "problem", "user_impact", "proposed_fix"):
        value = _coerce_string(ticket.get(key))
        if value:
            chunks.append(value)
    for item in _coerce_string_list(ticket.get("investigation_steps")):
        chunks.append(item)

    anchors: set[str] = set()
    for chunk in chunks:
        for match in _EXPORT_PATH_LIKE_RE.findall(chunk):
            anchors.add(match.lower().replace("\\", "/"))
    return anchors


def _ticket_export_fingerprint(ticket: dict[str, Any]) -> str:
    """
    Compute a stable fingerprint for action-ledger tracking.

    The fingerprint is intentionally derived from normalized title tokens and "evidence anchors"
    (path-like strings found in ticket text), plus stable structured labels such as
    `change_surface.kinds` and `suggested_owner` / `component` when present.

    Parameters
    ----------
    ticket:
        Backlog ticket object.

    Returns
    -------
    str
        A short hex fingerprint suitable for use as a key in `configs/backlog_actions.yaml`.
    """

    title = _coerce_string(ticket.get("title")) or ""
    title_tokens = sorted(set(_EXPORT_TOKEN_RE.findall(title.lower())))
    anchors = sorted(_ticket_export_anchors(ticket))

    change_surface_raw = ticket.get("change_surface")
    change_surface = change_surface_raw if isinstance(change_surface_raw, dict) else {}
    kinds = sorted(set(_coerce_string_list(change_surface.get("kinds"))))

    owner = (
        _coerce_string(ticket.get("suggested_owner"))
        or _coerce_string(ticket.get("component"))
        or "unknown"
    )

    payload = {
        "title_tokens": title_tokens[:24],
        "anchors": anchors[:24],
        "kinds": kinds[:24],
        "owner": owner,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return sha256(blob).hexdigest()[:16]
def _summarize_atoms_for_totals(atoms: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize atoms for totals into aggregate counters.

    Parameters
    ----------
    atoms:
        Backlog atom payload list.

    Returns
    -------
    dict[str, Any]
        Structured mapping result.
    """
    source_counts: dict[str, int] = {}
    severity_hint_counts: dict[str, int] = {}
    runs: set[str] = set()
    for atom in atoms:
        run_rel = _coerce_string(atom.get("run_rel"))
        if run_rel is not None and not run_rel.startswith("__aggregate__/"):
            runs.add(run_rel)
        source = _coerce_string(atom.get("source"))
        if source is not None:
            source_counts[source] = source_counts.get(source, 0) + 1
        severity = _coerce_string(atom.get("severity_hint")) or "medium"
        severity_hint_counts[severity] = severity_hint_counts.get(severity, 0) + 1
    return {
        "runs": len(runs),
        "atoms": len(atoms),
        "source_counts": source_counts,
        "severity_hint_counts": severity_hint_counts,
    }
def _backfill_failure_event_atoms_from_legacy_entries(
    *,
    atom_actions: dict[str, dict[str, Any]],
    generated_at: str,
) -> dict[str, Any]:
    """
    Ensure canonical `:run_failure_event:1` atoms exist and inherit lifecycle state.

    This is intentionally idempotent and only promotes (never demotes).
    """

    mapped = 0
    created = 0
    promoted = 0

    # Iterate over a snapshot since we may insert new canonical keys.
    for legacy_atom_id, legacy_entry in list(atom_actions.items()):
        canonical = _canonicalize_failure_atom_id(legacy_atom_id)
        if canonical is None or canonical == legacy_atom_id:
            continue
        mapped += 1

        legacy_status = _normalize_atom_status(_coerce_string(legacy_entry.get("status")))

        existing = atom_actions.get(canonical)
        if existing is None:
            existing = {
                "atom_id": canonical,
                "status": legacy_status,
                "first_seen_at": _coerce_string(legacy_entry.get("first_seen_at")) or generated_at,
            }
            atom_actions[canonical] = existing
            created += 1

        old_status = _normalize_atom_status(_coerce_string(existing.get("status")))
        new_status = _promote_atom_status(old_status, legacy_status)
        if _ATOM_STATUS_ORDER[new_status] > _ATOM_STATUS_ORDER[old_status]:
            promoted += 1
        existing["status"] = new_status
        existing["last_seen_at"] = generated_at

        for list_key in ("ticket_ids", "queue_paths", "queue_owner_roots", "fingerprints"):
            values: list[str] = []
            values.extend([item for item in existing.get(list_key, []) if isinstance(item, str)])
            values.extend(
                [item for item in legacy_entry.get(list_key, []) if isinstance(item, str)]
            )
            existing[list_key] = _sorted_unique_strings(values)

        derived = [
            item for item in existing.get("derived_from_atom_ids", []) if isinstance(item, str)
        ]
        derived.append(legacy_atom_id)
        existing["derived_from_atom_ids"] = _sorted_unique_strings(derived)

        atom_actions[canonical] = existing

    return {
        "legacy_atoms_mapped": mapped,
        "canonical_atoms_created": created,
        "canonical_atoms_promoted": promoted,
    }


def _update_atom_actions_from_backlog(
    *,
    atom_actions: dict[str, dict[str, Any]],
    atoms: list[dict[str, Any]],
    tickets: list[dict[str, Any]],
    generated_at: str,
    backlog_json_path: Path,
) -> dict[str, Any]:
    """
    Update atom lifecycle status during backlog generation.

    - atom in non-blocked ticket evidence -> at least `ticketed`
    - atom not cited by ticket evidence -> at least `new`
    """

    ticket_ids_by_atom: dict[str, set[str]] = {}
    for ticket in tickets:
        stage = (_coerce_string(ticket.get("stage")) or "triage").strip().lower()
        if stage == "blocked":
            # Blocked tickets are intentionally not treated as "ticket outcomes" for the
            # atom ledger so evidence can accumulate across runs/models and be re-mined.
            continue
        ticket_id = f"TKT-{ticket_export_fingerprint(ticket)}"
        for atom_id in _coerce_string_list(ticket.get("evidence_atom_ids")):
            bucket = ticket_ids_by_atom.setdefault(atom_id, set())
            bucket.add(ticket_id)

    created = 0
    promoted = 0
    observed = 0
    ticketed_now = 0
    new_now = 0

    for atom in atoms:
        atom_id = _coerce_string(atom.get("atom_id"))
        if atom_id is None:
            continue
        if atom_id.startswith("__aggregate__/"):
            # Synthetic aggregates are regenerated every time and should not be tracked
            # in the lifecycle ledger.
            continue
        observed += 1
        desired = "ticketed" if atom_id in ticket_ids_by_atom else "new"
        if desired == "ticketed":
            ticketed_now += 1
        else:
            new_now += 1

        existing = atom_actions.get(atom_id)
        if existing is None:
            existing = {"atom_id": atom_id, "status": desired, "first_seen_at": generated_at}
            atom_actions[atom_id] = existing
            created += 1
        old_status = _normalize_atom_status(_coerce_string(existing.get("status")))
        new_status = _promote_atom_status(old_status, desired)
        if _ATOM_STATUS_ORDER[new_status] > _ATOM_STATUS_ORDER[old_status]:
            promoted += 1
        existing["status"] = new_status
        existing["last_backlog_status"] = desired
        existing["last_seen_at"] = generated_at
        existing["last_backlog_generated_at"] = generated_at
        existing["last_backlog_json"] = str(backlog_json_path)
        existing["source"] = _coerce_string(atom.get("source")) or existing.get("source")
        existing["severity_hint"] = _coerce_string(atom.get("severity_hint")) or existing.get(
            "severity_hint"
        )
        existing["run_rel"] = _coerce_string(atom.get("run_rel")) or existing.get("run_rel")
        existing["agent"] = _coerce_string(atom.get("agent")) or existing.get("agent")
        existing["mission_id"] = _coerce_string(atom.get("mission_id")) or existing.get(
            "mission_id"
        )
        existing["persona_id"] = _coerce_string(atom.get("persona_id")) or existing.get(
            "persona_id"
        )
        existing["target_slug"] = _coerce_string(atom.get("target_slug")) or existing.get(
            "target_slug"
        )
        existing["repo_input"] = _coerce_string(atom.get("repo_input")) or existing.get(
            "repo_input"
        )
        ticket_ids_existing = [
            item for item in existing.get("ticket_ids", []) if isinstance(item, str)
        ]
        if atom_id in ticket_ids_by_atom:
            ticket_ids_existing.extend(sorted(ticket_ids_by_atom[atom_id]))
        existing["ticket_ids"] = _sorted_unique_strings(ticket_ids_existing)
        atom_actions[atom_id] = existing

    status_counts: dict[str, int] = {}
    for entry in atom_actions.values():
        status = _normalize_atom_status(_coerce_string(entry.get("status")))
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "observed_atoms": observed,
        "current_new_atoms": new_now,
        "current_ticketed_atoms": ticketed_now,
        "created_entries": created,
        "promoted_entries": promoted,
        "ledger_atoms_total": len(atom_actions),
        "status_counts": status_counts,
    }


def _update_atom_actions_from_exports(
    *,
    atom_actions: dict[str, dict[str, Any]],
    queued_refs: list[dict[str, str]],
    generated_at: str,
    export_json_path: Path,
) -> dict[str, Any]:
    """
    Update atom lifecycle status during ticket export.

    - atom referenced by an exported ticket -> at least `queued`
    - atom referenced by a deduped existing plan ticket -> `queued` or `actioned` (from plan bucket)
    """

    touched_atoms: set[str] = set()
    promoted = 0
    created = 0

    for ref in queued_refs:
        atom_id_raw = _coerce_string(ref.get("atom_id"))
        if atom_id_raw is None:
            continue
        if atom_id_raw.startswith("__aggregate__/"):
            continue
        derived_from_atom_id: str | None = None
        atom_id = atom_id_raw
        canonical_atom_id = _canonicalize_failure_atom_id(atom_id_raw)
        if canonical_atom_id is not None and canonical_atom_id != atom_id_raw:
            derived_from_atom_id = atom_id_raw
            atom_id = canonical_atom_id
        desired_status = _normalize_atom_status(_coerce_string(ref.get("desired_status")))
        if desired_status not in ("queued", "actioned"):
            desired_status = "queued"
        touched_atoms.add(atom_id)
        existing = atom_actions.get(atom_id)
        if existing is None:
            existing = {"atom_id": atom_id, "status": desired_status, "first_seen_at": generated_at}
            atom_actions[atom_id] = existing
            created += 1

        old_status = _normalize_atom_status(_coerce_string(existing.get("status")))
        new_status = _promote_atom_status(old_status, desired_status)
        if _ATOM_STATUS_ORDER[new_status] > _ATOM_STATUS_ORDER[old_status]:
            promoted += 1
        existing["status"] = new_status
        existing["last_queue_status"] = desired_status
        existing["last_seen_at"] = generated_at
        existing["last_queue_at"] = generated_at
        existing["last_export_json"] = str(export_json_path)

        ticket_ids = [item for item in existing.get("ticket_ids", []) if isinstance(item, str)]
        ticket_id = _coerce_string(ref.get("ticket_id"))
        if ticket_id is not None:
            ticket_ids.append(ticket_id)
        existing["ticket_ids"] = _sorted_unique_strings(ticket_ids)

        queue_paths = [item for item in existing.get("queue_paths", []) if isinstance(item, str)]
        idea_path = _coerce_string(ref.get("idea_path"))
        if idea_path is not None:
            queue_paths.append(idea_path)
        existing["queue_paths"] = _sorted_unique_strings(queue_paths)

        queue_roots = [
            item for item in existing.get("queue_owner_roots", []) if isinstance(item, str)
        ]
        owner_root = _coerce_string(ref.get("owner_root"))
        if owner_root is not None:
            queue_roots.append(owner_root)
        existing["queue_owner_roots"] = _sorted_unique_strings(queue_roots)

        fingerprints = [item for item in existing.get("fingerprints", []) if isinstance(item, str)]
        fingerprint = _coerce_string(ref.get("fingerprint"))
        if fingerprint is not None:
            fingerprints.append(fingerprint)
        existing["fingerprints"] = _sorted_unique_strings(fingerprints)

        if derived_from_atom_id is not None:
            derived = [
                item for item in existing.get("derived_from_atom_ids", []) if isinstance(item, str)
            ]
            derived.append(derived_from_atom_id)
            existing["derived_from_atom_ids"] = _sorted_unique_strings(derived)

        atom_actions[atom_id] = existing

    status_counts: dict[str, int] = {}
    for entry in atom_actions.values():
        status = _normalize_atom_status(_coerce_string(entry.get("status")))
        status_counts[status] = status_counts.get(status, 0) + 1

    return {
        "queued_atoms_touched": len(touched_atoms),
        "created_entries": created,
        "promoted_entries": promoted,
        "ledger_atoms_total": len(atom_actions),
        "status_counts": status_counts,
    }


def _render_intent_snapshot_markdown(snapshot: dict[str, Any]) -> str:
    """
    Render a human-readable markdown view of an intent snapshot JSON.

    Parameters
    ----------
    snapshot:
        Snapshot object as written to `.intent_snapshot.json`.

    Returns
    -------
    str
        Markdown content.
    """

    generated_at = _coerce_string(snapshot.get("generated_at")) or "unknown"
    scope = snapshot.get("scope") if isinstance(snapshot.get("scope"), dict) else {}
    target = _coerce_string(scope.get("target")) or "all"
    repo_input = _coerce_string(scope.get("repo_input"))

    lines: list[str] = []
    lines.append("# Repo Intent Snapshot")
    lines.append("")
    lines.append(f"- Generated at: `{generated_at}`")
    lines.append(f"- Scope target: `{target}`")
    if repo_input is not None:
        lines.append(f"- Scope repo_input: `{repo_input}`")
    lines.append("")

    repo_intent = _coerce_string(snapshot.get("repo_intent_excerpt"))
    if repo_intent:
        lines.append("## Human-Owned Intent (excerpt)")
        lines.append("")
        lines.append(repo_intent.strip())
        lines.append("")

    lines.append("## Command Surface")
    lines.append("")
    cmds = snapshot.get("commands")
    cmd_list = [item for item in cmds if isinstance(item, dict)] if isinstance(cmds, list) else []
    if not cmd_list:
        lines.append("- (no commands extracted)")
        lines.append("")
    else:
        for cmd in cmd_list[:120]:
            command = _coerce_string(cmd.get("command")) or "unknown"
            help_text = _coerce_string(cmd.get("help")) or ""
            suffix = f": {help_text}" if help_text else ""
            lines.append(f"- `{command}`{suffix}")
        lines.append("")

    lines.append("## Docs Index")
    lines.append("")
    docs = snapshot.get("docs_index")
    docs_list = [item for item in docs if isinstance(item, dict)] if isinstance(docs, list) else []
    if not docs_list:
        lines.append("- (no docs indexed)")
        lines.append("")
    else:
        for item in docs_list[:120]:
            path = _coerce_string(item.get("path")) or "unknown"
            title = _coerce_string(item.get("title"))
            if title:
                lines.append(f"- `{path}`: {title}")
            else:
                lines.append(f"- `{path}`")
        lines.append("")

    llm_meta = snapshot.get("llm_summary_meta")
    if isinstance(llm_meta, dict):
        status = _coerce_string(llm_meta.get("status")) or "unknown"
        lines.append("## Optional Summary Pass")
        lines.append("")
        lines.append(f"- Status: `{status}`")
        prompt_hash = _coerce_string(llm_meta.get("prompt_hash"))
        if prompt_hash:
            lines.append(f"- Prompt hash: `{prompt_hash}`")
        agent = _coerce_string(llm_meta.get("agent"))
        if agent:
            lines.append(f"- Agent: `{agent}`")
        model = _coerce_string(llm_meta.get("model"))
        if model:
            lines.append(f"- Model: `{model}`")
        lines.append("")

    return "\n".join(lines) + "\n"


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


def _cmd_reports_compile(args: argparse.Namespace) -> int:
    """Execute the `reports compile` command handler.

    Parameters
    ----------
    args:
        Parsed command-line arguments namespace.

    Returns
    -------
    int
        Process exit code.
    """
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
    """Execute the `reports analyze` command handler.

    Parameters
    ----------
    args:
        Parsed command-line arguments namespace.

    Returns
    -------
    int
        Process exit code.
    """
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


def _cmd_reports_intent_snapshot(args: argparse.Namespace) -> int:
    """Execute the `reports intent snapshot` command handler.

    Parameters
    ----------
    args:
        Parsed command-line arguments namespace.

    Returns
    -------
    int
        Process exit code.
    """
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

    default_name = slugify(repo_input) if repo_input is not None else (target_slug or "all")

    if args.out_json is not None:
        out_json = _resolve_optional_path(repo_root, args.out_json) or args.out_json.resolve()
    else:
        if target_slug is not None:
            out_json = runs_dir / target_slug / "_compiled" / f"{default_name}.intent_snapshot.json"
        else:
            out_json = runs_dir / "_compiled" / f"{default_name}.intent_snapshot.json"

    if args.out_md is not None:
        out_md = _resolve_optional_path(repo_root, args.out_md) or args.out_md.resolve()
    else:
        out_md = out_json.with_suffix(".md")

    repo_intent_arg: Path | None = args.repo_intent_md
    if repo_intent_arg is not None:
        repo_intent_path = (
            _resolve_optional_path(repo_root, repo_intent_arg) or repo_intent_arg.resolve()
        )
    else:
        repo_intent_path = repo_root / "configs" / "repo_intent.md"
    if not repo_intent_path.exists():
        print(f"Missing repo intent doc: {repo_intent_path}", file=sys.stderr)
        return 2

    readme_arg: Path | None = args.readme_md
    if readme_arg is not None:
        readme_path = _resolve_optional_path(repo_root, readme_arg) or readme_arg.resolve()
    else:
        readme_path = repo_root / "README.md"
    if not readme_path.exists():
        print(f"Missing README: {readme_path}", file=sys.stderr)
        return 2

    docs_dir_arg: Path | None = args.docs_dir
    if docs_dir_arg is not None:
        docs_dir = _resolve_optional_path(repo_root, docs_dir_arg) or docs_dir_arg.resolve()
    else:
        docs_dir = repo_root / "docs"

    max_readme_bytes = max(1, int(args.max_readme_bytes))
    max_doc_bytes = max(1, int(args.max_doc_bytes))

    try:
        repo_intent_excerpt = repo_intent_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"Failed reading repo intent doc: {repo_intent_path}: {e}", file=sys.stderr)
        return 2

    try:
        readme_excerpt = _read_text_excerpt(readme_path, max_bytes=max_readme_bytes)
    except OSError as e:
        print(f"Failed reading README: {readme_path}: {e}", file=sys.stderr)
        return 2

    commands = _extract_cli_commands(build_parser())
    docs_index = _index_docs(repo_root=repo_root, docs_dir=docs_dir, max_doc_bytes=max_doc_bytes)

    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": {
            "target": target_slug,
            "repo_input": repo_input,
        },
        "inputs": {
            "repo_intent_path": _safe_relpath(repo_intent_path, repo_root),
            "readme_path": _safe_relpath(readme_path, repo_root),
            "docs_dir": _safe_relpath(docs_dir, repo_root),
        },
        "repo_intent_excerpt": repo_intent_excerpt,
        "readme_excerpt": readme_excerpt,
        "docs_index": docs_index,
        "commands": commands,
        "llm_summary": None,
        "llm_summary_meta": {"status": "not_requested"},
    }

    prompts_dir_arg: Path | None = args.prompts_dir
    if prompts_dir_arg is not None:
        prompts_dir = (
            _resolve_optional_path(repo_root, prompts_dir_arg) or prompts_dir_arg.resolve()
        )
    else:
        prompts_dir = repo_root / "configs" / "backlog_prompts"

    with_summary = bool(args.with_summary)
    resume = bool(args.resume)
    force = bool(args.force)
    dry_run = bool(args.dry_run)
    agent = str(args.agent)
    model = str(args.model) if isinstance(args.model, str) and args.model.strip() else None

    if with_summary:
        template_path = prompts_dir / "intent_snapshot.md"
        if not template_path.exists():
            print(f"Missing intent snapshot prompt template: {template_path}", file=sys.stderr)
            return 2

        template = template_path.read_text(encoding="utf-8")
        prompt = _render_template(
            template,
            {
                "REPO_INTENT_MD": repo_intent_excerpt,
                "README_MD": readme_excerpt,
                "DOCS_INDEX_JSON": json.dumps(docs_index, indent=2, ensure_ascii=False),
                "COMMANDS_JSON": json.dumps(commands, indent=2, ensure_ascii=False),
            },
        )

        prompt_hash = sha256(prompt.encode("utf-8")).hexdigest()[:16]
        artifacts_dir = out_json.parent / f"{default_name}.intent_snapshot_artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        tag = f"intent_snapshot_{prompt_hash}"
        cached_path = artifacts_dir / f"{tag}.summary.json"

        summary_obj: dict[str, Any] | None = None
        status = "ok"
        used_cached = False

        if resume and not force and cached_path.exists():
            try:
                cached = json.loads(cached_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                warnings.warn(
                    f"Failed to parse cached intent summary at {cached_path}: {e}; rerunning summary.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                cached = None
            except OSError as e:
                warnings.warn(
                    f"Failed reading cached intent summary at {cached_path}: {e}; rerunning summary.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                cached = None
            if isinstance(cached, dict):
                summary_obj = cached
                status = "cached"
                used_cached = True
            elif cached is not None:
                warnings.warn(
                    "Ignoring cached intent summary with unexpected payload type "
                    f"{type(cached).__name__} at {cached_path}; expected object.",
                    RuntimeWarning,
                    stacklevel=2,
                )

        if summary_obj is None:
            if dry_run:
                (artifacts_dir / f"{tag}.dry_run.prompt.txt").write_text(prompt, encoding="utf-8")
                status = "dry_run"
            else:
                raw_text = run_backlog_prompt(
                    agent=agent,
                    prompt=prompt,
                    out_dir=artifacts_dir,
                    tag=tag,
                    model=model,
                    cfg=cfg,
                )
                parsed = _parse_first_json_object(raw_text)
                if not isinstance(parsed, dict):
                    (artifacts_dir / f"{tag}.parse_error.txt").write_text(
                        raw_text.strip() + "\n",
                        encoding="utf-8",
                    )
                    print(
                        "Failed to parse JSON from summary output "
                        f"(see artifacts under {artifacts_dir})",
                        file=sys.stderr,
                    )
                    return 2
                summary_obj = parsed
                cached_path.write_text(
                    json.dumps(summary_obj, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )

        snapshot["llm_summary"] = summary_obj
        snapshot["llm_summary_meta"] = {
            "status": status,
            "prompt_hash": prompt_hash,
            "agent": agent,
            "model": model,
            "cached": used_cached,
            "template_path": _safe_relpath(template_path, repo_root),
        }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out_md.write_text(_render_intent_snapshot_markdown(snapshot), encoding="utf-8")

    print(str(out_json))
    print(str(out_md))
    if not with_summary:
        print(
            "Summary pass not requested (use --with-summary to generate an optional cached "
            "LLM summary)."
        )
    else:
        meta = snapshot.get("llm_summary_meta")
        status = meta.get("status") if isinstance(meta, dict) else None
        print(f"Summary status: {status}")
    return 0


def _render_ux_review_markdown(doc: dict[str, Any]) -> str:
    """
    Render a human-readable markdown view of a UX review JSON artifact.

    Parameters
    ----------
    doc:
        UX review document as written to `.ux_review.json`.

    Returns
    -------
    str
        Markdown content.
    """

    generated_at = _coerce_string(doc.get("generated_at")) or "unknown"
    status = _coerce_string(doc.get("status")) or "unknown"
    scope = doc.get("scope") if isinstance(doc.get("scope"), dict) else {}
    target = _coerce_string(scope.get("target")) or "all"
    repo_input = _coerce_string(scope.get("repo_input"))

    lines: list[str] = []
    lines.append("# UX / Intent Review")
    lines.append("")
    lines.append(f"- Generated at: `{generated_at}`")
    lines.append(f"- Status: `{status}`")
    lines.append(f"- Scope target: `{target}`")
    if repo_input is not None:
        lines.append(f"- Scope repo_input: `{repo_input}`")
    lines.append("")

    review = doc.get("review")
    review_obj = review if isinstance(review, dict) else None
    if review_obj is None:
        lines.append("## Output")
        lines.append("")
        lines.append("- No reviewer output was generated.")
        artifacts_dir = _coerce_string(doc.get("artifacts_dir"))
        if artifacts_dir:
            lines.append(f"- Artifacts dir: `{artifacts_dir}`")
        lines.append("")
        return "\n".join(lines) + "\n"

    budget = review_obj.get("command_surface_budget")
    if isinstance(budget, dict):
        max_new = budget.get("max_new_commands_per_quarter")
        notes = _coerce_string(budget.get("notes")) or ""
        lines.append("## Command Surface Budget")
        lines.append("")
        if isinstance(max_new, int):
            lines.append(f"- Max new commands/quarter: `{max_new}`")
        elif isinstance(max_new, (float, str)):
            lines.append(f"- Max new commands/quarter: `{max_new}`")
        if notes:
            lines.append(f"- Notes: {notes}")
        lines.append("")

    recs = review_obj.get("recommendations")
    rec_list = [item for item in recs if isinstance(item, dict)] if isinstance(recs, list) else []
    lines.append("## Recommendations")
    lines.append("")
    if not rec_list:
        lines.append("- (no recommendations)")
        lines.append("")
    else:
        for rec in rec_list[:80]:
            rec_id = _coerce_string(rec.get("recommendation_id")) or "UX-???"
            approach = _coerce_string(rec.get("recommended_approach")) or "unknown"
            ticket_ids = rec.get("ticket_ids")
            tickets_s = (
                ", ".join([tid for tid in ticket_ids if isinstance(tid, str) and tid.strip()])
                if isinstance(ticket_ids, list)
                else ""
            )
            title_bits = f" ({tickets_s})" if tickets_s else ""
            lines.append(f"### {rec_id}: {approach}{title_bits}")
            rationale = _coerce_string(rec.get("rationale"))
            if rationale:
                lines.append("")
                lines.append(rationale.strip())
                lines.append("")
            next_steps = rec.get("next_steps")
            if isinstance(next_steps, list):
                steps = [s for s in next_steps if isinstance(s, str) and s.strip()]
                if steps:
                    lines.append("- Next steps:")
                    for step in steps[:10]:
                        lines.append(f"  - {step}")
                    lines.append("")

        lines.append("")

    notes = _coerce_string(review_obj.get("notes"))
    if notes:
        lines.append("## Notes")
        lines.append("")
        lines.append(notes.strip())
        lines.append("")

    return "\n".join(lines) + "\n"


def _cmd_reports_review_ux(args: argparse.Namespace) -> int:
    """Execute the `reports review ux` command handler.

    Parameters
    ----------
    args:
        Parsed command-line arguments namespace.

    Returns
    -------
    int
        Process exit code.
    """
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

    default_name = slugify(repo_input) if repo_input is not None else (target_slug or "all")
    if target_slug is not None:
        compiled_dir = runs_dir / target_slug / "_compiled"
    else:
        compiled_dir = runs_dir / "_compiled"

    backlog_arg: Path | None = args.backlog_json
    if backlog_arg is not None:
        backlog_path = _resolve_optional_path(repo_root, backlog_arg) or backlog_arg.resolve()
    else:
        backlog_path = compiled_dir / f"{default_name}.backlog.json"
    if not backlog_path.exists():
        print(f"Missing backlog JSON: {backlog_path}", file=sys.stderr)
        return 2

    intent_snapshot_arg: Path | None = args.intent_snapshot_json
    if intent_snapshot_arg is not None:
        intent_snapshot_path = (
            _resolve_optional_path(repo_root, intent_snapshot_arg) or intent_snapshot_arg.resolve()
        )
    else:
        intent_snapshot_path = compiled_dir / f"{default_name}.intent_snapshot.json"

    allow_missing_snapshot = bool(args.allow_missing_intent_snapshot)
    intent_snapshot_obj: dict[str, Any] | None = None
    if intent_snapshot_path.exists():
        try:
            raw_snapshot = json.loads(intent_snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(
                f"Failed to parse intent snapshot JSON: {intent_snapshot_path}: {e}",
                file=sys.stderr,
            )
            return 2
        if isinstance(raw_snapshot, dict):
            intent_snapshot_obj = raw_snapshot
        else:
            intent_snapshot_obj = {"raw": raw_snapshot}
    elif not allow_missing_snapshot:
        print(
            "Missing intent snapshot JSON: "
            f"{intent_snapshot_path} (run `usertest-backlog reports intent-snapshot`)",
            file=sys.stderr,
        )
        return 2

    intent_snapshot_json_path = (
        str(intent_snapshot_path) if intent_snapshot_obj is not None else None
    )

    repo_intent_arg: Path | None = args.repo_intent_md
    if repo_intent_arg is not None:
        repo_intent_path = (
            _resolve_optional_path(repo_root, repo_intent_arg) or repo_intent_arg.resolve()
        )
    else:
        repo_intent_path = repo_root / "configs" / "repo_intent.md"
    if not repo_intent_path.exists():
        print(f"Missing repo intent doc: {repo_intent_path}", file=sys.stderr)
        return 2

    if args.out_json is not None:
        out_json = _resolve_optional_path(repo_root, args.out_json) or args.out_json.resolve()
    else:
        out_json = compiled_dir / f"{default_name}.ux_review.json"

    if args.out_md is not None:
        out_md = _resolve_optional_path(repo_root, args.out_md) or args.out_md.resolve()
    else:
        out_md = out_json.with_suffix(".md")

    prompts_dir_arg: Path | None = args.prompts_dir
    if prompts_dir_arg is not None:
        prompts_dir = (
            _resolve_optional_path(repo_root, prompts_dir_arg) or prompts_dir_arg.resolve()
        )
    else:
        prompts_dir = repo_root / "configs" / "backlog_prompts"

    template_path = prompts_dir / "ux_reviewer.md"
    if not template_path.exists():
        print(f"Missing UX reviewer prompt template: {template_path}", file=sys.stderr)
        return 2

    try:
        backlog_doc = json.loads(backlog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Failed to parse backlog JSON: {backlog_path}: {e}", file=sys.stderr)
        return 2
    if not isinstance(backlog_doc, dict):
        print(f"Invalid backlog JSON (expected object): {backlog_path}", file=sys.stderr)
        return 2

    tickets_raw = backlog_doc.get("tickets")
    tickets = (
        [item for item in tickets_raw if isinstance(item, dict)]
        if isinstance(tickets_raw, list)
        else []
    )
    review_tickets = [
        ticket
        for ticket in tickets
        if (_coerce_string(ticket.get("stage")) or "triage") == "research_required"
    ]

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not review_tickets:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_md.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "schema_version": 1,
            "generated_at": generated_at,
            "scope": {"target": target_slug, "repo_input": repo_input},
            "status": "no_research_required_tickets",
            "inputs": {
                "backlog_json": str(backlog_path),
                "intent_snapshot_json": intent_snapshot_json_path,
                "repo_intent_md": str(repo_intent_path),
            },
            "review": {"recommendations": [], "confidence": 1.0},
            "artifacts_dir": None,
        }
        out_json.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        out_md.write_text(_render_ux_review_markdown(doc), encoding="utf-8")
        print(str(out_json))
        print(str(out_md))
        return 0

    try:
        repo_intent_text = repo_intent_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"Failed reading repo intent doc: {repo_intent_path}: {e}", file=sys.stderr)
        return 2

    template = template_path.read_text(encoding="utf-8")
    tickets_payload: list[dict[str, Any]] = []
    for ticket in review_tickets:
        payload: dict[str, Any] = {}
        for key in (
            "ticket_id",
            "title",
            "problem",
            "user_impact",
            "severity",
            "confidence",
            "change_surface",
            "breadth",
            "stage",
            "risks",
            "proposed_fix",
            "investigation_steps",
            "success_criteria",
            "suggested_owner",
        ):
            if key in ticket:
                payload[key] = ticket.get(key)
        tickets_payload.append(payload)

    prompt = _render_template(
        template,
        {
            "REPO_INTENT_MD": repo_intent_text,
            "INTENT_SNAPSHOT_JSON": json.dumps(intent_snapshot_obj, indent=2, ensure_ascii=False)
            if intent_snapshot_obj is not None
            else "null",
            "TICKETS_JSON": json.dumps(tickets_payload, indent=2, ensure_ascii=False),
        },
    )
    prompt_hash = sha256(prompt.encode("utf-8")).hexdigest()[:16]

    artifacts_dir = out_json.parent / f"{default_name}.ux_review_artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    agent = str(args.agent)
    model = str(args.model) if isinstance(args.model, str) and args.model.strip() else None
    resume = bool(args.resume)
    force = bool(args.force)
    dry_run = bool(args.dry_run)

    tag = f"ux_review_{prompt_hash}"
    cached_path = artifacts_dir / f"{tag}.review.json"

    review_obj: dict[str, Any] | None = None
    status = "ok"
    used_cached = False

    if resume and not force and cached_path.exists():
        try:
            cached = json.loads(cached_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            warnings.warn(
                f"Failed to parse cached UX review at {cached_path}: {e}; rerunning review.",
                RuntimeWarning,
                stacklevel=2,
            )
            cached = None
        except OSError as e:
            warnings.warn(
                f"Failed reading cached UX review at {cached_path}: {e}; rerunning review.",
                RuntimeWarning,
                stacklevel=2,
            )
            cached = None
        if isinstance(cached, dict):
            review_obj = cached
            status = "cached"
            used_cached = True
        elif cached is not None:
            warnings.warn(
                "Ignoring cached UX review with unexpected payload type "
                f"{type(cached).__name__} at {cached_path}; expected object.",
                RuntimeWarning,
                stacklevel=2,
            )

    if review_obj is None:
        if dry_run:
            (artifacts_dir / f"{tag}.dry_run.prompt.txt").write_text(prompt, encoding="utf-8")
            status = "dry_run"
        else:
            raw_text = run_backlog_prompt(
                agent=agent,
                prompt=prompt,
                out_dir=artifacts_dir,
                tag=tag,
                model=model,
                cfg=cfg,
            )
            parsed = _parse_first_json_object(raw_text)
            if not isinstance(parsed, dict):
                (artifacts_dir / f"{tag}.parse_error.txt").write_text(
                    raw_text.strip() + "\n",
                    encoding="utf-8",
                )
                print(
                    "Failed to parse JSON from reviewer output "
                    f"(see artifacts under {artifacts_dir})",
                    file=sys.stderr,
                )
                return 2
            review_obj = parsed
            cached_path.write_text(
                json.dumps(review_obj, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    doc: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": generated_at,
        "scope": {"target": target_slug, "repo_input": repo_input},
        "status": status,
        "prompt_hash": prompt_hash,
        "inputs": {
            "backlog_json": str(backlog_path),
            "intent_snapshot_json": intent_snapshot_json_path,
            "repo_intent_md": str(repo_intent_path),
            "allow_missing_intent_snapshot": allow_missing_snapshot,
        },
        "artifacts_dir": str(artifacts_dir),
        "review_meta": {
            "agent": agent,
            "model": model,
            "cached": used_cached,
            "template_path": _safe_relpath(template_path, repo_root),
        },
        "tickets_meta": {
            "tickets_total": len(tickets),
            "research_required_total": len(review_tickets),
        },
        "review": review_obj,
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out_md.write_text(_render_ux_review_markdown(doc), encoding="utf-8")

    print(str(out_json))
    print(str(out_md))
    print(f"Reviewer status: {status}")
    return 0


_RESEARCH_TICKET_TEMPLATE_MD = """## Research / ADR Template

### Intent check
- What does `configs/repo_intent.md` say about this proposal?
- Does this solve a repo-wide problem or a single mission-local preference?

### Surface consolidation checklist
- Can an existing command be parameterized instead of adding a new command?
- Can docs/examples remove the friction without any new surface area?
- If a new surface is required, what is the minimal addition?

### Alternatives considered
- Parameterize existing command(s)
- Improve docs/examples
- Defer / do nothing

### Decision outcome
- Outcome: (approved | rejected | deferred)
- Notes:
"""


def _render_export_issue_body(
    *,
    ticket: dict[str, Any],
    fingerprint: str,
    export_kind: str,
    surface_area_high: set[str],
) -> str:
    """Render export issue body output text.

    Parameters
    ----------
    ticket:
        Ticket payload mapping.
    fingerprint:
        Input parameter.
    export_kind:
        Input parameter.
    surface_area_high:
        Input parameter.

    Returns
    -------
    str
        Normalized string result.
    """
    ticket_id = _coerce_string(ticket.get("ticket_id")) or "TKT-unknown"
    title = _coerce_string(ticket.get("title")) or ""
    problem = _coerce_string(ticket.get("problem")) or ""
    user_impact = _coerce_string(ticket.get("user_impact")) or ""
    proposed_fix = _coerce_string(ticket.get("proposed_fix")) or ""

    change_surface_raw = ticket.get("change_surface")
    change_surface = change_surface_raw if isinstance(change_surface_raw, dict) else {}
    kinds = sorted(set(_coerce_string_list(change_surface.get("kinds"))))
    user_visible = bool(change_surface.get("user_visible"))
    breadth_raw = ticket.get("breadth")
    breadth = breadth_raw if isinstance(breadth_raw, dict) else {}

    lines: list[str] = []
    lines.append(f"- Source ticket: `{ticket_id}`")
    lines.append(f"- Fingerprint: `{fingerprint}`")
    lines.append(f"- Export kind: `{export_kind}`")
    stage = _coerce_string(ticket.get("stage"))
    if stage:
        lines.append(f"- Stage: `{stage}`")
    severity = _coerce_string(ticket.get("severity"))
    if severity:
        lines.append(f"- Severity: `{severity}`")
    if kinds:
        lines.append(f"- Change surface kinds: `{', '.join(kinds)}`")
    if user_visible:
        gated = bool(set(kinds) & surface_area_high)
        lines.append(f"- User-visible: `true` (high-surface gated: `{str(gated).lower()}`)")
    lines.append("")

    if title:
        lines.append("## Title")
        lines.append("")
        lines.append(title)
        lines.append("")

    if problem:
        lines.append("## Problem")
        lines.append("")
        lines.append(problem)
        lines.append("")

    if user_impact:
        lines.append("## User impact")
        lines.append("")
        lines.append(user_impact)
        lines.append("")

    if proposed_fix:
        lines.append("## Proposed fix")
        lines.append("")
        lines.append(proposed_fix)
        lines.append("")

    inv_steps = _coerce_string_list(ticket.get("investigation_steps"))
    if inv_steps:
        lines.append("## Investigation steps")
        lines.append("")
        for step in inv_steps:
            lines.append(f"- {step}")
        lines.append("")

    success = _coerce_string_list(ticket.get("success_criteria"))
    if success:
        lines.append("## Success criteria")
        lines.append("")
        for criterion in success:
            lines.append(f"- {criterion}")
        lines.append("")

    if breadth:
        lines.append("## Evidence breadth (counts)")
        lines.append("")
        for dim in ("missions", "targets", "repo_inputs", "agents", "personas", "runs"):
            val = breadth.get(dim)
            if isinstance(val, (int, float)):
                lines.append(f"- {dim}: {int(val)}")
        lines.append("")

    evidence_ids = _coerce_string_list(ticket.get("evidence_atom_ids"))
    if evidence_ids:
        lines.append("## Evidence atom ids")
        lines.append("")
        for atom_id in evidence_ids[:40]:
            lines.append(f"- `{atom_id}`")
        lines.append("")

    if export_kind == "research":
        lines.append(_RESEARCH_TICKET_TEMPLATE_MD.rstrip())
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_ticket_export_markdown(doc: dict[str, Any]) -> str:
    """Render ticket export markdown output text.

    Parameters
    ----------
    doc:
        Structured document payload.

    Returns
    -------
    str
        Normalized string result.
    """
    generated_at = _coerce_string(doc.get("generated_at")) or "unknown"
    scope = doc.get("scope") if isinstance(doc.get("scope"), dict) else {}
    target = _coerce_string(scope.get("target")) or "all"
    repo_input = _coerce_string(scope.get("repo_input"))

    lines: list[str] = []
    lines.append("# Ticket Export")
    lines.append("")
    lines.append(f"- Generated at: `{generated_at}`")
    lines.append(f"- Scope target: `{target}`")
    if repo_input is not None:
        lines.append(f"- Scope repo_input: `{repo_input}`")

    filters = doc.get("filters") if isinstance(doc.get("filters"), dict) else {}
    stages = filters.get("stages")
    if isinstance(stages, list) and stages:
        lines.append(f"- Stages: `{', '.join([s for s in stages if isinstance(s, str)])}`")
    min_sev = _coerce_string(filters.get("min_severity"))
    if min_sev:
        lines.append(f"- Min severity: `{min_sev}`")
    include_actioned = filters.get("include_actioned")
    if isinstance(include_actioned, bool):
        lines.append(f"- Include actioned: `{str(include_actioned).lower()}`")
    lines.append("")

    exports_raw = doc.get("exports")
    exports = (
        [item for item in exports_raw if isinstance(item, dict)]
        if isinstance(exports_raw, list)
        else []
    )

    research = [e for e in exports if _coerce_string(e.get("export_kind")) == "research"]
    impl = [e for e in exports if _coerce_string(e.get("export_kind")) == "implementation"]

    def _render_section(title: str, items: list[dict[str, Any]]) -> None:
        """Render section output text.

        Parameters
        ----------
        title:
            Title text input.
        items:
            Collection items to process.

        Returns
        -------
        None
            None.
        """
        lines.append(f"## {title}")
        lines.append("")
        if not items:
            lines.append("- (none)")
            lines.append("")
            return
        for item in items:
            issue_title = _coerce_string(item.get("title")) or "Untitled"
            fingerprint = _coerce_string(item.get("fingerprint")) or "unknown"
            lines.append(f"### {issue_title}")
            lines.append("")
            lines.append(f"- Fingerprint: `{fingerprint}`")
            owner_repo_raw = item.get("owner_repo")
            owner_repo = owner_repo_raw if isinstance(owner_repo_raw, dict) else {}
            idea_path = _coerce_string(owner_repo.get("idea_path"))
            if idea_path:
                lines.append(f"- Idea file: `{idea_path}`")
            owner_root = _coerce_string(owner_repo.get("root"))
            if owner_root:
                lines.append(f"- Owner repo root: `{owner_root}`")
            body = _coerce_string(item.get("body_markdown")) or ""
            if body:
                lines.append("")
                lines.append(body.rstrip())
            lines.append("")

    _render_section("Research / Design", research)
    _render_section("Implementation", impl)
    return "\n".join(lines).rstrip() + "\n"


def _cmd_reports_export_tickets(args: argparse.Namespace) -> int:
    """Execute the `reports export tickets` command handler.

    Parameters
    ----------
    args:
        Parsed command-line arguments namespace.

    Returns
    -------
    int
        Process exit code.
    """
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

    default_name = slugify(repo_input) if repo_input is not None else (target_slug or "all")
    if target_slug is not None:
        compiled_dir = runs_dir / target_slug / "_compiled"
    else:
        compiled_dir = runs_dir / "_compiled"

    backlog_arg: Path | None = args.backlog_json
    if backlog_arg is not None:
        backlog_path = _resolve_optional_path(repo_root, backlog_arg) or backlog_arg.resolve()
    else:
        backlog_path = compiled_dir / f"{default_name}.backlog.json"
    if not backlog_path.exists():
        print(f"Missing backlog JSON: {backlog_path}", file=sys.stderr)
        return 2

    actions_arg: Path | None = args.actions_yaml
    if actions_arg is not None:
        actions_path = _resolve_optional_path(repo_root, actions_arg) or actions_arg.resolve()
    else:
        actions_path = repo_root / "configs" / "backlog_actions.yaml"

    atom_actions_arg: Path | None = args.atom_actions_yaml
    if atom_actions_arg is not None:
        atom_actions_path = (
            _resolve_optional_path(repo_root, atom_actions_arg) or atom_actions_arg.resolve()
        )
    else:
        atom_actions_path = repo_root / "configs" / "backlog_atom_actions.yaml"

    try:
        actions = _load_backlog_actions_yaml(actions_path)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    policy_cfg: BacklogPolicyConfig | None = None
    policy_config_path: Path | None
    if args.policy_config is not None:
        policy_config_path = (
            _resolve_optional_path(repo_root, args.policy_config) or args.policy_config.resolve()
        )
    else:
        default_policy = repo_root / "configs" / "backlog_policy.yaml"
        policy_config_path = default_policy if default_policy.exists() else None
    if policy_config_path is None or not policy_config_path.exists():
        print(
            "Missing backlog policy config (needed for high-surface gating). "
            "Provide --policy-config or add configs/backlog_policy.yaml.",
            file=sys.stderr,
        )
        return 2

    try:
        policy_raw = _load_yaml(policy_config_path).get("backlog_policy", {})
        if not isinstance(policy_raw, dict):
            raise ValueError("backlog_policy config must be a mapping")
        policy_cfg = BacklogPolicyConfig.from_dict(policy_raw)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as e:
        print(f"Invalid backlog policy config: {policy_config_path}: {e}", file=sys.stderr)
        return 2

    surface_area_high = set(policy_cfg.surface_area_high)

    if args.out_json is not None:
        out_json = _resolve_optional_path(repo_root, args.out_json) or args.out_json.resolve()
    else:
        out_json = compiled_dir / f"{default_name}.tickets_export.json"

    if args.out_md is not None:
        out_md = _resolve_optional_path(repo_root, args.out_md) or args.out_md.resolve()
    else:
        out_md = out_json.with_suffix(".md")

    try:
        backlog_doc = json.loads(backlog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Failed to parse backlog JSON: {backlog_path}: {e}", file=sys.stderr)
        return 2
    if not isinstance(backlog_doc, dict):
        print(f"Invalid backlog JSON (expected object): {backlog_path}", file=sys.stderr)
        return 2
    backlog_scope_raw = backlog_doc.get("scope")
    backlog_scope = backlog_scope_raw if isinstance(backlog_scope_raw, dict) else {}
    backlog_scope_repo_input = _coerce_string(backlog_scope.get("repo_input"))

    tickets_raw = backlog_doc.get("tickets")
    tickets = (
        [item for item in tickets_raw if isinstance(item, dict)]
        if isinstance(tickets_raw, list)
        else []
    )

    stage_filters = [s.strip() for s in args.stage if isinstance(s, str) and s.strip()]
    stages = stage_filters if stage_filters else ["triage", "ready_for_ticket", "research_required"]
    min_severity = str(args.min_severity)
    include_actioned = bool(args.include_actioned)

    print(
        "Export filters:",
        f"stages={stages}",
        f"min_severity={min_severity}",
        f"include_actioned={include_actioned}",
        sep=" ",
    )

    exports: list[dict[str, Any]] = []
    queued_refs: list[dict[str, str]] = []
    skipped_actioned = 0
    skipped_existing_plan = 0
    skipped_stage = 0
    skipped_severity = 0
    idea_files_written: list[str] = []
    plan_index_cache: dict[Path, dict[str, dict[str, Any]]] = {}
    skip_plan_folder_dedupe = bool(getattr(args, "skip_plan_folder_dedupe", False))

    for ticket in tickets:
        stage = (_coerce_string(ticket.get("stage")) or "triage").strip()
        if stage not in stages:
            skipped_stage += 1
            continue
        severity = (_coerce_string(ticket.get("severity")) or "medium").strip().lower()
        if _severity_rank(severity) < _severity_rank(min_severity):
            skipped_severity += 1
            continue

        fingerprint = ticket_export_fingerprint(ticket)
        if (fingerprint in actions) and not include_actioned:
            skipped_actioned += 1
            continue

        change_surface_raw = ticket.get("change_surface")
        change_surface = change_surface_raw if isinstance(change_surface_raw, dict) else {}
        kinds = set(_coerce_string_list(change_surface.get("kinds")))
        user_visible = bool(change_surface.get("user_visible"))

        export_kind = "implementation"
        if stage == "research_required":
            export_kind = "research"
        elif user_visible and bool(kinds & surface_area_high):
            export_kind = "research"

        title = _coerce_string(ticket.get("title")) or "Untitled"
        issue_title = f"[Research] {title}" if export_kind == "research" else title

        body = _render_export_issue_body(
            ticket=ticket,
            fingerprint=fingerprint,
            export_kind=export_kind,
            surface_area_high=surface_area_high,
        )

        labels: list[str] = []
        labels.append(f"stage:{stage}")
        labels.append(f"severity:{severity}")
        if export_kind == "research":
            labels.append("type:research")
        else:
            labels.append("type:implementation")
        owner = _coerce_string(ticket.get("suggested_owner")) or _coerce_string(
            ticket.get("component")
        )
        if owner:
            labels.append(f"owner:{owner}")
        for kind in sorted(kinds):
            labels.append(f"surface:{kind}")

        owner_repo_root, owner_repo_input, owner_repo_resolution = _resolve_owner_repo_root(
            ticket=ticket,
            scope_repo_input=backlog_scope_repo_input,
            cli_repo_input=repo_input,
            repo_root=repo_root,
        )

        if not include_actioned and not skip_plan_folder_dedupe:
            owner_key = owner_repo_root.resolve()
            if owner_key not in plan_index_cache:
                plan_index_cache[owner_key] = _scan_plan_ticket_index(owner_root=owner_key)
            existing = plan_index_cache[owner_key].get(fingerprint)
            if isinstance(existing, dict):
                skipped_existing_plan += 1
                desired_status = _normalize_atom_status(_coerce_string(existing.get("status")))
                if desired_status not in ("queued", "actioned"):
                    desired_status = "queued"
                queue_paths = [
                    item for item in existing.get("paths", []) if isinstance(item, str) and item
                ]
                ticket_id = _coerce_string(ticket.get("ticket_id")) or "unknown"
                for atom_id in _coerce_string_list(ticket.get("evidence_atom_ids")):
                    ref: dict[str, str] = {
                        "atom_id": atom_id,
                        "ticket_id": ticket_id,
                        "fingerprint": fingerprint,
                        "owner_root": str(owner_repo_root),
                        "desired_status": desired_status,
                    }
                    if queue_paths:
                        ref["idea_path"] = queue_paths[0]
                    queued_refs.append(ref)
                continue

        idea_path = _write_ticket_idea_file(
            ticket=ticket,
            issue_title=issue_title,
            fingerprint=fingerprint,
            body_markdown=body,
            owner_repo_root=owner_repo_root,
        )
        _cleanup_stale_ticket_idea_files(
            ticket=ticket,
            fingerprint=fingerprint,
            owner_repo_root=owner_repo_root,
            repo_root=repo_root,
            scope_repo_input=backlog_scope_repo_input,
            cli_repo_input=repo_input,
            keep_path=idea_path,
        )
        idea_files_written.append(str(idea_path))
        ticket_id = _coerce_string(ticket.get("ticket_id")) or "unknown"
        for atom_id in _coerce_string_list(ticket.get("evidence_atom_ids")):
            queued_refs.append(
                {
                    "atom_id": atom_id,
                    "ticket_id": ticket_id,
                    "fingerprint": fingerprint,
                    "idea_path": str(idea_path),
                    "owner_root": str(owner_repo_root),
                }
            )

        exports.append(
            {
                "fingerprint": fingerprint,
                "export_kind": export_kind,
                "title": issue_title,
                "labels": labels,
                "body_markdown": body,
                "source_ticket": {
                    "ticket_id": ticket.get("ticket_id"),
                    "stage": stage,
                    "severity": severity,
                },
                "owner_repo": {
                    "repo_input": owner_repo_input,
                    "root": str(owner_repo_root),
                    "resolution": owner_repo_resolution,
                    "idea_path": str(idea_path),
                },
                "action_ledger": actions.get(fingerprint),
            }
        )

    try:
        atom_actions = _load_atom_actions_yaml(atom_actions_path)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    atom_status_meta = _update_atom_actions_from_exports(
        atom_actions=atom_actions,
        queued_refs=queued_refs,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        export_json_path=out_json,
    )
    _write_atom_actions_yaml(atom_actions_path, atom_actions)

    export_doc: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": {"target": target_slug, "repo_input": repo_input},
        "inputs": {
            "backlog_json": str(backlog_path),
            "actions_yaml": str(actions_path),
            "atom_actions_yaml": str(atom_actions_path),
            "policy_config": str(policy_config_path),
        },
        "filters": {
            "stages": stages,
            "min_severity": min_severity,
            "include_actioned": include_actioned,
        },
        "policy": {
            "surface_area_high": sorted(surface_area_high),
        },
        "stats": {
            "tickets_total": len(tickets),
            "exports_total": len(exports),
            "skipped_actioned": skipped_actioned,
            "skipped_existing_plan": skipped_existing_plan,
            "skipped_stage": skipped_stage,
            "skipped_severity": skipped_severity,
            "actioned_total": len(actions),
            "idea_files_written": len(idea_files_written),
            "atom_status_updates": atom_status_meta,
        },
        "idea_files": idea_files_written,
        "exports": exports,
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(export_doc, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out_md.write_text(_render_ticket_export_markdown(export_doc), encoding="utf-8")

    print(str(out_json))
    print(str(out_md))
    for path in idea_files_written:
        print(path)
    print(json.dumps(export_doc["stats"], indent=2, ensure_ascii=False))
    return 0


def _cmd_reports_backlog(args: argparse.Namespace) -> int:
    """Execute the `reports backlog` command handler.

    Parameters
    ----------
    args:
        Parsed command-line arguments namespace.

    Returns
    -------
    int
        Process exit code.
    """
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

    default_name = slugify(repo_input) if repo_input is not None else (target_slug or "all")

    if args.out_json is not None:
        out_json = _resolve_optional_path(repo_root, args.out_json) or args.out_json.resolve()
    else:
        if target_slug is not None:
            out_json = runs_dir / target_slug / "_compiled" / f"{default_name}.backlog.json"
        else:
            out_json = runs_dir / "_compiled" / f"{default_name}.backlog.json"

    if args.out_md is not None:
        out_md = _resolve_optional_path(repo_root, args.out_md) or args.out_md.resolve()
    else:
        out_md = out_json.with_suffix(".md")

    atom_actions_arg: Path | None = args.atom_actions_yaml
    if atom_actions_arg is not None:
        atom_actions_path = (
            _resolve_optional_path(repo_root, atom_actions_arg) or atom_actions_arg.resolve()
        )
    else:
        atom_actions_path = repo_root / "configs" / "backlog_atom_actions.yaml"

    prompts_dir_arg: Path | None = args.prompts_dir
    if prompts_dir_arg is not None:
        prompts_dir = (
            _resolve_optional_path(repo_root, prompts_dir_arg) or prompts_dir_arg.resolve()
        )
    else:
        prompts_dir = repo_root / "configs" / "backlog_prompts"
    prompt_manifest = load_prompt_manifest(prompts_dir)

    atoms_jsonl = out_json.parent / f"{default_name}.backlog.atoms.jsonl"
    artifacts_dir = out_json.parent / f"{default_name}.backlog_artifacts"

    records = list(
        iter_report_history(
            runs_dir,
            target_slug=target_slug,
            repo_input=repo_input,
            embed="none",
        )
    )
    atoms_doc_raw = extract_backlog_atoms(records, repo_root=repo_root)
    atoms_raw = atoms_doc_raw.get("atoms")
    raw_atoms = (
        [item for item in atoms_raw if isinstance(item, dict)]
        if isinstance(atoms_raw, list)
        else []
    )

    try:
        atom_actions = _load_atom_actions_yaml(atom_actions_path)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    plan_sync_meta: dict[str, Any] | None = None
    plan_sync_at: str | None = None
    if not bool(getattr(args, "skip_plan_folder_sync", False)):
        candidate_roots: list[Path] = [repo_root]

        for record in records:
            target_ref = record.get("target_ref")
            if not isinstance(target_ref, dict):
                continue
            repo_input_from_record = _coerce_string(target_ref.get("repo_input"))
            if repo_input_from_record is None:
                continue
            if not _looks_like_local_repo_input(repo_input_from_record):
                continue
            resolved = _resolve_local_repo_root(repo_root, repo_input_from_record)
            if resolved is None:
                continue
            candidate_roots.append(resolved)

        for entry in atom_actions.values():
            roots_raw = entry.get("queue_owner_roots")
            roots = (
                [item for item in roots_raw if isinstance(item, str) and item.strip()]
                if isinstance(roots_raw, list)
                else []
            )
            for root_s in roots:
                if not _looks_like_local_repo_input(root_s):
                    continue
                resolved = _resolve_local_repo_root(repo_root, root_s)
                if resolved is None:
                    continue
                candidate_roots.append(resolved)

        owner_roots = sorted({p.resolve() for p in candidate_roots}, key=lambda p: str(p))
        sync_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        plan_sync_at = sync_at
        plan_sync_meta = _sync_atom_actions_from_plan_folders(
            atom_actions=atom_actions,
            owner_roots=owner_roots,
            generated_at=sync_at,
        )

    backfill_at = plan_sync_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    backfill_meta = _backfill_failure_event_atoms_from_legacy_entries(
        atom_actions=atom_actions,
        generated_at=backfill_at,
    )
    if plan_sync_meta is not None:
        plan_sync_meta["failure_event_backfill"] = backfill_meta
        _write_atom_actions_yaml(atom_actions_path, atom_actions)

    # By default, do not re-mine atoms that already produced any ticket outcome.
    exclude_atom_statuses = args.exclude_atom_status or ["ticketed", "queued", "actioned"]
    exclude_atom_status_set = {
        _normalize_atom_status(_coerce_string(status))
        for status in exclude_atom_statuses
        if _coerce_string(status) is not None
    }
    excluded_atoms: list[dict[str, Any]] = []
    atoms: list[dict[str, Any]] = []
    excluded_status_counts: dict[str, int] = {}
    for atom in raw_atoms:
        atom_id = _coerce_string(atom.get("atom_id"))
        atom_status = "new"
        if atom_id is not None:
            existing = atom_actions.get(atom_id)
            if isinstance(existing, dict):
                atom_status = _normalize_atom_status(_coerce_string(existing.get("status")))
        if atom_status in exclude_atom_status_set:
            excluded_atoms.append(atom)
            excluded_status_counts[atom_status] = excluded_status_counts.get(atom_status, 0) + 1
            continue
        atoms.append(atom)

    eligible_atoms_trackable = len(atoms)
    eligible_run_rels = {
        run_rel
        for atom in atoms
        for run_rel in [_coerce_string(atom.get("run_rel"))]
        if run_rel is not None
    }
    aggregate_run_id_prefix = (
        "__aggregate__/"
        + (target_slug or "all")
        + "/"
        + (slugify(repo_input) if repo_input is not None else "all")
    )
    aggregate_atoms = build_aggregate_metrics_atoms(
        records,
        eligible_run_rels,
        run_id_prefix=aggregate_run_id_prefix,
    )
    atoms.extend(aggregate_atoms)

    atom_totals = _summarize_atoms_for_totals(atoms)
    atoms_doc = dict(atoms_doc_raw)
    atoms_doc["atoms"] = atoms
    totals_raw = atoms_doc_raw.get("totals")
    totals_dict = dict(totals_raw) if isinstance(totals_raw, dict) else {}
    totals_dict.update(atom_totals)
    atoms_doc["totals"] = totals_dict
    atoms_doc["atom_filter"] = {
        "exclude_statuses": sorted(exclude_atom_status_set),
        "eligible_atoms": len(atoms),
        "eligible_atoms_trackable": eligible_atoms_trackable,
        "synthetic_atoms_added": len(aggregate_atoms),
        "excluded_atoms": len(excluded_atoms),
        "excluded_status_counts": excluded_status_counts,
        "plan_folder_sync": plan_sync_meta,
        "excluded_atom_ids_preview": [
            atom_id
            for atom in excluded_atoms[:200]
            for atom_id in [_coerce_string(atom.get("atom_id"))]
            if atom_id is not None
        ],
    }
    write_backlog_atoms(atoms_doc, atoms_jsonl)

    miners = max(0, int(args.miners))
    sample_size = int(args.sample_size)
    if sample_size < 0:
        raise ValueError("--sample-size must be >= 0")
    sample_size_semantics = "all_atoms" if sample_size == 0 else "fixed_sample"
    coverage_miners = max(0, int(args.coverage_miners))
    bagging_miners = (
        max(0, int(args.bagging_miners))
        if args.bagging_miners is not None
        else max(0, miners - coverage_miners)
    )
    max_tickets_per_miner = max(1, int(args.max_tickets_per_miner))
    orphan_pass = max(0, int(args.orphan_pass))
    seed = int(args.seed)
    resume = bool(args.resume)
    force = bool(args.force)
    dry_run = bool(args.dry_run)
    no_merge = bool(args.no_merge)
    agent = str(args.agent)
    model = str(args.model) if isinstance(args.model, str) and args.model.strip() else None
    labelers = max(0, int(args.labelers))
    if labelers == 0:
        print(
            "WARNING: --labelers=0 disables ticket labeling; tickets keep "
            "change_surface.kinds=['unknown'] and policy stage promotion will not run.",
            file=sys.stderr,
        )

    policy_cfg: BacklogPolicyConfig | None = None
    policy_config_path: Path | None
    if args.policy_config is not None:
        policy_config_path = (
            _resolve_optional_path(repo_root, args.policy_config) or args.policy_config.resolve()
        )
    else:
        default_policy = repo_root / "configs" / "backlog_policy.yaml"
        policy_config_path = default_policy if default_policy.exists() else None
    if not bool(args.no_policy) and policy_config_path is not None and policy_config_path.exists():
        policy_root = _load_yaml(policy_config_path).get("backlog_policy")
        if policy_root is None:
            raise ValueError(f"Expected backlog_policy key in {policy_config_path}")
        if not isinstance(policy_root, dict):
            raise ValueError(
                f"Expected mapping at backlog_policy in {policy_config_path}, got "
                f"{type(policy_root).__name__}"
            )
        policy_cfg = BacklogPolicyConfig.from_dict(policy_root)

    ensemble = run_backlog_ensemble(
        atoms=atoms,
        artifacts_dir=artifacts_dir,
        prompts_dir=prompts_dir,
        prompt_manifest=prompt_manifest,
        agent=agent,
        model=model,
        cfg=cfg,
        miners=miners,
        sample_size=sample_size,
        coverage_miners=coverage_miners,
        bagging_miners=bagging_miners,
        max_tickets_per_miner=max_tickets_per_miner,
        seed=seed,
        resume=resume,
        force=force,
        dry_run=dry_run,
        no_merge=no_merge,
        orphan_pass=orphan_pass,
    )

    tickets_raw = ensemble.get("tickets")
    tickets = (
        [item for item in tickets_raw if isinstance(item, dict)]
        if isinstance(tickets_raw, list)
        else []
    )
    miners_meta = ensemble.get("miners_meta")
    miners_meta_dict = miners_meta if isinstance(miners_meta, dict) else {}

    labelers_meta_dict: dict[str, Any] = {}
    if labelers > 0 and tickets:
        atoms_by_id = {
            atom_id: atom
            for atom in atoms
            for atom_id in [atom.get("atom_id")]
            if isinstance(atom_id, str) and atom_id
        }
        labeled = run_labeler_jobs(
            tickets=tickets,
            atoms_by_id=atoms_by_id,
            prompts_dir=prompts_dir,
            prompt_manifest=prompt_manifest,
            artifacts_dir=artifacts_dir,
            agent=agent,
            model=model,
            cfg=cfg,
            labelers=labelers,
            resume=resume,
            force=force,
            dry_run=dry_run,
        )
        tickets_raw = labeled.get("tickets")
        tickets = (
            [item for item in tickets_raw if isinstance(item, dict)]
            if isinstance(tickets_raw, list)
            else tickets
        )
        labelers_meta = labeled.get("labelers_meta")
        labelers_meta_dict = labelers_meta if isinstance(labelers_meta, dict) else {}

    eligible_atom_ids = {
        atom_id
        for atom in atoms
        for atom_id in [_coerce_string(atom.get("atom_id"))]
        if atom_id is not None
    }
    dropped_tickets_excluded_atoms = 0
    filtered_tickets: list[dict[str, Any]] = []
    for ticket in tickets:
        evidence_ids = _coerce_string_list(ticket.get("evidence_atom_ids"))
        filtered_ids = [atom_id for atom_id in evidence_ids if atom_id in eligible_atom_ids]
        if not filtered_ids:
            dropped_tickets_excluded_atoms += 1
            continue
        updated = dict(ticket)
        updated["evidence_atom_ids"] = filtered_ids
        filtered_tickets.append(updated)
    tickets = filtered_tickets

    summary = build_backlog_document(
        atoms_doc=atoms_doc,
        tickets=tickets,
        input_meta={
            "runs_dir": str(runs_dir),
            "target": target_slug,
            "repo_input": repo_input,
            "agent": agent,
            "model": model,
            "miners": miners,
            "sample_size": sample_size,
            "sample_size_semantics": sample_size_semantics,
            "exclude_atom_statuses": sorted(exclude_atom_status_set),
            "coverage_miners": coverage_miners,
            "bagging_miners": bagging_miners,
            "max_tickets_per_miner": max_tickets_per_miner,
            "resume": resume,
            "force": force,
            "seed": seed,
            "no_merge": no_merge,
            "orphan_pass": orphan_pass,
            "dry_run": dry_run,
            "labelers": labelers,
        },
        artifacts={
            "atoms_jsonl": str(atoms_jsonl),
            "artifacts_dir": str(artifacts_dir),
            "prompts_dir": str(prompts_dir),
            "atom_filter": {
                **(atoms_doc.get("atom_filter") or {}),
                "dropped_tickets_excluded_atoms": dropped_tickets_excluded_atoms,
            },
            "prompt_manifest": {
                "path": str(prompts_dir / "manifest.json"),
                "coverage_templates": list(prompt_manifest.coverage_templates),
                "bagging_templates": list(prompt_manifest.bagging_templates),
                "orphan_template": prompt_manifest.orphan_template,
                "merge_judge_template": prompt_manifest.merge_judge_template,
                "labeler_template": prompt_manifest.labeler_template,
            },
            "labelers_meta": labelers_meta_dict,
        },
        miners_meta=miners_meta_dict,
    )

    if policy_cfg is not None:
        tickets_raw = summary.get("tickets")
        tickets_list = (
            [item for item in tickets_raw if isinstance(item, dict)]
            if isinstance(tickets_raw, list)
            else []
        )
        if tickets_list:
            updated_tickets, policy_meta = apply_backlog_policy(tickets_list, config=policy_cfg)
            summary["tickets"] = updated_tickets
            artifacts = summary.get("artifacts")
            artifacts_dict = artifacts if isinstance(artifacts, dict) else {}
            artifacts_dict["policy"] = {
                "config_path": str(policy_config_path) if policy_config_path is not None else None,
                "meta": policy_meta,
            }
            summary["artifacts"] = artifacts_dict

    generated_at = _coerce_string(summary.get("generated_at_utc")) or datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    tickets_for_atoms_raw = summary.get("tickets")
    tickets_for_atoms = (
        [item for item in tickets_for_atoms_raw if isinstance(item, dict)]
        if isinstance(tickets_for_atoms_raw, list)
        else []
    )
    atom_status_meta = _update_atom_actions_from_backlog(
        atom_actions=atom_actions,
        atoms=atoms,
        tickets=tickets_for_atoms,
        generated_at=generated_at,
        backlog_json_path=out_json,
    )
    _write_atom_actions_yaml(atom_actions_path, atom_actions)

    artifacts = summary.get("artifacts")
    artifacts_dict = artifacts if isinstance(artifacts, dict) else {}
    artifacts_dict["atom_actions"] = {
        "path": str(atom_actions_path),
        "meta": atom_status_meta,
    }
    summary["artifacts"] = artifacts_dict

    scope_bits = []
    if target_slug is not None:
        scope_bits.append(f"target={target_slug}")
    if repo_input is not None:
        scope_bits.append(f"repo_input={repo_input}")
    title_suffix = f" ({', '.join(scope_bits)})" if scope_bits else ""

    write_backlog(
        summary,
        out_json_path=out_json,
        out_md_path=out_md,
        title=f"Usertest Backlog{title_suffix}",
    )

    print(str(out_json))
    print(str(out_md))
    print(str(atoms_jsonl))
    print(json.dumps(summary.get("totals", {}), indent=2, ensure_ascii=False))
    print(json.dumps(summary.get("coverage", {}), indent=2, ensure_ascii=False))
    return 0


def _default_triage_output_path(input_json: Path, *, suffix: str) -> Path:
    """Return deterministic default output path for PR triage artifacts.

    Parameters
    ----------
    input_json:
        Source PR-list JSON path.
    suffix:
        Output suffix (for example ``".triage_prs.json"``).

    Returns
    -------
    Path
        Output path in the same directory as the input payload.
    """

    return input_json.with_name(f"{input_json.stem}{suffix}")


def _cmd_triage_backlog(args: argparse.Namespace) -> int:
    """Execute the ``triage-backlog`` command."""

    input_json = args.input_json.resolve()
    if not input_json.exists():
        raise FileNotFoundError(f"Input file not found: {input_json}")

    issues, input_metadata = load_issue_items(input_json)
    report = triage_issues(
        issues,
        group_key=args.group_key,
        dedupe_overall_threshold=float(args.dedupe_overall_threshold),
        theme_overall_threshold=float(args.theme_overall_threshold),
        theme_k=int(args.theme_k),
        theme_representative_threshold=float(args.theme_representative_threshold),
    )
    report["input_json"] = str(input_json)
    if input_metadata:
        report["input_metadata"] = input_metadata

    out_json = (
        args.out_json.resolve()
        if args.out_json is not None
        else _default_triage_output_path(input_json, suffix=".triage_backlog.json")
    )
    out_md = (
        args.out_md.resolve()
        if args.out_md is not None
        else _default_triage_output_path(input_json, suffix=".triage_backlog.md")
    )
    out_xlsx = args.out_xlsx.resolve() if args.out_xlsx is not None else None

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out_md.write_text(
        render_backlog_triage_markdown(report, title="Backlog Triage Report"),
        encoding="utf-8",
    )

    if out_xlsx is not None:
        write_triage_xlsx(report, out_xlsx)

    print(str(out_json))
    print(str(out_md))
    if out_xlsx is not None:
        print(str(out_xlsx))
    print(json.dumps(report.get("totals", {}), indent=2, ensure_ascii=False))
    return 0


def _coerce_pr_items(raw_payload: Any) -> list[dict[str, Any]]:
    """Normalize PR input payload into canonical in-memory records.

    Parameters
    ----------
    raw_payload:
        JSON-decoded payload containing either a list of PR objects or an object
        with a ``pullRequests`` list.

    Returns
    -------
    list[dict[str, Any]]
        Normalized PR records with keys ``number``, ``title``, ``body``, and ``files``.

    Raises
    ------
    ValueError
        Raised when payload is not list-like in the expected shape.
    """

    payload = raw_payload
    if isinstance(payload, dict) and isinstance(payload.get("pullRequests"), list):
        payload = payload.get("pullRequests")
    if not isinstance(payload, list):
        raise ValueError("Expected a JSON list or object containing a pullRequests list.")

    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            continue
        number_raw = item.get("number")
        number: int
        used_number_fallback = False
        if isinstance(number_raw, bool):
            number = idx
            used_number_fallback = True
        elif isinstance(number_raw, int):
            number = number_raw
        elif isinstance(number_raw, float):
            number = int(number_raw)
        elif isinstance(number_raw, str) and number_raw.strip().isdigit():
            number = int(number_raw.strip())
        else:
            number = idx
            used_number_fallback = True

        if used_number_fallback:
            warnings.warn(
                (
                    "PR triage input item is missing a valid `number`; "
                    f"falling back to positional index {idx}."
                ),
                RuntimeWarning,
                stacklevel=2,
            )

        title = _coerce_string(item.get("title"))
        if title is None:
            title = f"PR {number}"
            warnings.warn(
                (
                    "PR triage input item is missing a valid `title`; "
                    f"falling back to {title!r}."
                ),
                RuntimeWarning,
                stacklevel=2,
            )
        body = _coerce_string(item.get("body")) or ""
        files_raw = item.get("files")
        files = [entry for entry in files_raw if isinstance(entry, str) and entry.strip()] if isinstance(files_raw, list) else []

        normalized.append(
            {
                "number": number,
                "title": title,
                "body": body,
                "files": files,
            }
        )
    return normalized


def _render_triage_markdown(doc: dict[str, Any]) -> str:
    """Render PR triage JSON payload into human-readable markdown.

    Parameters
    ----------
    doc:
        Triage document payload emitted by ``_cmd_triage_prs``.

    Returns
    -------
    str
        Markdown report content.
    """

    lines: list[str] = []
    lines.append("# PR Triage Report")
    lines.append("")
    lines.append(f"- Generated: `{doc.get('generated_at', '')}`")
    lines.append(f"- Input: `{doc.get('input_json', '')}`")
    lines.append(f"- PRs: **{int(doc.get('pull_requests_total', 0))}**")
    lines.append(f"- Clusters: **{int(doc.get('clusters_total', 0))}**")
    lines.append("")

    clusters_raw = doc.get("clusters")
    clusters = [item for item in clusters_raw if isinstance(item, dict)] if isinstance(clusters_raw, list) else []
    if not clusters:
        lines.append("No clusters were produced.")
        lines.append("")
        return "\n".join(lines)

    for cluster in clusters:
        cluster_id = int(cluster.get("cluster_id", 0))
        size = int(cluster.get("size", 0))
        score = float(cluster.get("score", 0.0))
        representative = _coerce_string(cluster.get("representative_title")) or "Unknown"
        lines.append(f"## Cluster {cluster_id}")
        lines.append(f"- Size: **{size}**")
        lines.append(f"- Score: **{score:.3f}**")
        lines.append(f"- Representative: {representative}")

        anchors_raw = cluster.get("common_path_anchors")
        anchors = [item for item in anchors_raw if isinstance(item, str)] if isinstance(anchors_raw, list) else []
        if anchors:
            lines.append(f"- Common anchors: {', '.join(f'`{anchor}`' for anchor in anchors)}")

        prs_raw = cluster.get("pull_requests")
        prs = [item for item in prs_raw if isinstance(item, dict)] if isinstance(prs_raw, list) else []
        for pr in prs:
            number = int(pr.get("number", 0))
            title = _coerce_string(pr.get("title")) or "Untitled"
            lines.append(f"- PR #{number}: {title}")
        lines.append("")

    return "\n".join(lines)


def _cmd_triage_prs(args: argparse.Namespace) -> int:
    """Execute the ``triage-prs`` command.

    Parameters
    ----------
    args:
        Parsed argparse namespace for triage command options.

    Returns
    -------
    int
        Process exit code (`0` on success).
    """

    input_json = args.input_json.resolve()
    if not input_json.exists():
        raise FileNotFoundError(f"Input file not found: {input_json}")

    payload = json.loads(input_json.read_text(encoding="utf-8"))
    prs = _coerce_pr_items(payload)

    title_threshold = float(args.title_threshold)
    clusters_idx = cluster_items(
        prs,
        get_title=lambda pr: _coerce_string(pr.get("title")) or "",
        get_text_chunks=lambda pr: [
            _coerce_string(pr.get("title")) or "",
            _coerce_string(pr.get("body")) or "",
            *[item for item in pr.get("files", []) if isinstance(item, str)],
        ],
        title_overlap_threshold=title_threshold,
    )

    clusters: list[dict[str, Any]] = []
    for cluster_id, indexes in enumerate(clusters_idx, start=1):
        members = [prs[idx] for idx in indexes]
        members_sorted = sorted(
            members,
            key=lambda pr: int(pr.get("number", 0)),
        )
        per_pr_anchors = [
            extract_path_anchors_from_chunks(
                [
                    _coerce_string(pr.get("title")) or "",
                    _coerce_string(pr.get("body")) or "",
                    *[item for item in pr.get("files", []) if isinstance(item, str)],
                ]
            )
            for pr in members_sorted
        ]
        common_anchors = (
            sorted(set.intersection(*per_pr_anchors)) if per_pr_anchors else []
        )
        unique_anchors = sorted(set().union(*per_pr_anchors)) if per_pr_anchors else []
        score = float(len(members_sorted)) + math.log1p(float(len(unique_anchors)))

        clusters.append(
            {
                "cluster_id": cluster_id,
                "size": len(members_sorted),
                "score": score,
                "pr_numbers": [int(pr.get("number", 0)) for pr in members_sorted],
                "representative_title": _coerce_string(members_sorted[0].get("title")) or "",
                "common_path_anchors": common_anchors[:12],
                "pull_requests": [
                    {
                        "number": int(pr.get("number", 0)),
                        "title": _coerce_string(pr.get("title")) or "",
                    }
                    for pr in members_sorted
                ],
            }
        )

    clusters.sort(
        key=lambda cluster: (
            -int(cluster.get("size", 0)),
            -float(cluster.get("score", 0.0)),
            min(
                [item for item in cluster.get("pr_numbers", []) if isinstance(item, int)] or [0]
            ),
        )
    )
    for idx, cluster in enumerate(clusters, start=1):
        cluster["cluster_id"] = idx

    out_json = (
        args.out_json.resolve()
        if args.out_json is not None
        else _default_triage_output_path(input_json, suffix=".triage_prs.json")
    )
    out_md = (
        args.out_md.resolve()
        if args.out_md is not None
        else _default_triage_output_path(input_json, suffix=".triage_prs.md")
    )

    doc = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "input_json": str(input_json),
        "title_threshold": title_threshold,
        "pull_requests_total": len(prs),
        "clusters_total": len(clusters),
        "clusters": clusters,
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out_md.write_text(_render_triage_markdown(doc), encoding="utf-8")

    print(str(out_json))
    print(str(out_md))
    return 0


def main(argv: list[str] | None = None) -> None:
    """Run the CLI entrypoint dispatch.

    Parameters
    ----------
    argv:
        Optional command-line argument vector.

    Returns
    -------
    None
        None.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "reports":
        if args.reports_cmd == "compile":
            raise SystemExit(_cmd_reports_compile(args))
        if args.reports_cmd == "analyze":
            raise SystemExit(_cmd_reports_analyze(args))
        if args.reports_cmd == "intent-snapshot":
            raise SystemExit(_cmd_reports_intent_snapshot(args))
        if args.reports_cmd == "review-ux":
            raise SystemExit(_cmd_reports_review_ux(args))
        if args.reports_cmd == "export-tickets":
            raise SystemExit(_cmd_reports_export_tickets(args))
        if args.reports_cmd == "backlog":
            raise SystemExit(_cmd_reports_backlog(args))
        raise SystemExit(2)
    if args.cmd == "triage-prs":
        raise SystemExit(_cmd_triage_prs(args))
    if args.cmd == "triage-backlog":
        raise SystemExit(_cmd_triage_backlog(args))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
