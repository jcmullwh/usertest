#!/usr/bin/env python
"""Continuously refresh, implement, review, and merge blocker/high backlog tickets."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


_SEVERITY_PATTERN = re.compile(r"^- Severity:\s*`?([^`\r\n]+)`?\s*$", re.MULTILINE)
_EXPORT_KIND_PATTERN = re.compile(r"^- Export kind:\s*`?([^`\r\n]+)`?\s*$", re.MULTILINE)
_FINGERPRINT_PATTERN = re.compile(r"^- Fingerprint:\s*`?([^`\r\n]+)`?\s*$", re.MULTILINE)


@dataclass
class LoopContext:
    """Shared runtime configuration for the continuous implementation loop."""

    repo_root: Path
    owner_root: Path
    runs_dir: Path
    target: str
    repo_input: str
    settings_path: Path
    settings_profile: str
    backlog_agent: str
    backlog_model: str | None
    implementation_agent: str
    implementation_model: str | None
    review_agent: str
    review_model: str | None
    allowed_severities: set[str]
    sleep_seconds: float
    cleanup_interval_seconds: float
    log_path: Path
    state_path: Path
    pid_path: Path
    implement_python: Path
    backlog_python: Path
    gh_bin: str = "gh"
    last_cleanup_monotonic: float = field(default=0.0)
    last_refresh_monotonic: float = field(default=0.0)


def _utc_now_z() -> str:
    """Return the current UTC time formatted as an ISO 8601 Z string."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _append_log(ctx: LoopContext, message: str) -> None:
    """Append a single log line to the loop log and stderr."""

    line = f"{_utc_now_z()} {message}"
    ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
    with ctx.log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
    print(line, file=sys.stderr, flush=True)


