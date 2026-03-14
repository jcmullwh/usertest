from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


BEGIN_RE = re.compile(r"^BEGIN phase=(?P<phase>\S+) .* workers=(?P<workers>\[.*\])$")
PHASE_RE = re.compile(r"^PHASE (?P<phase>\S+) cycle=(?P<cycle>\d+)$")
WAVE_RE = re.compile(
    r"^WAVE phase=(?P<phase>\S+) cycle=(?P<cycle>\d+) candidates=(?P<candidates>\d+) parallel=(?P<parallel>\d+)$"
)
REFRESH_RE = re.compile(r"^(?P<action>REFRESH|REUSE) source=(?P<source>\S+) (?P<detail>.*)$")
LAUNCH_RE = re.compile(
    r"^LAUNCH source=(?P<source>\S+) fingerprint=(?P<fingerprint>\S+) severity=(?P<severity>\S+) "
    r"worker=(?:slot|worker_index)=(?P<worker_index>\d+) agent=(?P<agent>\S+) model=(?P<model>.+?) ticket_path=(?P<ticket_path>.+)$"
)
SUCCESS_RE = re.compile(
    r"^SUCCESS source=(?P<source>\S+) fingerprint=(?P<fingerprint>\S+) "
    r"worker=(?:slot|worker_index)=(?P<worker_index>\d+) agent=(?P<agent>\S+) model=(?P<model>.+?) "
    r"run_dir=(?P<run_dir>\S+) branch=(?P<branch>\S+) pushed=(?P<pushed>\S+) "
    r"pr_created=(?P<pr_created>\S+) pr_url=(?P<pr_url>.+)$"
)
FAIL_RE = re.compile(
    r"^FAIL source=(?P<source>\S+) fingerprint=(?P<fingerprint>\S+) "
    r"worker=(?:slot|worker_index)=(?P<worker_index>\d+) agent=(?P<agent>\S+) model=(?P<model>.+?) error=(?P<error>.+)$"
)
FAIL_CLAIM_RE = re.compile(
    r"^FAIL claim fingerprint=(?P<fingerprint>\S+) source=(?P<source>\S+) error=(?P<error>.+)$"
)
WORKER_RE = re.compile(
    r"(?:slot|worker_index)=(?P<worker_index>\d+) agent=(?P<agent>\S+) model=(?P<model>[^,'\]]+)"
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _enable_console_backslashreplace(stream: Any) -> None:
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
    _enable_console_backslashreplace(sys.stdout)
    _enable_console_backslashreplace(sys.stderr)


def _write_stdout(text: str) -> None:
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        escaped = text.encode(encoding, errors="backslashreplace").decode(
            encoding, errors="ignore"
        )
        sys.stdout.write(escaped)
        sys.stdout.flush()


_configure_console_output()


def _tail(path: Path | None, limit: int) -> list[str]:
    if path is None or not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except Exception:
        return []


def _latest(log_dir: Path, suffix: str) -> Path | None:
    files = sorted(log_dir.glob(f"backlog_loop_*{suffix}"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _parse_line(line: str) -> tuple[str | None, str]:
    if not line.startswith("["):
        return (None, line.rstrip("\n"))
    close = line.find("]")
    if close <= 1:
        return (None, line.rstrip("\n"))
    return (line[1:close], line[close + 2 :].rstrip("\n"))


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _read_jsonl(path: Path | None, *, limit: int | None = None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            if isinstance(raw, dict):
                rows.append(raw)
    except Exception:
        return []
    return rows[-limit:] if limit is not None else rows


def _latest_batch_dir(repo_root: Path) -> Path | None:
    root = repo_root / "runs" / "_batch" / "usertest_implement"
    if not root.exists():
        return None
    dirs = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.name)
    return dirs[-1] if dirs else None


def _discover_loop_processes() -> list[dict[str, Any]]:
    if subprocess.os.name != "nt":
        return []
    cmd = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "$rows = Get-CimInstance Win32_Process | Where-Object { "
        "$_.CommandLine -like '*backlog_implementation_loop.py*' -or "
        "$_.CommandLine -like '*continuous_implement_loop.py*' -or "
        "$_.CommandLine -like '*continuous_loop_watchdog.ps1*' "
        "} "
        "| Select-Object ProcessId, CommandLine, CreationDate; "
        "$rows | ConvertTo-Json -Depth 3"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        raw = json.loads(proc.stdout)
    except Exception:
        return []
    rows = raw if isinstance(raw, list) else [raw]
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        command_line = str(row.get("CommandLine") or "")
        if "Get-CimInstance Win32_Process" in command_line:
            continue
        out.append(row)
    return out


def parse_batch_stdout(lines: list[str]) -> dict[str, Any]:
    phase = None
    cycle = None
    wave = None
    workers: list[dict[str, Any]] = []
    active: dict[str, dict[str, Any]] = {}
    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    refresh_counts: dict[str, dict[str, int]] = {}
    latest_event: dict[str, Any] | None = None

    def _worker_label(match: re.Match[str]) -> str:
        return (
            f"worker {match.group('worker_index')} - {match.group('agent')} - "
            f"{match.group('model').strip()}"
        )

    for raw in lines:
        timestamp, message = _parse_line(raw)
        latest_event = {"timestamp": timestamp, "message": message}
        if match := BEGIN_RE.match(message):
            phase = match.group("phase")
            workers = [
                {
                    "worker_index": int(item.group("worker_index")),
                    "agent": item.group("agent"),
                    "model": item.group("model").strip(),
                }
                for item in WORKER_RE.finditer(match.group("workers"))
            ]
            continue
        if match := PHASE_RE.match(message):
            phase = match.group("phase")
            cycle = int(match.group("cycle"))
            continue
        if match := WAVE_RE.match(message):
            wave = {
                "phase": match.group("phase"),
                "cycle": int(match.group("cycle")),
                "candidates": int(match.group("candidates")),
                "parallel": int(match.group("parallel")),
            }
            phase = wave["phase"]
            cycle = wave["cycle"]
            continue
        if match := REFRESH_RE.match(message):
            bucket = refresh_counts.setdefault(
                match.group("source"), {"refresh": 0, "reuse": 0}
            )
            bucket[match.group("action").lower()] += 1
            continue
        if match := LAUNCH_RE.match(message):
            active[match.group("fingerprint")] = {
                "timestamp": timestamp,
                "source": match.group("source"),
                "fingerprint": match.group("fingerprint"),
                "severity": match.group("severity"),
                "worker": _worker_label(match),
                "ticket_path": match.group("ticket_path"),
            }
            continue
        if match := SUCCESS_RE.match(message):
            active.pop(match.group("fingerprint"), None)
            successes.append(
                {
                    "timestamp": timestamp,
                    "fingerprint": match.group("fingerprint"),
                    "worker": _worker_label(match),
                    "run_dir": match.group("run_dir"),
                    "branch": match.group("branch"),
                    "pr_url": None
                    if match.group("pr_url").strip() == "None"
                    else match.group("pr_url").strip(),
                }
            )
            continue
        if match := FAIL_RE.match(message):
            active.pop(match.group("fingerprint"), None)
            failures.append(
                {
                    "timestamp": timestamp,
                    "kind": "run_failed",
                    "fingerprint": match.group("fingerprint"),
                    "worker": _worker_label(match),
                    "error": match.group("error"),
                }
            )
            continue
        if match := FAIL_CLAIM_RE.match(message):
            failures.append(
                {
                    "timestamp": timestamp,
                    "kind": "claim_failed",
                    "fingerprint": match.group("fingerprint"),
                    "worker": "-",
                    "error": match.group("error"),
                }
            )

    return {
        "phase": phase,
        "cycle": cycle,
        "wave": wave,
        "workers": workers,
        "refresh_counts": refresh_counts,
        "active_tickets": list(active.values()),
        "recent_successes": successes[-12:],
        "recent_failures": failures[-12:],
        "latest_event": latest_event,
    }


def _recent_runs(runs_root: Path, limit: int = 10) -> list[dict[str, Any]]:
    if not runs_root.exists():
        return []
    reports = sorted(
        runs_root.glob("*/*/*/report.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    rows: list[dict[str, Any]] = []
    for report_path in reports[:limit]:
        report = _read_json(report_path)
        if report is None:
            continue
        run_dir = report_path.parent
        pr_ref = _read_json(run_dir / "pr_ref.json")
        timing = _read_json(run_dir / "timing.json")
        rows.append(
            {
                "updated": datetime.fromtimestamp(
                    report_path.stat().st_mtime, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": report.get("status"),
                "run_dir": str(run_dir),
                "pr_url": pr_ref.get("url") if isinstance(pr_ref, dict) else None,
                "total_seconds": (
                    timing.get("total_wall_seconds") if isinstance(timing, dict) else None
                ),
            }
        )
    return rows


def _empty_parsed_snapshot() -> dict[str, Any]:
    return {
        "phase": None,
        "cycle": None,
        "wave": None,
        "workers": [],
        "refresh_counts": {},
        "active_tickets": [],
        "recent_successes": [],
        "recent_failures": [],
        "latest_event": None,
    }


def _compiled_progress(runs_root: Path) -> dict[str, Any]:
    compiled_root = runs_root / "_compiled"
    stage_files = [
        ("atoms", "usertest.backlog.atoms.jsonl"),
        ("problem_records", "usertest.problem_records.json"),
        ("prioritized_problems", "usertest.prioritized_problems.json"),
        ("research", "usertest.research.json"),
        ("solution_options", "usertest.solution_options.json"),
        ("solution_selection", "usertest.solution_selection.json"),
        ("change_plans", "usertest.change_plans.json"),
        ("backlog", "usertest.backlog.json"),
        ("intent_snapshot", "usertest.intent_snapshot.json"),
        ("ux_review", "usertest.ux_review.json"),
        ("tickets_export", "usertest.tickets_export.json"),
    ]
    rows: list[dict[str, Any]] = []
    for stage, filename in stage_files:
        path = compiled_root / filename
        if not path.exists():
            continue
        rows.append(
            {
                "stage": stage,
                "path": str(path),
                "updated": datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "size": path.stat().st_size,
            }
        )
    rows.sort(key=lambda item: item["updated"], reverse=True)
    latest_stage = rows[0]["stage"] if rows else None
    artifact_rows: list[dict[str, Any]] = []
    artifacts_root = compiled_root / "usertest.backlog_artifacts"
    if artifacts_root.exists():
        for path in sorted(
            artifacts_root.glob("**/*.last_message.txt"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )[:20]:
            group = path.parent.parent.name if path.parent.parent != artifacts_root else path.parent.name
            artifact_rows.append(
                {
                    "stage_group": group,
                    "artifact": path.parent.name,
                    "path": str(path),
                    "updated": datetime.fromtimestamp(
                        path.stat().st_mtime, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )
    active_artifact = artifact_rows[0] if artifact_rows else None
    return {
        "compiled_root": str(compiled_root),
        "latest_stage": latest_stage,
        "latest_artifact": active_artifact,
        "rows": rows[:12],
    }


def build_snapshot(repo_root: Path, log_dir: Path, runs_root: Path) -> dict[str, Any]:
    continuous_log_path = log_dir / "continuous_loop.log"
    watchdog_log_path = log_dir / "watchdog.log"
    loop_state_path = log_dir / "loop_state.json"
    using_continuous_loop = loop_state_path.exists()
    stdout_path = _latest(log_dir, ".stdout.txt")
    stderr_path = _latest(log_dir, ".stderr.txt")
    if stdout_path is None and continuous_log_path.exists():
        stdout_path = continuous_log_path
    if stderr_path is None and watchdog_log_path.exists():
        stderr_path = watchdog_log_path
    stdout_lines = _tail(stdout_path, 1000)
    summary_path = (
        loop_state_path
        if loop_state_path.exists()
        else log_dir / "backlog_implementation_loop.summary.json"
    )
    summary = _read_json(summary_path)
    batch_dir = _latest_batch_dir(repo_root)
    batch_state = _read_json(batch_dir / "batch_state.json" if batch_dir is not None else None)
    batch_blockers = _read_json(batch_dir / "global_blockers.json" if batch_dir is not None else None)
    batch_summary = _read_json(batch_dir / "batch_summary.json" if batch_dir is not None else None)
    ticket_outcomes = _read_jsonl(
        batch_dir / "ticket_outcomes.jsonl" if batch_dir is not None else None,
        limit=100,
    )
    continuous_batch_bridge = using_continuous_loop and isinstance(summary, dict) and (
        str(summary.get("current_action") or "").strip().lower() == "batch"
    )
    if using_continuous_loop and not continuous_batch_bridge:
        batch_state = None
        batch_blockers = None
        batch_summary = None
        ticket_outcomes = []
    has_authoritative_batch = isinstance(batch_state, dict) and bool(batch_state.get("batch_id"))
    if (
        summary is not None
        and stdout_path is not None
        and summary_path.exists()
        and summary_path.stat().st_mtime < stdout_path.stat().st_mtime
    ):
        summary = None
    parsed = _empty_parsed_snapshot() if has_authoritative_batch else parse_batch_stdout(stdout_lines)
    if parsed["latest_event"] is None and stdout_lines:
        timestamp, message = _parse_line(stdout_lines[-1])
        parsed["latest_event"] = {"timestamp": timestamp, "message": message}
    if parsed["phase"] is None and isinstance(summary, dict):
        parsed["phase"] = summary.get("current_action")
    compiled_progress = _compiled_progress(runs_root)
    if parsed["phase"] is None and compiled_progress.get("latest_artifact"):
        parsed["phase"] = compiled_progress["latest_artifact"].get("stage_group")
    return {
        "generated_at": _iso_now(),
        "paths": {
            "stdout": str(stdout_path) if stdout_path else None,
            "stderr": str(stderr_path) if stderr_path else None,
            "summary": str(summary_path),
            "batch_dir": str(batch_dir) if batch_dir else None,
        },
        "processes": _discover_loop_processes(),
        "parsed": parsed,
        "summary": summary,
        "batch_state": batch_state,
        "batch_blockers": batch_blockers,
        "batch_summary": batch_summary,
        "ticket_outcomes": ticket_outcomes,
        "stdout_tail": stdout_lines[-80:],
        "stderr_tail": _tail(stderr_path, 40),
        "recent_runs": _recent_runs(runs_root),
        "compiled_progress": compiled_progress,
        "repo_root": str(repo_root),
    }


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "<div class='empty'>No data.</div>"
    header_html = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    rows_html = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{header_html}</tr></thead><tbody>{rows_html}</tbody></table>"


def render_html_page(snapshot: dict[str, Any], refresh_seconds: int) -> str:
    parsed = snapshot["parsed"]
    batch_state = snapshot.get("batch_state") or {}
    summary = snapshot.get("summary") or {}
    compiled_progress = snapshot.get("compiled_progress") or {}
    batch_status = str(batch_state.get("status") or "").strip().lower()
    status = batch_status or (
        "running" if snapshot["processes"] else ("degraded" if parsed["recent_failures"] else "idle")
    )
    if status not in {"running", "degraded", "idle", "blocked", "completed", "failed"}:
        status = "degraded"
    authoritative_active = batch_state.get("in_flight") if isinstance(batch_state, dict) else None
    active_source = authoritative_active if isinstance(authoritative_active, list) else parsed["active_tickets"]
    active_rows = [
        [
            html.escape(str(row.get("launched_utc") or row.get("timestamp") or "-")),
            html.escape(str(row.get("fingerprint") or "-")),
            html.escape(str(row.get("severity") or "-")),
            html.escape(
                row.get("worker")
                if isinstance(row.get("worker"), str)
                else (
                    f"worker {row.get('worker', {}).get('worker_index', '-')}"
                    f" - {row.get('worker', {}).get('agent', '-')}"
                    f" - {row.get('worker', {}).get('model', '<default>')}"
                )
            ),
            html.escape(str(row.get("ticket_path") or "-")),
        ]
        for row in active_source
    ]
    outcome_rows = snapshot.get("ticket_outcomes") or []
    success_source = [row for row in outcome_rows if row.get("failure", {}).get("failure_class") == "success"]
    failure_source = [row for row in outcome_rows if row.get("failure", {}).get("failure_class") != "success"]
    success_rows = [
        [
            html.escape(str(row.get("completed_utc") or row.get("timestamp") or "-")),
            html.escape(str(row.get("fingerprint") or "-")),
            html.escape(
                f"worker {row.get('worker', {}).get('worker_index', '-')}"
                f" - {row.get('worker', {}).get('agent', '-')}"
                f" - {row.get('worker', {}).get('model', '<default>')}"
                if isinstance(row.get("worker"), dict)
                else str(row.get("worker") or "-")
            ),
            html.escape(str(row.get("run_dir") or "-")),
            html.escape(str((row.get("handoff_summary") or {}).get("pr_url") or row.get("pr_url") or "-")),
        ]
        for row in reversed(success_source[-12:] if success_source else parsed["recent_successes"])
    ]
    failure_rows = [
        [
            html.escape(str(row.get("completed_utc") or row.get("timestamp") or "-")),
            html.escape(str(row.get("failure", {}).get("failure_class") or row.get("kind") or "-")),
            html.escape(str(row.get("fingerprint") or "-")),
            html.escape(
                f"worker {row.get('worker', {}).get('worker_index', '-')}"
                f" - {row.get('worker', {}).get('agent', '-')}"
                f" - {row.get('worker', {}).get('model', '<default>')}"
                if isinstance(row.get("worker"), dict)
                else str(row.get("worker") or "-")
            ),
            html.escape(str(row.get("failure", {}).get("summary") or row.get("error") or "-")),
        ]
        for row in reversed(failure_source[-12:] if failure_source else parsed["recent_failures"])
    ]
    refresh_rows = [
        [
            html.escape(source),
            html.escape(str(values.get("refresh", 0))),
            html.escape(str(values.get("reuse", 0))),
        ]
        for source, values in sorted(parsed["refresh_counts"].items())
    ]
    run_rows = [
        [
            html.escape(row["updated"]),
            html.escape(str(row["status"] or "-")),
            html.escape(row["run_dir"]),
            html.escape(str(row["pr_url"] or "-")),
            html.escape(
                "-"
                if row["total_seconds"] is None
                else f"{float(row['total_seconds']):.1f}s"
            ),
        ]
        for row in snapshot["recent_runs"]
    ]
    workers_source = batch_state.get("workers") if isinstance(batch_state, dict) else None
    workers = ", ".join(
        f"worker {worker['worker_index']} {worker['agent']} ({worker.get('model')})"
        for worker in (workers_source if isinstance(workers_source, list) else parsed["workers"])
    ) or "-"
    pids = ", ".join(str(proc.get("ProcessId")) for proc in snapshot["processes"]) or "none"
    latest = parsed["latest_event"]["message"] if parsed["latest_event"] else "-"
    current_action = str(summary.get("current_action") or batch_state.get("phase") or parsed["phase"] or "-")
    current_stage = str(
        (compiled_progress.get("latest_artifact") or {}).get("stage_group")
        or compiled_progress.get("latest_stage")
        or "-"
    )
    active_artifact = compiled_progress.get("latest_artifact") or {}
    progress_rows = [
        [
            html.escape(str(row.get("stage") or "-")),
            html.escape(str(row.get("updated") or "-")),
            html.escape(str(row.get("size") or "-")),
            html.escape(str(row.get("path") or "-")),
        ]
        for row in compiled_progress.get("rows", [])
    ]
    blockers_payload = snapshot.get("batch_blockers") or {}
    blockers = blockers_payload.get("global_blockers", []) if isinstance(blockers_payload, dict) else []
    blocker_rows = [
        [
            html.escape(str(row.get("created_utc") or "-")),
            html.escape(str(row.get("class") or "-")),
            html.escape(str(row.get("summary") or "-")),
            html.escape(str(row.get("fingerprint") or "-")),
        ]
        for row in reversed(blockers[-12:])
    ]
    stdout_tail = "\n".join(snapshot["stdout_tail"]) or "No stdout output."
    stderr_tail = "\n".join(snapshot["stderr_tail"]) or "No stderr output."
    summary_text = (
        json.dumps(batch_state or snapshot["summary"], indent=2, ensure_ascii=False)
        if (batch_state or snapshot["summary"])
        else "No summary yet."
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="{refresh_seconds}">
  <title>Batch Monitor</title>
  <style>
body{{margin:0;font-family:Georgia,serif;background:#f4efe4;color:#17211d}}
.wrap{{max-width:1360px;margin:0 auto;padding:24px}}
.top,.grid{{display:grid;grid-template-columns:1.2fr 1fr;gap:16px;margin-bottom:16px}}
.panel{{background:#fffdf8;border:1px solid #d9d1c2;border-radius:14px;padding:16px 18px}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:14px}}
.card{{background:#faf6ee;border:1px solid #ddd3c1;border-radius:12px;padding:12px}}
.label{{font:600 11px Consolas,monospace;text-transform:uppercase;letter-spacing:.08em;color:#5c6762}}
.value{{font-size:22px;font-weight:700;margin-top:6px}}
.kv{{margin:0 0 10px}}
.k{{font:600 11px Consolas,monospace;text-transform:uppercase;letter-spacing:.08em;color:#5c6762}}
.v{{font:500 14px Consolas,monospace;word-break:break-word}}
table{{width:100%;border-collapse:collapse}}
th,td{{padding:8px 6px;text-align:left;vertical-align:top;word-break:break-word}}
th{{font:600 11px Consolas,monospace;text-transform:uppercase;letter-spacing:.08em;color:#5c6762;border-bottom:1px solid #ddd3c1}}
td{{border-top:1px solid #ebe2d4}}
.log{{background:#171d1b;color:#eaf2ee;border-radius:12px;padding:14px;font:13px Consolas,monospace;white-space:pre-wrap;max-height:420px;overflow:auto}}
.empty{{color:#5c6762;font-style:italic}}
.badge{{display:inline-block;padding:6px 10px;border-radius:999px;font:700 12px Consolas,monospace;text-transform:uppercase;letter-spacing:.08em}}
.running{{background:#d5efea;color:#0f766e}}
.degraded{{background:#fde7cf;color:#b45309}}
.idle{{background:#e7e5e1;color:#5c6762}}
.blocked{{background:#fee2e2;color:#b91c1c}}
.completed{{background:#dcfce7;color:#166534}}
.failed{{background:#fecaca;color:#991b1b}}
@media (max-width:1100px){{.top,.grid{{grid-template-columns:1fr}} .cards{{grid-template-columns:repeat(2,1fr)}}}}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <section class="panel">
        <div>{html.escape(snapshot["generated_at"])}</div>
        <h1>Backlog Batch Monitor</h1>
        <div class="badge {status}">{status}</div>
        <div class="cards">
          <div class="card"><div class="label">Action</div><div class="value">{html.escape(current_action)}</div></div>
          <div class="card"><div class="label">Stage</div><div class="value">{html.escape(current_stage)}</div></div>
          <div class="card"><div class="label">Cycle</div><div class="value">{html.escape(str(parsed["cycle"] or "-"))}</div></div>
          <div class="card"><div class="label">Active</div><div class="value">{len(active_source)}</div></div>
          <div class="card"><div class="label">Done / Failed</div><div class="value">{len(batch_state.get("completed", [])) if isinstance(batch_state, dict) else len(parsed["recent_successes"])} / {len(batch_state.get("failed", [])) if isinstance(batch_state, dict) else len(parsed["recent_failures"])}</div></div>
        </div>
      </section>
      <section class="panel">
        <div class="kv"><div class="k">Loop Status</div><div class="v">{html.escape(str(summary.get("status") or status))}</div></div>
        <div class="kv"><div class="k">Artifact</div><div class="v">{html.escape(str(active_artifact.get("artifact") or "-"))}</div></div>
        <div class="kv"><div class="k">Artifact Updated</div><div class="v">{html.escape(str(active_artifact.get("updated") or "-"))}</div></div>
        <div class="kv"><div class="k">Workers</div><div class="v">{html.escape(workers)}</div></div>
        <div class="kv"><div class="k">Loop PIDs</div><div class="v">{html.escape(pids)}</div></div>
        <div class="kv"><div class="k">Latest Event</div><div class="v">{html.escape(latest)}</div></div>
        <div class="kv"><div class="k">Batch Dir</div><div class="v">{html.escape(str(snapshot["paths"].get("batch_dir") or "-"))}</div></div>
        <div class="kv"><div class="k">Stdout</div><div class="v">{html.escape(str(snapshot["paths"]["stdout"] or "-"))}</div></div>
        <div class="kv"><div class="k">API</div><div class="v">/api/status</div></div>
      </section>
    </div>
    <div class="grid">
      <section class="panel"><h2>Compiled Progress</h2>{_table(["Stage", "Updated", "Size", "Path"], progress_rows)}</section>
      <section class="panel"><h2>Refresh Activity</h2>{_table(["Source", "Refreshes", "Reuses"], refresh_rows)}</section>
    </div>
    <div class="grid">
      <section class="panel"><h2>Active Tickets</h2>{_table(["Launched", "Fingerprint", "Severity", "Worker", "Ticket"], active_rows)}</section>
      <section class="panel"><h2>Recent Runs</h2>{_table(["Updated", "Status", "Run Dir", "PR", "Total"], run_rows)}</section>
    </div>
    <div class="grid">
      <section class="panel"><h2>Recent Successes</h2>{_table(["Time", "Fingerprint", "Worker", "Run Dir", "PR"], success_rows)}</section>
      <section class="panel"><h2>Recent Failures</h2>{_table(["Time", "Kind", "Fingerprint", "Worker", "Error"], failure_rows)}</section>
    </div>
    <div class="grid">
      <section class="panel"><h2>Global Blockers</h2>{_table(["Time", "Class", "Summary", "Fingerprint"], blocker_rows)}</section>
      <section class="panel"><h2>Summary</h2><div class="log">{html.escape(summary_text)}</div></section>
    </div>
    <div class="grid">
      <section class="panel"><h2>Loop Log Tail</h2><div class="log">{html.escape(stdout_tail)}</div></section>
      <section class="panel"><h2>Watchdog / Stderr Tail</h2><div class="log">{html.escape(stderr_tail)}</div></section>
    </div>
  </div>
</body>
</html>"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--log-dir", type=Path, default=Path("runs/_tmp_backlog_rebuild_logs"))
    parser.add_argument("--runs-root", type=Path, default=Path("runs/usertest_implement/usertest"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--refresh-seconds", type=int, default=5)
    parser.add_argument("--dump-json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    log_dir = (
        (repo_root / args.log_dir).resolve()
        if not args.log_dir.is_absolute()
        else args.log_dir.resolve()
    )
    runs_root = (
        (repo_root / args.runs_root).resolve()
        if not args.runs_root.is_absolute()
        else args.runs_root.resolve()
    )
    if args.dump_json:
        _write_stdout(
            json.dumps(build_snapshot(repo_root, log_dir, runs_root), indent=2, ensure_ascii=False)
            + "\n"
        )
        return 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            snapshot = build_snapshot(repo_root, log_dir, runs_root)
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                payload = render_html_page(snapshot, max(1, int(args.refresh_seconds))).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if path == "/api/status":
                payload = (json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if path == "/healthz":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"ok\n")
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    with ThreadingHTTPServer((args.host, int(args.port)), Handler) as server:
        _write_stdout(
            json.dumps(
                {
                    "url": f"http://{args.host}:{args.port}/",
                    "api": f"http://{args.host}:{args.port}/api/status",
                },
                indent=2,
            )
            + "\n"
        )
        server.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