def _write_state(ctx: LoopContext, **payload: Any) -> None:
    """Persist the current loop state to JSON for external monitoring."""

    ctx.state_path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "schema_version": 1,
        "updated_at_utc": _utc_now_z(),
        "pid": os.getpid(),
        **payload,
    }
    ctx.state_path.write_text(
        json.dumps(body, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _run_logged(
    ctx: LoopContext,
    argv: list[str],
    *,
    cwd: Path,
    label: str,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess while appending its stdout/stderr to the shared loop log."""

    cmd_text = subprocess.list2cmdline(argv)
    _append_log(ctx, f"RUN {label}: {cmd_text}")
    with ctx.log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{_utc_now_z()} BEGIN {label}\n")
        handle.flush()
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            stdout=handle,
            stderr=handle,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
        handle.write(f"{_utc_now_z()} END {label} rc={proc.returncode}\n")
    return proc


def _run_captured(
    ctx: LoopContext,
    argv: list[str],
    *,
    cwd: Path,
    label: str,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and return captured stdout/stderr while logging the command."""

    cmd_text = subprocess.list2cmdline(argv)
    _append_log(ctx, f"RUN {label}: {cmd_text}")
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
    if proc.stdout.strip():
        _append_log(ctx, f"{label} stdout:\n{proc.stdout.rstrip()}")
    if proc.stderr.strip():
        _append_log(ctx, f"{label} stderr:\n{proc.stderr.rstrip()}")
    _append_log(ctx, f"{label} rc={proc.returncode}")
    return proc


def _docker_healthy(ctx: LoopContext) -> bool:
    """Return True when the local Docker daemon is responsive."""

    proc = _run_captured(
        ctx,
        ["docker", "version", "--format", "{{json .}}"],
        cwd=ctx.repo_root,
        label="docker version",
        timeout_seconds=30,
    )
    return proc.returncode == 0


def _maintenance_cleanup_due(ctx: LoopContext) -> bool:
    """Return True when the maintenance-image cleanup interval has elapsed."""

    if ctx.last_cleanup_monotonic <= 0:
        return True
    return (time.monotonic() - ctx.last_cleanup_monotonic) >= ctx.cleanup_interval_seconds


def _run_maintenance_cleanup(ctx: LoopContext) -> None:
    """Run maintenance-image cleanup best-effort and never raise on failure."""

    proc = _run_logged(
        ctx,
        [
            str(ctx.implement_python),
            "-m",
            "usertest_implement.cli",
            "--repo-root",
            str(ctx.repo_root),
            "maintenance-images",
            "cleanup",
            "--timeout-seconds",
            "60",
        ],
        cwd=ctx.repo_root,
        label="maintenance-images cleanup",
        timeout_seconds=120,
    )
    ctx.last_cleanup_monotonic = time.monotonic()
    if proc.returncode != 0:
        _append_log(ctx, "WARNING maintenance image cleanup failed; continuing")


def _load_ledger(ctx: LoopContext) -> dict[str, Any]:
    """Load the implementation attempt ledger from `.agents/state`."""

    ledger_path = ctx.repo_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    if not ledger_path.exists():
        return {"actions": {}}
    raw = yaml.safe_load(ledger_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {"actions": {}}
    actions = raw.get("actions")
    if not isinstance(actions, dict):
        raw["actions"] = {}
    return raw


def _find_ticket_path(ctx: LoopContext, fingerprint: str) -> Path | None:
    """Find the current markdown ticket path for a fingerprint anywhere in `.agents/plans`."""

    plans_root = ctx.owner_root / ".agents" / "plans"
    matches = sorted(plans_root.glob(f"*/*{fingerprint}*.md"))
    return matches[0] if matches else None


def _ticket_metadata(ticket_path: Path) -> dict[str, str | None]:
    """Parse the small metadata header from an exported ticket markdown file."""

    text = ticket_path.read_text(encoding="utf-8")
    severity_match = _SEVERITY_PATTERN.search(text)
    export_kind_match = _EXPORT_KIND_PATTERN.search(text)
    fingerprint_match = _FINGERPRINT_PATTERN.search(text)
    return {
        "severity": severity_match.group(1).strip().lower() if severity_match else None,
        "export_kind": export_kind_match.group(1).strip().lower() if export_kind_match else None,
        "fingerprint": fingerprint_match.group(1).strip() if fingerprint_match else None,
    }


def _list_ready_candidates(ctx: LoopContext) -> list[Path]:
    """List ready implementation tickets restricted to blocker/high severities."""

    ready_dir = ctx.owner_root / ".agents" / "plans" / "2 - ready"
    if not ready_dir.exists():
        return []
    candidates: list[Path] = []
    for ticket_path in sorted(ready_dir.glob("*.md")):
        meta = _ticket_metadata(ticket_path)
        if meta["export_kind"] != "implementation":
            continue
        if meta["severity"] not in ctx.allowed_severities:
            continue
        candidates.append(ticket_path)
    return candidates


def _list_export_candidates(ctx: LoopContext) -> list[Path]:
    """List fresh blocker/high implementation tickets directly from the latest export file."""

    export_path = ctx.runs_dir / ctx.target / "_compiled" / f"{ctx.target}.tickets_export.json"
    if not export_path.exists():
        return []
    try:
        doc = json.loads(export_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    exports = doc.get("exports")
    if not isinstance(exports, list):
        return []
    out: list[Path] = []
    seen: set[str] = set()
    for item in exports:
        if not isinstance(item, dict):
            continue
        if str(item.get("export_kind") or "").strip().lower() != "implementation":
            continue
        source_ticket = item.get("source_ticket")
        if not isinstance(source_ticket, dict):
            continue
        if str(source_ticket.get("stage") or "").strip().lower() != "ready_for_ticket":
            continue
        severity = str(source_ticket.get("severity") or "").strip().lower()
        if severity not in ctx.allowed_severities:
            continue
        owner_repo = item.get("owner_repo")
        if not isinstance(owner_repo, dict):
            continue
        idea_path_raw = owner_repo.get("idea_path")
        if not isinstance(idea_path_raw, str) or not idea_path_raw.strip():
            continue
        idea_path = Path(idea_path_raw)
        if not idea_path.exists():
            continue
        idea_key = str(idea_path.resolve()).lower()
        if idea_key in seen:
            continue
        seen.add(idea_key)
        out.append(idea_path.resolve())
    return sorted(out)


def _gh_pr_view(ctx: LoopContext, pr_url: str) -> dict[str, Any] | None:
    """Return a small PR status payload via GitHub CLI or None on failure."""

    proc = _run_captured(
        ctx,
        [
            ctx.gh_bin,
            "pr",
            "view",
            pr_url,
            "--json",
            "url,state,mergedAt",
        ],
        cwd=ctx.owner_root,
        label=f"gh pr view {pr_url}",
        timeout_seconds=60,
    )
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def _move_ticket(ctx: LoopContext, fingerprint: str, to_bucket: str) -> bool:
    """Move a ticket between plan buckets through the CLI and return success."""

    proc = _run_logged(
        ctx,
        [
            str(ctx.implement_python),
            "-m",
            "usertest_implement.cli",
            "--repo-root",
            str(ctx.repo_root),
            "tickets",
            "move",
            "--owner-root",
            str(ctx.owner_root),
            "--fingerprint",
            fingerprint,
            "--to-bucket",
            to_bucket,
        ],
        cwd=ctx.repo_root,
        label=f"tickets move {fingerprint} -> {to_bucket}",
        timeout_seconds=60,
    )
    return proc.returncode == 0


def _run_review(ctx: LoopContext, fingerprint: str) -> bool:
    """Run the explicit review stage for a PR-backed ticket."""

    if not _docker_healthy(ctx):
        _write_state(
            ctx,
            status="waiting_for_docker",
            current_action="review",
            current_ticket=fingerprint,
        )
        return False
    argv = [
        str(ctx.implement_python),
        "-m",
        "usertest_implement.cli",
        "--repo-root",
        str(ctx.repo_root),
        "review",
        "run",
        "--owner-root",
        str(ctx.owner_root),
        "--fingerprint",
        fingerprint,
        "--agent",
        ctx.review_agent,
    ]
    if ctx.review_model:
        argv.extend(["--model", ctx.review_model])
    proc = _run_logged(
        ctx,
        argv,
        cwd=ctx.repo_root,
        label=f"review run {fingerprint}",
    )
    return proc.returncode == 0


def _merge_review(ctx: LoopContext, fingerprint: str) -> bool:
    """Attempt to merge an approved review ticket."""

    proc = _run_logged(
        ctx,
        [
            str(ctx.implement_python),
            "-m",
            "usertest_implement.cli",
            "--repo-root",
            str(ctx.repo_root),
            "review",
            "merge",
            "--owner-root",
            str(ctx.owner_root),
            "--fingerprint",
            fingerprint,
        ],
        cwd=ctx.repo_root,
        label=f"review merge {fingerprint}",
        timeout_seconds=120,
    )
    return proc.returncode == 0


def _reconcile_review_queue(ctx: LoopContext) -> None:
    """Reconcile ledger-backed PR tickets so closed PRs requeue and approved PRs merge."""

    ledger = _load_ledger(ctx)
    actions = ledger.get("actions", {})
    if not isinstance(actions, dict):
        return
    for fingerprint, entry_raw in sorted(actions.items()):
        if not isinstance(entry_raw, dict):
            continue
        pr_url = entry_raw.get("last_pr_url") or entry_raw.get("last_review_pr_url")
        if not isinstance(pr_url, str) or not pr_url.strip():
            continue
        ticket_path = _find_ticket_path(ctx, fingerprint)
        if ticket_path is None:
            continue
        if (
            ticket_path.parent.name == "5 - complete"
            and entry_raw.get("last_merge_pr_url") == pr_url
            and isinstance(entry_raw.get("last_merged_at"), str)
            and str(entry_raw.get("last_merged_at")).strip()
        ):
            continue
        pr_doc = _gh_pr_view(ctx, pr_url)
        if not isinstance(pr_doc, dict):
            continue
        if pr_doc.get("mergedAt"):
            if ticket_path.parent.name != "5 - complete":
                _move_ticket(ctx, fingerprint, "5 - complete")
            continue
        state = str(pr_doc.get("state") or "").upper()
        if state == "CLOSED":
            if ticket_path.parent.name == "4 - for_review":
                _move_ticket(ctx, fingerprint, "2 - ready")
            continue
        if state != "OPEN":
            continue
        if ticket_path.parent.name != "4 - for_review":
            _move_ticket(ctx, fingerprint, "4 - for_review")
        merge_ready = (
            entry_raw.get("last_review_pr_url") == pr_url
            and entry_raw.get("last_review_merge_ready") is True
        )
        if not merge_ready:
            _run_review(ctx, fingerprint)
            ledger = _load_ledger(ctx)
            entry_raw = (
                ledger.get("actions", {}).get(fingerprint, {})
                if isinstance(ledger.get("actions"), dict)
                else {}
            )
            merge_ready = (
                isinstance(entry_raw, dict)
                and entry_raw.get("last_review_pr_url") == pr_url
                and entry_raw.get("last_review_merge_ready") is True
            )
        if merge_ready:
            _merge_review(ctx, fingerprint)


def _refresh_backlog(ctx: LoopContext) -> bool:
    """Run the full backlog refresh workflow used before ticket implementation."""

    common = [
        str(ctx.backlog_python),
        "-m",
        "usertest_backlog.cli",
        "reports",
    ]
    root_flags = [
        "--repo-root",
        str(ctx.repo_root),
        "--runs-dir",
        str(ctx.runs_dir),
        "--target",
        ctx.target,
    ]
    steps: list[tuple[str, list[str]]] = [
        (
            "reports backlog",
            [
                *common,
                "backlog",
                *root_flags,
                "--repo-input",
                ctx.repo_input,
                "--agent",
                ctx.backlog_agent,
            ],
        ),
        (
            "reports intent-snapshot",
            [*common, "intent-snapshot", *root_flags],
        ),
        (
            "reports review-ux",
            [*common, "review-ux", *root_flags, "--agent", ctx.review_agent],
        ),
        (
            "reports export-tickets",
            [*common, "export-tickets", *root_flags, "--stage", "ready_for_ticket"],
        ),
    ]
    if ctx.backlog_model:
        steps[0][1].extend(["--model", ctx.backlog_model])
    if ctx.review_model:
        steps[2][1].extend(["--model", ctx.review_model])
    _write_state(ctx, status="running", current_action="refresh_backlog")
    for label, argv in steps:
        proc = _run_logged(ctx, argv, cwd=ctx.repo_root, label=label)
        if proc.returncode != 0:
            _append_log(ctx, f"WARNING refresh step failed: {label}")
            return False
    ctx.last_refresh_monotonic = time.monotonic()
    return True


def _run_ticket(ctx: LoopContext, ticket_path: Path) -> bool:
    """Run one implementation ticket through commit/push/PR and automatic review."""

    if not _docker_healthy(ctx):
        _write_state(
            ctx,
            status="waiting_for_docker",
            current_action="implement",
            current_ticket=str(ticket_path),
        )
        return False
    argv = [
        str(ctx.implement_python),
        "-m",
        "usertest_implement.cli",
        "--repo-root",
        str(ctx.repo_root),
        "run",
        "--settings",
        str(ctx.settings_path),
        "--settings-profile",
        ctx.settings_profile,
        "--ticket-path",
        str(ticket_path),
        "--repo",
        str(ctx.repo_root),
        "--ref",
        "dev",
        "--agent",
        ctx.implementation_agent,
    ]
    if ctx.implementation_model:
        argv.extend(["--model", ctx.implementation_model])
    argv.extend(["--implementation-review-agent", ctx.review_agent])
    if ctx.review_model:
        argv.extend(["--implementation-review-model", ctx.review_model])
    _write_state(
        ctx,
        status="running",
        current_action="implement",
        current_ticket=str(ticket_path),
    )
    proc = _run_logged(ctx, argv, cwd=ctx.repo_root, label=f"implement {ticket_path.name}")
    return proc.returncode == 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the continuous implementation loop."""

    parser = argparse.ArgumentParser(
        prog="continuous_implement_loop",
        description="Continuously refresh backlog tickets, implement blocker/high items, and merge approved PRs.",
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--owner-root", type=Path, default=Path.cwd())
    parser.add_argument("--runs-dir", type=Path, default=Path("runs/usertest_implement"))
    parser.add_argument("--target", default="usertest")
    parser.add_argument("--repo-input", default=None)
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path("configs/usertest_implement_settings.yaml"),
    )
    parser.add_argument("--settings-profile", default="default")
    parser.add_argument("--backlog-agent", choices=["claude", "codex", "gemini"], default="codex")
    parser.add_argument("--backlog-model", default="gpt-5.4")
    parser.add_argument("--implementation-agent", choices=["claude", "codex", "gemini"], default="codex")
    parser.add_argument("--implementation-model", default=None)
    parser.add_argument("--review-agent", choices=["claude", "codex", "gemini"], default="claude")
    parser.add_argument("--review-model", default=None)
    parser.add_argument(
        "--severity",
        dest="severities",
        action="append",
        default=["blocker", "high"],
        help="Repeatable severity filter for ready-ticket selection.",
    )
    parser.add_argument("--sleep-seconds", type=float, default=60.0)
    parser.add_argument("--cleanup-interval-seconds", type=float, default=21600.0)
    parser.add_argument(
        "--log-path",
        type=Path,
        default=Path("runs/_continuous_loop/continuous_loop.log"),
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path("runs/_continuous_loop/loop_state.json"),
    )
    parser.add_argument(
        "--pid-path",
        type=Path,
        default=Path("runs/_continuous_loop/loop.pid"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the continuous implementation loop until externally terminated."""

    parser = _build_parser()
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    owner_root = args.owner_root.resolve()
    runs_dir = args.runs_dir if args.runs_dir.is_absolute() else (repo_root / args.runs_dir).resolve()
    settings_path = args.settings if args.settings.is_absolute() else (repo_root / args.settings).resolve()
    ctx = LoopContext(
        repo_root=repo_root,
        owner_root=owner_root,
        runs_dir=runs_dir,
        target=str(args.target),
        repo_input=(
            str(args.repo_input).strip()
            if isinstance(args.repo_input, str) and str(args.repo_input).strip()
            else str(repo_root)
        ),
        settings_path=settings_path,
        settings_profile=str(args.settings_profile),
        backlog_agent=str(args.backlog_agent),
        backlog_model=str(args.backlog_model).strip() if isinstance(args.backlog_model, str) and args.backlog_model.strip() else None,
        implementation_agent=str(args.implementation_agent),
        implementation_model=str(args.implementation_model).strip() if isinstance(args.implementation_model, str) and args.implementation_model and str(args.implementation_model).strip() else None,
        review_agent=str(args.review_agent),
        review_model=str(args.review_model).strip() if isinstance(args.review_model, str) and args.review_model and str(args.review_model).strip() else None,
        allowed_severities={str(value).strip().lower() for value in args.severities if str(value).strip()},
        sleep_seconds=float(args.sleep_seconds),
        cleanup_interval_seconds=float(args.cleanup_interval_seconds),
        log_path=args.log_path if args.log_path.is_absolute() else (repo_root / args.log_path).resolve(),
        state_path=args.state_path if args.state_path.is_absolute() else (repo_root / args.state_path).resolve(),
        pid_path=args.pid_path if args.pid_path.is_absolute() else (repo_root / args.pid_path).resolve(),
        implement_python=(repo_root / "apps" / "usertest_implement" / ".venv" / "Scripts" / "python.exe").resolve(),
        backlog_python=(repo_root / "apps" / "usertest_backlog" / ".venv" / "Scripts" / "python.exe").resolve(),
    )

    ctx.pid_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    _append_log(ctx, "continuous implementation loop starting")

    while True:
        try:
            _write_state(ctx, status="running", current_action="startup")
            if _maintenance_cleanup_due(ctx):
                _run_maintenance_cleanup(ctx)

            _reconcile_review_queue(ctx)

            ready_candidates = _list_ready_candidates(ctx)
            if not ready_candidates:
                _refresh_backlog(ctx)
                _reconcile_review_queue(ctx)
                ready_candidates = _list_ready_candidates(ctx)
            export_candidates = _list_export_candidates(ctx)
            all_candidates: list[Path] = []
            seen_candidates: set[str] = set()
            for candidate in [*ready_candidates, *export_candidates]:
                key = str(candidate.resolve()).lower()
                if key in seen_candidates:
                    continue
                seen_candidates.add(key)
                all_candidates.append(candidate)

            if all_candidates:
                next_ticket = all_candidates[0]
                _append_log(ctx, f"selected ready ticket: {next_ticket}")
                _run_ticket(ctx, next_ticket)
                _reconcile_review_queue(ctx)
                continue

            _write_state(
                ctx,
                status="idle",
                current_action="sleep",
                sleep_seconds=ctx.sleep_seconds,
                ready_candidates=0,
            )
            _append_log(ctx, f"no blocker/high ready tickets; sleeping {ctx.sleep_seconds:.0f}s")
            time.sleep(ctx.sleep_seconds)
        except KeyboardInterrupt:
            _append_log(ctx, "continuous implementation loop interrupted")
            return 130
        except Exception as exc:
            _append_log(ctx, f"UNHANDLED ERROR: {exc!r}")
            _write_state(
                ctx,
                status="error",
                current_action="exception",
                error=repr(exc),
                sleep_seconds=ctx.sleep_seconds,
            )
            time.sleep(ctx.sleep_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
