#!/usr/bin/env python3
"""
Run Dashboard  –  deep-dive visualisation of everything in runs/.

Generates a self-contained HTML file.  No server needed – open in a browser.

Sections
--------
  Discovery  – backlog tickets, evidence atoms, severity/priority breakdown
  Pipeline   – funnel (commit→PR→CI→review→success), outcome-reason timeline
  Tickets    – per-ticket attempt history, theme keywords, attempt distribution
  Code       – packages/files changed, cumulative code velocity
  Failures   – detailed failure taxonomy, error message browser
  All Runs   – full sortable/filterable table

Usage
-----
    python tools/run_dashboard.py                         # ./runs  →  ./run_dashboard.html
    python tools/run_dashboard.py --open                  # open browser after writing
    python tools/run_dashboard.py --runs-dir /path --output out.html
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── helpers ───────────────────────────────────────────────────────────────────

def _ts(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _jload(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _pkg_prefix(path: str) -> str:
    """'packages/runner_core/src/…' → 'packages/runner_core'."""
    parts = path.replace("\\", "/").split("/")
    top = parts[0] if parts else ""
    if len(parts) >= 2 and top in ("packages", "apps", "scripts", "tools", "configs", "docs"):
        return f"{top}/{parts[1]}"
    return top or "root"


def _outcome_reason(r: dict) -> str:
    et = r.get("err_type") or ""
    if "QuotaExceeded" in et or "quota" in et.lower():
        return "quota_exceeded"
    if "PreflightFailed" in et or "auth_missing" in (r.get("err_msg") or "").lower():
        return "agent_auth"
    if "ExecFailed" in et:
        msg = (r.get("err_msg") or "").lower()
        return "agent_auth" if ("auth" in msg or "token" in msg) else "agent_exec"
    if et == "RuntimeError":
        msg = (r.get("err_msg") or "").lower()
        return "infra_error" if any(k in msg for k in ("docker", "buildx", "container", "pip", "bootstrap")) else "runtime_error"
    if et:
        return "other_error"

    bf = r.get("bf_class") or ""
    if bf == "infra_transient":    return "infra_transient"
    if bf == "registry_or_auth":   return "registry_auth"
    if bf == "verification_control_plane": return "verif_failure"
    if bf == "ticket_regression":  return "ci_failure"
    if bf in ("batch_control_plane",): return "batch_failure"
    if bf == "success":            return "success"

    fs = r.get("final_st")
    if fs == "success":
        return "success"
    if fs == "failure":
        if not r.get("commit_ok"):           return "no_commit"
        if not r.get("pr_ok"):               return "push_failed"
        ci_st, ci_c = r.get("ci_st"), r.get("ci_conc")
        if ci_st == "timed_out":             return "ci_timeout"
        if ci_c == "failure":                return "ci_failure"
        if ci_c == "success":
            rev_err = (r.get("review_error") or "").lower()
            if "not in 4" in rev_err or "cannot be reviewed" in rev_err:
                return "review_not_ready"
            return "review_blocked"
        return "pipeline_other"

    if r.get("exit_code") == 0:   return "success"
    if r.get("exit_code") is not None: return "agent_failed"
    return "unknown"


def _pipeline_stage(r: dict) -> str:
    if r.get("suite") != "usertest_implement":
        return "n/a"
    if r.get("final_st") == "success":        return "success"
    if r.get("review_dec") or r.get("review_ready") is not None: return "reviewed"
    ci = r.get("ci_conc")
    if ci == "success":   return "ci_passed"
    if ci is not None:    return "ci_ran"
    if r.get("pr_ok"):    return "pr_created"
    if r.get("commit_ok"): return "committed"
    if r.get("err_type") or r.get("bf_class"): return "setup_failed"
    return "ran"


# ── per-run extraction ────────────────────────────────────────────────────────

def _extract(run_dir: Path, suite: str, instance: str, ts_str: str,
             ts_dt: datetime, agent: str, attempt: int) -> dict[str, Any]:
    r: dict[str, Any] = {
        "id": f"{suite}/{instance}/{ts_str}/{agent}/{attempt}",
        "suite": suite, "instance": instance,
        "ts": ts_str, "ts_iso": ts_dt.isoformat(),
        "date": ts_dt.strftime("%Y-%m-%d"),
        "agent": agent, "attempt": attempt,
    }

    # run_meta.json
    meta = _jload(run_dir / "run_meta.json")
    if meta:
        p = meta.get("phases") or {}
        r.update(wall_s=meta.get("run_wall_seconds"),
                 setup_s=p.get("setup_seconds"), agent_s=p.get("agent_seconds"),
                 verif_s=p.get("verification_seconds"), post_s=p.get("postprocess_seconds"),
                 verif_reused=bool(p.get("verification_reused")))
    else:
        r.update(wall_s=None, setup_s=None, agent_s=None,
                 verif_s=None, post_s=None, verif_reused=False)

    # metrics.json
    m = _jload(run_dir / "metrics.json")
    if m:
        ec = m.get("event_counts") or {}
        r.update(ev_cmd=ec.get("run_command", 0), ev_read=ec.get("read_file", 0),
                 ev_msg=ec.get("agent_message", 0), ev_write=ec.get("write_file", 0),
                 cmds_ok=m.get("commands_executed", 0), cmds_fail=m.get("commands_failed", 0),
                 lines_add=m.get("lines_added_total", 0), lines_rm=m.get("lines_removed_total", 0),
                 step_count=m.get("step_count", 0),
                 files_w=len(m.get("distinct_files_written") or []),
                 files_r=len(m.get("distinct_files_read") or []))
    else:
        r.update(ev_cmd=0, ev_read=0, ev_msg=0, ev_write=0,
                 cmds_ok=0, cmds_fail=0, lines_add=0, lines_rm=0,
                 step_count=0, files_w=0, files_r=0)

    # agent_attempts.json
    aa = _jload(run_dir / "agent_attempts.json")
    if aa:
        atts = aa.get("attempts") or []
        last = atts[-1] if atts else {}
        v = last.get("verification") or {}
        r.update(exit_code=last.get("exit_code"), fail_sub=last.get("failure_subtype"),
                 verif_ok=v.get("passed"), att_wall_s=last.get("attempt_wall_seconds"),
                 rl_retries=aa.get("rate_limit_retries_used", 0), n_atts=len(atts))
    else:
        r.update(exit_code=None, fail_sub=None, verif_ok=None,
                 att_wall_s=None, rl_retries=0, n_atts=0)

    # handoff_summary.json
    h = _jload(run_dir / "handoff_summary.json")
    if h:
        r.update(final_st=h.get("final_status"), ci_st=h.get("ci_status"),
                 ci_conc=h.get("ci_conclusion"), pr_ok=bool(h.get("pr_created")),
                 pr_url=h.get("pr_url"), commit_ok=bool(h.get("commit_performed")),
                 review_dec=h.get("review_decision"), review_ready=h.get("review_merge_ready"),
                 review_error=h.get("review_error"))
    else:
        r.update(final_st=None, ci_st=None, ci_conc=None, pr_ok=False,
                 pr_url=None, commit_ok=False, review_dec=None,
                 review_ready=None, review_error=None)

    # ticket_ref.json
    t = _jload(run_dir / "ticket_ref.json")
    if t:
        r.update(tick_fp=t.get("fingerprint"), tick_title=t.get("title"))
    else:
        r.update(tick_fp=None, tick_title=None)

    # batch_failure.json
    bf = _jload(run_dir / "batch_failure.json")
    r["bf_class"] = bf.get("failure_class") if bf else None

    # error.json
    e = _jload(run_dir / "error.json")
    if e:
        r.update(err_type=e.get("type"), err_msg=(e.get("message") or "")[:200])
    else:
        r.update(err_type=None, err_msg=None)

    # git_ref.json
    g = _jload(run_dir / "git_ref.json")
    r["branch"] = g.get("branch") if g else None

    # target_ref.json
    tr = _jload(run_dir / "target_ref.json")
    if tr:
        r.update(persona=tr.get("persona_id"), mission=tr.get("mission_id"))
    else:
        r.update(persona=None, mission=None)

    # diff_numstat.json
    dn = _jload(run_dir / "diff_numstat.json")
    if dn and isinstance(dn, list):
        paths = [e["path"] for e in dn if isinstance(e, dict) and e.get("path")]
        r["diff_paths"] = paths
        r["diff_pkgs"]  = list({_pkg_prefix(p) for p in paths})
    else:
        r["diff_paths"] = []
        r["diff_pkgs"]  = []

    # pr_ref.json
    pr = _jload(run_dir / "pr_ref.json")
    if pr:
        r.update(pr_title=pr.get("title"), pr_draft=bool(pr.get("draft")),
                 pr_model=pr.get("model"))
    else:
        r.update(pr_title=None, pr_draft=False, pr_model=None)

    # ci_gate.json
    cig = _jload(run_dir / "ci_gate.json")
    r["ci_run_url"] = cig.get("run_url") if cig else None

    # derived
    r["outcome"]        = _derive_outcome(r)
    r["outcome_reason"] = _outcome_reason(r)
    r["pipeline_stage"] = _pipeline_stage(r)
    return r


def _derive_outcome(r: dict) -> str:
    if r.get("err_type"):          return "error"
    if r.get("bf_class") == "success": return "success"
    if r.get("bf_class"):          return "failure"
    if r.get("final_st") == "success": return "success"
    if r.get("final_st") == "failure": return "failure"
    if r.get("exit_code") == 0:    return "success"
    if r.get("exit_code") is not None: return "failure"
    return "unknown"


# ── directory walk ────────────────────────────────────────────────────────────

def collect_runs(runs_dir: Path) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    if not runs_dir.exists():
        print(f"[warn] runs dir not found: {runs_dir}", file=sys.stderr)
        return runs
    for suite_d in sorted(runs_dir.iterdir()):
        if not suite_d.is_dir() or suite_d.name.startswith("_"):
            continue
        for inst_d in sorted(suite_d.iterdir()):
            if not inst_d.is_dir():
                continue
            for ts_d in sorted(inst_d.iterdir()):
                if not ts_d.is_dir():
                    continue
                dt = _ts(ts_d.name)
                if dt is None:
                    continue
                for ag_d in sorted(ts_d.iterdir()):
                    if not ag_d.is_dir():
                        continue
                    for att_d in sorted(ag_d.iterdir()):
                        if not att_d.is_dir():
                            continue
                        try:
                            n = int(att_d.name)
                        except ValueError:
                            continue
                        runs.append(_extract(att_d, suite_d.name, inst_d.name,
                                             ts_d.name, dt, ag_d.name, n))
    return sorted(runs, key=lambda r: r["ts_iso"])


# ── compiled backlog data ─────────────────────────────────────────────────────

def load_backlog(runs_dir: Path) -> dict[str, Any]:
    bl_path = runs_dir / "usertest_implement" / "usertest" / "_compiled"
    result: dict[str, Any] = {"tickets": [], "totals": {}, "atom_status": {}}

    bl = _jload(bl_path / "usertest.backlog.json")
    if bl:
        raw_tickets = bl.get("tickets") or []
        result["totals"] = bl.get("totals") or {}
        result["tickets"] = [
            {
                "title":          t.get("title", ""),
                "severity":       t.get("severity", ""),
                "confidence":     t.get("confidence"),
                "priority":       (t.get("priority") or {}).get("priority_bucket", ""),
                "component":      t.get("component", ""),
                "stage":          t.get("stage", ""),
                "evidence_count": len(t.get("evidence_atom_ids") or []),
                "problem":        (t.get("problem") or "")[:200],
                "user_impact":    (t.get("user_impact") or "")[:200],
                "proposed_fix":   (t.get("proposed_fix") or "")[:300],
                "user_visible":   (t.get("change_surface") or {}).get("user_visible", False),
                "intent_risk":    t.get("intent_risk", ""),
                "breadth_runs":   (t.get("breadth") or {}).get("runs", 0),
                "breadth_agents": (t.get("breadth") or {}).get("agents", 0),
                "selected_option": (t.get("selected_solution") or {}).get("selected_family_id", ""),
            }
            for t in raw_tickets
        ]

    te = _jload(bl_path / "usertest.tickets_export.json")
    if te:
        stats = te.get("stats") or {}
        result["atom_status"] = (
            (stats.get("atom_status_updates") or {}).get("status_counts") or {}
        )
        result["export_stats"] = {
            k: stats.get(k, 0)
            for k in ("tickets_total", "exports_total", "actioned_total",
                      "skipped_stage", "skipped_actioned")
        }

    return result


# ── HTML template ─────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Run Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg:       #090b12;
  --s1:       #111421;
  --s2:       #181c2b;
  --s3:       #1e2235;
  --border:   #272c42;
  --text:     #dde3f0;
  --muted:    #6b7898;
  --accent:   #6366f1;
  --success:  #22c55e;
  --failure:  #ef4444;
  --warning:  #f59e0b;
  --info:     #38bdf8;
  --c-codex:  #3b82f6;
  --c-claude: #a855f7;
  --c-gemini: #10b981;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,sans-serif;font-size:14px;line-height:1.5}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
code{font-family:ui-monospace,monospace;font-size:11px;background:var(--s3);padding:1px 5px;border-radius:3px}

.topbar{background:var(--s1);border-bottom:1px solid var(--border);padding:0 20px;display:flex;align-items:center;gap:0;position:sticky;top:0;z-index:100}
.topbar h1{font-size:14px;font-weight:700;padding:12px 16px 12px 0;border-right:1px solid var(--border);margin-right:4px;white-space:nowrap}
.topbar h1 span{color:var(--accent)}
.nav-links{display:flex;gap:2px;flex:1}
.nav-link{color:var(--muted);font-size:13px;padding:12px 14px;border-bottom:2px solid transparent;cursor:pointer;transition:color .15s,border-color .15s}
.nav-link:hover{color:var(--text)}
.nav-link.active{color:var(--text);border-bottom-color:var(--accent)}
.topbar-right{margin-left:auto;color:var(--muted);font-size:12px;white-space:nowrap}

.filters{background:var(--s1);border-bottom:1px solid var(--border);padding:8px 20px;display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.filters label{font-size:12px;color:var(--muted);display:flex;align-items:center;gap:6px}
.filters select,.filters input{background:var(--s2);border:1px solid var(--border);color:var(--text);border-radius:5px;padding:4px 8px;font-size:12px;outline:none}
.filters select:focus,.filters input:focus{border-color:var(--accent)}
.filters input{width:200px}
#filter-count{font-size:12px;color:var(--muted);margin-left:auto}
#filter-count strong{color:var(--text)}

main{padding:20px;max-width:1600px;margin:0 auto}
.section{margin-bottom:40px;scroll-margin-top:80px}
.section-header{display:flex;align-items:baseline;gap:12px;margin-bottom:14px;border-left:3px solid var(--accent);padding-left:12px}
.section-header h2{font-size:15px;font-weight:700}
.section-header .hint{font-size:12px;color:var(--muted)}

.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:16px}
.card{background:var(--s1);border:1px solid var(--border);border-radius:8px;padding:14px 16px}
.card .label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:4px}
.card .val{font-size:24px;font-weight:700;line-height:1.1}
.card .sub{font-size:11px;color:var(--muted);margin-top:2px}
.card.green .val{color:var(--success)}.card.red .val{color:var(--failure)}.card.blue .val{color:var(--accent)}.card.amber .val{color:var(--warning)}.card.teal .val{color:var(--c-gemini)}.card.muted .val{color:var(--muted)}

.chart-grid{display:grid;gap:14px;margin-bottom:14px}
.chart-grid.cols2{grid-template-columns:repeat(2,1fr)}
.chart-grid.cols3{grid-template-columns:repeat(3,1fr)}
.chart-grid.cols1{grid-template-columns:1fr}
@media(max-width:900px){.chart-grid.cols2,.chart-grid.cols3{grid-template-columns:1fr}}
.chart-card{background:var(--s1);border:1px solid var(--border);border-radius:8px;padding:16px}
.chart-card h3{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px}
.chart-wrap{position:relative;height:220px}
.chart-wrap.h280{height:280px}.chart-wrap.h340{height:340px}

.tbl-card{background:var(--s1);border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-bottom:14px}
.tbl-hdr{padding:12px 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border)}
.tbl-hdr h3{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.tbl-hdr .tbl-meta{font-size:12px;color:var(--muted)}
.tbl-scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{background:var(--s2);color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.4px;padding:7px 12px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--border);cursor:pointer;user-select:none}
th:hover{color:var(--text)}th.sorted .si{opacity:1}
.si{opacity:.35;margin-left:3px;font-style:normal}
td{padding:7px 12px;border-bottom:1px solid var(--border);vertical-align:middle;white-space:nowrap}
tr:last-child td{border-bottom:none}tr:hover td{background:var(--s2)}
td.mono{font-family:ui-monospace,monospace;font-size:11px;color:var(--muted)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.wrap{white-space:normal;max-width:300px}

.badge{display:inline-flex;align-items:center;padding:1px 7px;border-radius:20px;font-size:11px;font-weight:600;white-space:nowrap}
.b-success{background:rgba(34,197,94,.15);color:#22c55e}.b-failure{background:rgba(239,68,68,.15);color:#ef4444}
.b-error{background:rgba(249,115,22,.15);color:#f97316}.b-warn{background:rgba(245,158,11,.15);color:#f59e0b}
.b-info{background:rgba(56,189,248,.15);color:#38bdf8}.b-neutral{background:rgba(107,120,152,.15);color:#6b7898}
.b-p0{background:rgba(239,68,68,.2);color:#ef4444}.b-p1{background:rgba(245,158,11,.2);color:#f59e0b}.b-p2{background:rgba(99,102,241,.2);color:#6366f1}
.b-blocker{background:rgba(239,68,68,.2);color:#ef4444}.b-high{background:rgba(249,115,22,.2);color:#f97316}
.b-medium{background:rgba(245,158,11,.2);color:#f59e0b}.b-low{background:rgba(56,189,248,.2);color:#38bdf8}
.pill-codex{background:rgba(59,130,246,.2);color:#3b82f6;font-size:11px;font-weight:600;padding:1px 7px;border-radius:4px;display:inline-block}
.pill-claude{background:rgba(168,85,247,.2);color:#a855f7;font-size:11px;font-weight:600;padding:1px 7px;border-radius:4px;display:inline-block}
.pill-gemini{background:rgba(16,185,129,.2);color:#10b981;font-size:11px;font-weight:600;padding:1px 7px;border-radius:4px;display:inline-block}
.no-data{color:var(--muted);text-align:center;padding:30px;font-size:13px}
</style>
</head>
<body>

<div class="topbar">
  <h1>Run <span>Dashboard</span></h1>
  <div class="nav-links">
    <span class="nav-link active" data-sec="discovery">Discovery</span>
    <span class="nav-link" data-sec="pipeline">Pipeline</span>
    <span class="nav-link" data-sec="tickets">Tickets</span>
    <span class="nav-link" data-sec="code">Code</span>
    <span class="nav-link" data-sec="failures">Failures</span>
    <span class="nav-link" data-sec="runs">All Runs</span>
  </div>
  <span class="topbar-right" id="meta-range">—</span>
</div>

<div class="filters">
  <label>Suite
    <select id="f-suite">
      <option value="all">All</option>
      <option value="usertest">usertest</option>
      <option value="usertest_implement">usertest_implement</option>
    </select>
  </label>
  <label>Agent
    <select id="f-agent">
      <option value="all">All agents</option>
      <option value="codex">codex</option>
      <option value="claude">claude</option>
      <option value="gemini">gemini</option>
    </select>
  </label>
  <label>Outcome
    <select id="f-outcome">
      <option value="all">All outcomes</option>
      <option value="success">success</option>
      <option value="failure">failure</option>
      <option value="error">error</option>
    </select>
  </label>
  <label>Search
    <input id="f-search" placeholder="ticket title, fp, agent…">
  </label>
  <span id="filter-count"><strong id="fc-n">—</strong> runs</span>
</div>

<main>

<!-- DISCOVERY -->
<section class="section" id="discovery">
  <div class="section-header">
    <h2>Discovery</h2>
    <span class="hint">Backlog tickets synthesised from run observations · evidence atom ledger</span>
  </div>
  <div class="cards" id="disc-cards"></div>
  <div class="chart-grid cols2">
    <div class="chart-card">
      <h3>Backlog tickets by severity</h3>
      <div class="chart-wrap"><canvas id="ch-sev"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Evidence atom ledger</h3>
      <div class="chart-wrap"><canvas id="ch-atoms"></canvas></div>
    </div>
  </div>
  <div class="chart-grid cols2">
    <div class="chart-card">
      <h3>Ticket component breakdown</h3>
      <div class="chart-wrap h280"><canvas id="ch-components"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Evidence source types (from run observations)</h3>
      <div class="chart-wrap h280"><canvas id="ch-sources"></canvas></div>
    </div>
  </div>
  <div class="tbl-card">
    <div class="tbl-hdr">
      <h3>Backlog Tickets</h3>
      <span class="tbl-meta" id="bl-meta">—</span>
    </div>
    <div class="tbl-scroll">
      <table>
        <thead><tr>
          <th>Pri</th><th>Sev</th><th>Component</th><th>Title</th>
          <th data-col="confidence">Conf <i class="si">↕</i></th>
          <th data-col="evidence_count">Evidence <i class="si">↕</i></th>
          <th data-col="breadth_runs">Runs <i class="si">↕</i></th>
          <th>User visible</th><th>Risk</th>
        </tr></thead>
        <tbody id="bl-body"></tbody>
      </table>
    </div>
  </div>
</section>

<!-- PIPELINE -->
<section class="section" id="pipeline">
  <div class="section-header">
    <h2>Implementation Pipeline</h2>
    <span class="hint">Implement runs only · where work falls off the funnel</span>
  </div>
  <div class="cards" id="pipe-cards"></div>
  <div class="chart-grid cols1">
    <div class="chart-card">
      <h3>Pipeline funnel — commit → PR → CI → reviewed → success</h3>
      <div class="chart-wrap"><canvas id="ch-funnel"></canvas></div>
    </div>
  </div>
  <div class="chart-grid cols2">
    <div class="chart-card">
      <h3>Daily runs — stacked by outcome reason</h3>
      <div class="chart-wrap h280"><canvas id="ch-timeline"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Outcome reason breakdown</h3>
      <div class="chart-wrap h280"><canvas id="ch-reasons"></canvas></div>
    </div>
  </div>
  <div class="chart-grid cols2">
    <div class="chart-card">
      <h3>CI pass rate over time (implement runs, weekly bins)</h3>
      <div class="chart-wrap"><canvas id="ch-ci-trend"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Avg phase duration (minutes) by agent</h3>
      <div class="chart-wrap"><canvas id="ch-phases"></canvas></div>
    </div>
  </div>
</section>

<!-- TICKETS -->
<section class="section" id="tickets">
  <div class="section-header">
    <h2>Ticket Progress</h2>
    <span class="hint">One row per unique ticket fingerprint across all runs</span>
  </div>
  <div class="chart-grid cols2">
    <div class="chart-card">
      <h3>Attempts needed per ticket (implement runs)</h3>
      <div class="chart-wrap"><canvas id="ch-attempts"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Top keywords in ticket titles</h3>
      <div class="chart-wrap"><canvas id="ch-keywords"></canvas></div>
    </div>
  </div>
  <div class="tbl-card">
    <div class="tbl-hdr">
      <h3>Ticket Explorer</h3>
      <span class="tbl-meta" id="tick-meta">—</span>
    </div>
    <div class="tbl-scroll">
      <table>
        <thead><tr>
          <th>FP</th><th>Title</th>
          <th data-col="attempts">Tries <i class="si">↕</i></th>
          <th>Agents</th><th>Best stage</th>
          <th data-col="lines_add">Lines+ <i class="si">↕</i></th>
          <th>PR</th><th>CI</th>
          <th data-col="first_run">First <i class="si">↕</i></th>
          <th data-col="last_run">Last <i class="si">↕</i></th>
        </tr></thead>
        <tbody id="tick-body"></tbody>
      </table>
    </div>
  </div>
</section>

<!-- CODE -->
<section class="section" id="code">
  <div class="section-header">
    <h2>Code Surface</h2>
    <span class="hint">Derived from diff_numstat · implement runs only</span>
  </div>
  <div class="chart-grid cols2">
    <div class="chart-card">
      <h3>Packages modified (distinct runs touching each)</h3>
      <div class="chart-wrap h340"><canvas id="ch-pkgs"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Top files modified</h3>
      <div class="chart-wrap h340"><canvas id="ch-files"></canvas></div>
    </div>
  </div>
  <div class="chart-grid cols2">
    <div class="chart-card">
      <h3>Cumulative lines added — by agent (implement runs)</h3>
      <div class="chart-wrap"><canvas id="ch-velocity"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Lines added per run — distribution</h3>
      <div class="chart-wrap"><canvas id="ch-lines-dist"></canvas></div>
    </div>
  </div>
</section>

<!-- FAILURES -->
<section class="section" id="failures">
  <div class="section-header">
    <h2>Failure Analysis</h2>
    <span class="hint">All suites · detailed root-cause taxonomy · error log</span>
  </div>
  <div class="chart-grid cols2">
    <div class="chart-card">
      <h3>Failure root causes (all runs)</h3>
      <div class="chart-wrap h280"><canvas id="ch-fail-tax"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>Infrastructure failures over time</h3>
      <div class="chart-wrap h280"><canvas id="ch-infra-trend"></canvas></div>
    </div>
  </div>
  <div class="tbl-card">
    <div class="tbl-hdr">
      <h3>Error / Failure Log</h3>
      <span class="tbl-meta" id="err-meta">—</span>
    </div>
    <div class="tbl-scroll">
      <table>
        <thead><tr>
          <th>Date</th><th>Suite</th><th>Agent</th><th>Reason</th>
          <th>Error type</th><th>Batch failure</th>
          <th>Message / context</th><th>CI</th><th>Review issue</th>
        </tr></thead>
        <tbody id="err-body"></tbody>
      </table>
    </div>
  </div>
</section>

<!-- ALL RUNS -->
<section class="section" id="runs">
  <div class="section-header">
    <h2>All Runs</h2>
    <span class="hint">Sortable · filterable · max 500 shown</span>
  </div>
  <div class="tbl-card">
    <div class="tbl-hdr">
      <h3>Runs</h3>
      <span class="tbl-meta" id="runs-meta">—</span>
    </div>
    <div class="tbl-scroll">
      <table>
        <thead><tr>
          <th data-col="date">Date <i class="si">↕</i></th>
          <th data-col="suite">Suite <i class="si">↕</i></th>
          <th data-col="agent">Agent <i class="si">↕</i></th>
          <th data-col="tick_title">Ticket <i class="si">↕</i></th>
          <th data-col="outcome_reason">Reason <i class="si">↕</i></th>
          <th data-col="wall_s" class="sorted">Dur <i class="si">↓</i></th>
          <th data-col="step_count">Steps <i class="si">↕</i></th>
          <th data-col="lines_add">Lines+ <i class="si">↕</i></th>
          <th data-col="lines_rm">Lines- <i class="si">↕</i></th>
          <th data-col="ci_conc">CI <i class="si">↕</i></th>
          <th>PR</th>
          <th data-col="outcome">Outcome <i class="si">↕</i></th>
        </tr></thead>
        <tbody id="runs-body"></tbody>
      </table>
    </div>
  </div>
</section>

</main>

<script>
const RUNS    = __RUNS_DATA__;
const BACKLOG = __BACKLOG_DATA__;

const OR = {
  success:          {bg:'rgba(34,197,94,.75)',   lb:'Success'},
  no_commit:        {bg:'rgba(100,116,139,.7)',  lb:'No commit (nothing to do)'},
  ci_failure:       {bg:'rgba(239,68,68,.75)',   lb:'CI failure'},
  ci_timeout:       {bg:'rgba(248,113,113,.7)',  lb:'CI timeout'},
  infra_error:      {bg:'rgba(249,115,22,.8)',   lb:'Infra error'},
  infra_transient:  {bg:'rgba(251,146,60,.7)',   lb:'Infra transient'},
  registry_auth:    {bg:'rgba(232,121,249,.75)', lb:'Registry/auth'},
  agent_auth:       {bg:'rgba(244,63,94,.75)',   lb:'Agent auth'},
  agent_exec:       {bg:'rgba(220,38,38,.75)',   lb:'Agent exec failed'},
  agent_failed:     {bg:'rgba(185,28,28,.7)',    lb:'Agent failed'},
  review_blocked:   {bg:'rgba(99,102,241,.75)',  lb:'Review blocked'},
  review_not_ready: {bg:'rgba(129,140,248,.7)',  lb:'Review not ready'},
  push_failed:      {bg:'rgba(75,85,99,.75)',    lb:'Push failed'},
  verif_failure:    {bg:'rgba(168,85,247,.75)',  lb:'Verification failure'},
  batch_failure:    {bg:'rgba(217,119,6,.75)',   lb:'Batch ctrl failure'},
  quota_exceeded:   {bg:'rgba(245,158,11,.8)',   lb:'Quota exceeded'},
  runtime_error:    {bg:'rgba(194,65,12,.75)',   lb:'Runtime error'},
  pipeline_other:   {bg:'rgba(120,53,15,.7)',    lb:'Pipeline other'},
  other_error:      {bg:'rgba(153,27,27,.7)',    lb:'Other error'},
  unknown:          {bg:'rgba(55,65,81,.6)',     lb:'Unknown'},
};
const AC = {
  codex: {bg:'rgba(59,130,246,.75)',  ln:'#3b82f6'},
  claude:{bg:'rgba(168,85,247,.75)',  ln:'#a855f7'},
  gemini:{bg:'rgba(16,185,129,.75)',  ln:'#10b981'},
};
const SX  = {ticks:{color:'#6b7898'},grid:{color:'#1e2235'}};
const SXnr= {...SX,grid:{display:false}};
Chart.defaults.color='#6b7898';
Chart.defaults.borderColor='#1e2235';

const groupBy=(a,fn)=>{const m={};for(const x of a){const k=fn(x);(m[k]??=[]).push(x);}return m;};
const countBy=(a,fn)=>{const m={};for(const x of a){const k=fn(x);m[k]=(m[k]??0)+1;}return m;};
const avg=(a,fn)=>{const v=a.map(fn).filter(x=>x!=null&&!isNaN(x));return v.length?v.reduce((s,x)=>s+x,0)/v.length:0;};
const fmtDur=s=>{if(s==null)return'—';const m=s/60;return m<60?`${Math.round(m)}m`:`${(m/60).toFixed(1)}h`;};
const esc=s=>String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const set=(id,v)=>{const el=document.getElementById(id);if(el)el.textContent=v;};
const pct=(n,d)=>d?Math.round(100*n/d)+'%':'—';

const ST={suite:'all',agent:'all',outcome:'all',search:'',sortCol:'ts_iso',sortDir:-1};
function filt(){
  let r=RUNS;
  if(ST.suite!=='all')   r=r.filter(x=>x.suite===ST.suite);
  if(ST.agent!=='all')   r=r.filter(x=>x.agent===ST.agent);
  if(ST.outcome!=='all') r=r.filter(x=>x.outcome===ST.outcome);
  if(ST.search){const q=ST.search.toLowerCase();r=r.filter(x=>(x.tick_title||'').toLowerCase().includes(q)||(x.tick_fp||'').toLowerCase().includes(q)||(x.agent||'').toLowerCase().includes(q)||(x.err_type||'').toLowerCase().includes(q));}
  return r;
}

const CH={};
function mk(id,cfg){if(CH[id])CH[id].destroy();CH[id]=new Chart(document.getElementById(id).getContext('2d'),cfg);}

// ── Discovery (static – uses BACKLOG, not filtered runs) ──────────────────
function renderDiscovery(){
  const bl=BACKLOG;
  const tickets=bl.tickets||[];
  const totals=bl.totals||{};
  const atomSt=bl.atom_status||{};
  const sevCounts=countBy(tickets,t=>t.severity);
  const p0=tickets.filter(t=>t.priority==='p0').length;
  const totalAtoms=Object.values(atomSt).reduce((s,v)=>s+v,0);
  document.getElementById('disc-cards').innerHTML=`
    <div class="card blue"><div class="label">Backlog tickets</div><div class="val">${tickets.length}</div><div class="sub">${totals.runs||0} runs analysed</div></div>
    <div class="card red"><div class="label">P0 / blockers</div><div class="val">${p0}</div><div class="sub">${sevCounts.blocker||0} blocker severity</div></div>
    <div class="card amber"><div class="label">Atoms (total)</div><div class="val">${totalAtoms.toLocaleString()}</div><div class="sub">${atomSt.actioned||0} actioned</div></div>
    <div class="card teal"><div class="label">Ticketed</div><div class="val">${atomSt.ticketed||0}</div><div class="sub">${atomSt.queued||0} queued</div></div>
    <div class="card muted"><div class="label">Evidence types</div><div class="val">${Object.keys(totals.source_counts||{}).length}</div><div class="sub">${totals.atoms||0} observations</div></div>
  `;

  const sevOrder=['blocker','high','medium','low'];
  const sevColors={blocker:'rgba(239,68,68,.8)',high:'rgba(249,115,22,.8)',medium:'rgba(245,158,11,.8)',low:'rgba(56,189,248,.7)'};
  mk('ch-sev',{type:'bar',data:{labels:sevOrder,datasets:[{data:sevOrder.map(s=>sevCounts[s]||0),backgroundColor:sevOrder.map(s=>sevColors[s]),borderRadius:5}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:SX,y:SX}}});

  const asL=Object.keys(atomSt);
  const asC={actioned:'rgba(34,197,94,.8)',ticketed:'rgba(99,102,241,.8)',queued:'rgba(245,158,11,.8)',new:'rgba(55,65,81,.6)'};
  mk('ch-atoms',{type:'doughnut',data:{labels:asL,datasets:[{data:asL.map(k=>atomSt[k]),backgroundColor:asL.map(k=>asC[k]||'rgba(99,102,241,.6)'),borderWidth:0,hoverOffset:5}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:'right',labels:{color:'#6b7898',padding:12,boxWidth:12}}}}});

  const compCnt=countBy(tickets,t=>t.component||'unknown');
  const compS=Object.entries(compCnt).sort((a,b)=>b[1]-a[1]);
  mk('ch-components',{type:'bar',data:{labels:compS.map(([k])=>k),datasets:[{data:compS.map(([,v])=>v),backgroundColor:'rgba(99,102,241,.7)',borderRadius:3}]},options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:SX,y:{...SXnr,ticks:{color:'#dde3f0',font:{size:11}}}}}});

  const sc=totals.source_counts||{};
  const scS=Object.entries(sc).sort((a,b)=>b[1]-a[1]);
  mk('ch-sources',{type:'bar',data:{labels:scS.map(([k])=>k.replace(/_/g,' ')),datasets:[{data:scS.map(([,v])=>v),backgroundColor:'rgba(16,185,129,.7)',borderRadius:3}]},options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:SX,y:{...SXnr,ticks:{color:'#dde3f0',font:{size:11}}}}}});

  const sorted=[...tickets].sort((a,b)=>{const po={p0:0,p1:1,p2:2},so={blocker:0,high:1,medium:2,low:3};return(po[a.priority]??9)-(po[b.priority]??9)||(so[a.severity]??9)-(so[b.severity]??9);});
  set('bl-meta',sorted.length+' tickets');
  document.getElementById('bl-body').innerHTML=sorted.map(t=>{
    const pb=t.priority?`<span class="badge b-${t.priority}">${t.priority.toUpperCase()}</span>`:'';
    const sb=t.severity?`<span class="badge b-${t.severity}">${t.severity}</span>`:'';
    const vis=t.user_visible?'<span class="badge b-warn">yes</span>':'<span class="badge b-neutral">no</span>';
    const risk=t.intent_risk?`<span class="badge b-neutral">${t.intent_risk}</span>`:'';
    const conf=t.confidence!=null?Math.round(t.confidence*100)+'%':'—';
    const title=esc((t.title||'').slice(0,70))+(t.title&&t.title.length>70?'…':'');
    return `<tr><td>${pb}</td><td>${sb}</td><td class="mono">${esc(t.component)}</td>
      <td class="wrap" style="max-width:320px" title="${esc(t.problem)}">${title}</td>
      <td class="num">${conf}</td><td class="num">${t.evidence_count}</td>
      <td class="num">${t.breadth_runs}</td><td>${vis}</td><td>${risk}</td></tr>`;
  }).join('');
}

// ── Pipeline ──────────────────────────────────────────────────────────────
function renderPipeline(runs){
  const impl=runs.filter(r=>r.suite==='usertest_implement');
  const n=impl.length||1;
  const committed =impl.filter(r=>r.commit_ok).length;
  const prCreated =impl.filter(r=>r.pr_ok).length;
  const ciPassed  =impl.filter(r=>r.ci_conc==='success').length;
  const reviewed  =impl.filter(r=>r.review_dec!=null||r.review_ready!=null).length;
  const success   =impl.filter(r=>r.final_st==='success').length;

  document.getElementById('pipe-cards').innerHTML=`
    <div class="card blue"><div class="label">Implement runs</div><div class="val">${impl.length}</div><div class="sub">this filter</div></div>
    <div class="card teal"><div class="label">Committed</div><div class="val">${pct(committed,n)}</div><div class="sub">${committed} runs</div></div>
    <div class="card teal"><div class="label">PR created</div><div class="val">${pct(prCreated,n)}</div><div class="sub">${prCreated} runs</div></div>
    <div class="card green"><div class="label">CI passed</div><div class="val">${pct(ciPassed,n)}</div><div class="sub">${ciPassed} runs</div></div>
    <div class="card green"><div class="label">Success</div><div class="val">${pct(success,n)}</div><div class="sub">${success} runs</div></div>
  `;

  const fColors=['rgba(99,102,241,.8)','rgba(56,189,248,.8)','rgba(16,185,129,.8)','rgba(34,197,94,.8)','rgba(245,158,11,.8)','rgba(34,197,94,1)'];
  mk('ch-funnel',{type:'bar',data:{labels:['Ran','Committed','PR Created','CI Passed','Reviewed','Success'],datasets:[{data:[impl.length,committed,prCreated,ciPassed,reviewed,success],backgroundColor:fColors,borderRadius:4}]},options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{callbacks:{label:ctx=>`${ctx.raw} (${pct(ctx.raw,impl.length)} of implement runs)`}}},scales:{x:{...SX,max:impl.length},y:{...SXnr,ticks:{color:'#dde3f0',font:{size:12}}}}}});

  const byDate=groupBy(runs,r=>r.date);
  const dates=Object.keys(byDate).sort();
  const orKeys=Object.keys(OR);
  const ds=orKeys.filter(k=>dates.some(d=>(byDate[d]||[]).some(r=>r.outcome_reason===k))).map(k=>({label:OR[k].lb,data:dates.map(d=>(byDate[d]||[]).filter(r=>r.outcome_reason===k).length),backgroundColor:OR[k].bg,stack:'s',borderRadius:1}));
  mk('ch-timeline',{type:'bar',data:{labels:dates,datasets:ds},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#6b7898',boxWidth:10,padding:8,font:{size:10}}}},scales:{x:{...SX,stacked:true,ticks:{...SX.ticks,maxRotation:45}},y:{...SX,stacked:true}}}});

  const orCnt=countBy(runs,r=>r.outcome_reason);
  const orS=Object.entries(orCnt).sort((a,b)=>b[1]-a[1]);
  mk('ch-reasons',{type:'bar',data:{labels:orS.map(([k])=>OR[k]?.lb||k),datasets:[{data:orS.map(([,v])=>v),backgroundColor:orS.map(([k])=>OR[k]?.bg||'rgba(99,102,241,.7)'),borderRadius:3}]},options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:SX,y:{...SXnr,ticks:{color:'#dde3f0',font:{size:11}}}}}});

  const implWithCI=impl.filter(r=>r.ci_conc!=null);
  const byWeek=groupBy(implWithCI,r=>{const d=new Date(r.date);const y=d.getFullYear();const w=Math.ceil(d.getDate()/7);return`${y}-${String(d.getMonth()+1).padStart(2,'0')}-W${w}`;});
  const weeks=Object.keys(byWeek).sort();
  mk('ch-ci-trend',{type:'line',data:{labels:weeks,datasets:[{label:'CI pass rate %',data:weeks.map(w=>{const wr=byWeek[w];return Math.round(100*wr.filter(r=>r.ci_conc==='success').length/wr.length);}),borderColor:'#22c55e',backgroundColor:'rgba(34,197,94,.1)',fill:true,tension:0.3,pointRadius:3}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:SX,y:{...SX,min:0,max:100,ticks:{...SX.ticks,callback:v=>v+'%'}}}}});

  const withMeta=runs.filter(r=>r.agent_s!=null);
  const agents=[...new Set(withMeta.map(r=>r.agent))].sort();
  const byAg=groupBy(withMeta,r=>r.agent);
  const mAvg=key=>agents.map(ag=>{const v=(byAg[ag]||[]).map(r=>r[key]).filter(x=>x!=null);return v.length?v.reduce((s,x)=>s+x,0)/v.length/60:0;});
  mk('ch-phases',{type:'bar',data:{labels:agents,datasets:[
    {label:'Setup',       data:mAvg('setup_s'),backgroundColor:'rgba(59,130,246,.8)',stack:'p',borderRadius:2},
    {label:'Agent',       data:mAvg('agent_s'),backgroundColor:'rgba(168,85,247,.8)',stack:'p',borderRadius:2},
    {label:'Verification',data:mAvg('verif_s'),backgroundColor:'rgba(16,185,129,.8)',stack:'p',borderRadius:2},
    {label:'Postprocess', data:mAvg('post_s'), backgroundColor:'rgba(245,158,11,.5)',stack:'p',borderRadius:2},
  ]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#6b7898',boxWidth:10,padding:8}}},scales:{x:{...SX,stacked:true},y:{...SX,stacked:true,ticks:{...SX.ticks,callback:v=>Math.round(v)+'m'},title:{display:true,text:'avg minutes',color:'#6b7898'}}}}});
}

// ── Tickets ────────────────────────────────────────────────────────────────
function renderTickets(runs){
  const impl=runs.filter(r=>r.suite==='usertest_implement'&&r.tick_fp);
  const byFP=groupBy(impl,r=>r.tick_fp);

  const attCounts=Object.values(byFP).map(tr=>tr.length);
  const maxA=Math.max(...attCounts,1);
  const dist=Array.from({length:maxA},(_,i)=>attCounts.filter(n=>n===i+1).length);
  mk('ch-attempts',{type:'bar',data:{labels:Array.from({length:maxA},(_,i)=>`${i+1}×`),datasets:[{data:dist,backgroundColor:'rgba(99,102,241,.75)',borderRadius:4}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:SX,y:{...SX,ticks:{...SX.ticks,stepSize:1},title:{display:true,text:'# tickets',color:'#6b7898'}}}}});

  const STOP=new Set(['and','the','for','with','that','this','from','are','not','add','fix','of','to','a','in','on','at','by','an','as','be','is','or','it','its','via','per','all','use','run','new','make','into','when','each','same','also','more','only','then']);
  const wc={};
  for(const r of impl) if(r.tick_title) for(const w of r.tick_title.toLowerCase().split(/\W+/).filter(w=>w.length>3&&!STOP.has(w))){wc[w]=(wc[w]||0)+1;}
  const topW=Object.entries(wc).sort((a,b)=>b[1]-a[1]).slice(0,20);
  mk('ch-keywords',{type:'bar',data:{labels:topW.map(([w])=>w),datasets:[{data:topW.map(([,v])=>v),backgroundColor:'rgba(56,189,248,.7)',borderRadius:3}]},options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:SX,y:{...SXnr,ticks:{color:'#dde3f0',font:{size:11}}}}}});

  const stageColor={success:'b-success',ci_passed:'b-info',pr_created:'b-warn',committed:'b-neutral',ran:'b-neutral',setup_failed:'b-failure'};
  const tickets=Object.entries(byFP).map(([fp,trs])=>{
    const sorted_=trs.sort((a,b)=>a.ts_iso.localeCompare(b.ts_iso));
    const agents=[...new Set(trs.map(r=>r.agent))];
    const bestStage=trs.some(r=>r.final_st==='success')?'success':trs.some(r=>r.ci_conc==='success')?'ci_passed':trs.some(r=>r.pr_ok)?'pr_created':trs.some(r=>r.commit_ok)?'committed':'ran';
    return {fp,title:sorted_[0].tick_title,attempts:trs.length,agents,bestStage,prUrl:trs.find(r=>r.pr_url)?.pr_url,ciConc:trs.find(r=>r.ci_conc)?.ci_conc,lines_add:trs.reduce((s,r)=>s+(r.lines_add||0),0),first_run:sorted_[0].date,last_run:sorted_[sorted_.length-1].date};
  }).sort((a,b)=>b.last_run.localeCompare(a.last_run));

  set('tick-meta',tickets.length+' unique tickets');
  document.getElementById('tick-body').innerHTML=tickets.map(t=>{
    const agP=t.agents.map(a=>`<span class="pill-${a}">${a}</span>`).join(' ');
    const stB=`<span class="badge ${stageColor[t.bestStage]||'b-neutral'}">${t.bestStage.replace(/_/g,' ')}</span>`;
    const ciB=t.ciConc?`<span class="badge ${t.ciConc==='success'?'b-success':'b-failure'}">${t.ciConc}</span>`:'—';
    const pr=t.prUrl?`<a href="${esc(t.prUrl)}" target="_blank">↗ PR</a>`:'—';
    const tit=esc((t.title||'').slice(0,65))+(t.title&&t.title.length>65?'…':'');
    return `<tr><td class="mono">${esc(t.fp.slice(0,8))}</td>
      <td class="wrap" style="max-width:280px" title="${esc(t.title||'')}">${tit}</td>
      <td class="num">${t.attempts}</td><td>${agP}</td><td>${stB}</td>
      <td class="num" style="color:#22c55e">+${t.lines_add.toLocaleString()}</td>
      <td>${pr}</td><td>${ciB}</td>
      <td class="mono">${t.first_run}</td><td class="mono">${t.last_run}</td></tr>`;
  }).join('');
}

// ── Code ──────────────────────────────────────────────────────────────────
function renderCode(runs){
  const impl=runs.filter(r=>r.suite==='usertest_implement');
  const pkgCnt=countBy(impl.flatMap(r=>r.diff_pkgs||[]),p=>p);
  const pkgS=Object.entries(pkgCnt).sort((a,b)=>b[1]-a[1]).slice(0,18);
  mk('ch-pkgs',{type:'bar',data:{labels:pkgS.map(([k])=>k),datasets:[{data:pkgS.map(([,v])=>v),backgroundColor:'rgba(16,185,129,.7)',borderRadius:3}]},options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:SX,y:{...SXnr,ticks:{color:'#dde3f0',font:{size:11}}}}}});

  const fileCnt=countBy(impl.flatMap(r=>r.diff_paths||[]),f=>f);
  const fileS=Object.entries(fileCnt).sort((a,b)=>b[1]-a[1]).slice(0,18);
  const fileLbls=fileS.map(([f])=>{const p=f.replace(/\\/g,'/').split('/');return p.slice(-2).join('/');});
  mk('ch-files',{type:'bar',data:{labels:fileLbls,datasets:[{data:fileS.map(([,v])=>v),backgroundColor:'rgba(56,189,248,.7)',borderRadius:3}]},options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:SX,y:{...SXnr,ticks:{color:'#dde3f0',font:{size:11}}}}}});

  const dates=[...new Set(impl.map(r=>r.date))].sort();
  const agents=[...new Set(impl.map(r=>r.agent))].sort();
  const velDs=agents.map(ag=>{let cum=0;const data=dates.map(d=>{cum+=impl.filter(r=>r.agent===ag&&r.date===d).reduce((s,r)=>s+(r.lines_add||0),0);return cum;});const c=AC[ag]||{bg:'rgba(99,102,241,.2)',ln:'#6366f1'};return{label:ag,data,borderColor:c.ln,backgroundColor:c.bg.replace('.75','.1'),fill:false,tension:0.3,pointRadius:2,borderWidth:2};});
  mk('ch-velocity',{type:'line',data:{labels:dates,datasets:velDs},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#6b7898',boxWidth:10,padding:8}}},scales:{x:{...SX,ticks:{...SX.ticks,maxRotation:45}},y:{...SX,title:{display:true,text:'cumulative lines added',color:'#6b7898'}}}}});

  const buckets=[0,10,50,100,200,500,1000,Infinity];
  const bL=['0','1–10','11–50','51–100','101–200','201–500','501–1k','>1k'];
  const wc=impl.filter(r=>(r.lines_add||0)+(r.lines_rm||0)>0);
  mk('ch-lines-dist',{type:'bar',data:{labels:bL,datasets:[{data:bL.map((_,i)=>wc.filter(r=>(r.lines_add||0)>=buckets[i]&&(r.lines_add||0)<buckets[i+1]).length),backgroundColor:'rgba(99,102,241,.7)',borderRadius:4}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:SX,y:{...SX,title:{display:true,text:'# runs',color:'#6b7898'}}}}});
}

// ── Failures ──────────────────────────────────────────────────────────────
function renderFailures(runs){
  const failed=runs.filter(r=>r.outcome==='failure'||r.outcome==='error');
  const tax=countBy(failed,r=>r.outcome_reason);
  const taxS=Object.entries(tax).sort((a,b)=>b[1]-a[1]);
  mk('ch-fail-tax',{type:'bar',data:{labels:taxS.map(([k])=>OR[k]?.lb||k),datasets:[{data:taxS.map(([,v])=>v),backgroundColor:taxS.map(([k])=>OR[k]?.bg||'rgba(239,68,68,.7)'),borderRadius:3}]},options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:SX,y:{...SXnr,ticks:{color:'#dde3f0',font:{size:11}}}}}});

  const infraR=new Set(['infra_error','infra_transient','registry_auth','runtime_error','batch_failure']);
  const byDate=groupBy(runs,r=>r.date);
  const dates=Object.keys(byDate).sort();
  mk('ch-infra-trend',{type:'bar',data:{labels:dates,datasets:[
    {label:'Infra failures',data:dates.map(d=>(byDate[d]||[]).filter(r=>infraR.has(r.outcome_reason)).length),backgroundColor:'rgba(249,115,22,.7)',borderRadius:2,yAxisID:'y'},
    {type:'line',label:'Total runs',data:dates.map(d=>(byDate[d]||[]).length),borderColor:'#6366f1',backgroundColor:'transparent',tension:0.3,pointRadius:2,yAxisID:'y2'},
  ]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:'#6b7898',boxWidth:10,padding:8}}},scales:{x:{...SX,ticks:{...SX.ticks,maxRotation:45}},y:{...SX,position:'left',title:{display:true,text:'infra fails',color:'#6b7898'}},y2:{...SX,position:'right',title:{display:true,text:'total runs',color:'#6b7898'},grid:{drawOnChartArea:false}}}}});

  const errRuns=failed.sort((a,b)=>b.ts_iso.localeCompare(a.ts_iso)).slice(0,250);
  set('err-meta',`${failed.length} failed/error runs (showing ${Math.min(failed.length,250)})`);
  document.getElementById('err-body').innerHTML=errRuns.map(r=>{
    const rb=`<span class="badge" style="background:${OR[r.outcome_reason]?.bg||'rgba(99,102,241,.2)'};color:#dde3f0;font-size:10px">${OR[r.outcome_reason]?.lb||r.outcome_reason}</span>`;
    const et=r.err_type?`<code>${esc(r.err_type)}</code>`:'—';
    const bf=r.bf_class?`<code>${esc(r.bf_class)}</code>`:'—';
    const msg=r.err_msg?(esc(r.err_msg.slice(0,90))+(r.err_msg.length>90?'…':'')):r.review_error?esc(r.review_error.slice(0,70)):r.fail_sub?`<code>${esc(r.fail_sub)}</code>`:'—';
    const ci=r.ci_conc?`<span class="badge ${r.ci_conc==='success'?'b-success':'b-failure'}">${r.ci_conc}</span>`:'—';
    const rev=r.review_error?esc(r.review_error.slice(0,60)):'—';
    return `<tr><td class="mono">${esc(r.date)}</td><td>${esc(r.suite.replace('usertest_',''))}</td>
      <td><span class="pill-${r.agent}">${esc(r.agent)}</span></td>
      <td>${rb}</td><td>${et}</td><td>${bf}</td>
      <td class="wrap" style="max-width:220px">${msg}</td>
      <td>${ci}</td><td class="wrap" style="max-width:160px">${rev}</td></tr>`;
  }).join('');
}

// ── All runs table ─────────────────────────────────────────────────────────
function renderRunsTable(runs){
  const sorted=[...runs].sort((a,b)=>{const va=a[ST.sortCol]??'',vb=b[ST.sortCol]??'';return ST.sortDir*(va<vb?-1:va>vb?1:0);});
  const shown=sorted.slice(0,500);
  set('runs-meta',`Showing ${shown.length} of ${sorted.length}`);
  if(!shown.length){document.getElementById('runs-body').innerHTML='<tr><td colspan="12" class="no-data">No runs match filters.</td></tr>';return;}
  document.getElementById('runs-body').innerHTML=shown.map(r=>{
    const oc=r.outcome||'unknown';
    const ocB=`<span class="badge b-${oc}">${oc}</span>`;
    const rb=r.outcome_reason?`<span style="font-size:11px;color:#6b7898">${OR[r.outcome_reason]?.lb||r.outcome_reason}</span>`:'—';
    const ag=`<span class="pill-${r.agent}">${esc(r.agent)}</span>`;
    const title=(r.tick_title||'').slice(0,50)+(r.tick_title&&r.tick_title.length>50?'…':'');
    const ci=r.ci_conc?`<span class="badge ${r.ci_conc==='success'?'b-success':'b-failure'}">${r.ci_conc}</span>`:'—';
    const pr=r.pr_url?`<a href="${esc(r.pr_url)}" target="_blank">↗</a>`:'—';
    return `<tr><td class="mono">${esc(r.date)}</td><td>${esc(r.suite.replace('usertest_',''))}</td>
      <td>${ag}</td>
      <td class="wrap" style="max-width:220px" title="${esc(r.tick_title||'')}">${esc(title)||'—'}</td>
      <td>${rb}</td><td class="num">${fmtDur(r.wall_s)}</td>
      <td class="num">${r.step_count||'—'}</td>
      <td class="num" style="color:#22c55e">+${(r.lines_add||0).toLocaleString()}</td>
      <td class="num" style="color:#ef4444">${r.lines_rm?'-'+r.lines_rm:''}</td>
      <td>${ci}</td><td>${pr}</td><td>${ocB}</td></tr>`;
  }).join('');
}

// ── global meta ────────────────────────────────────────────────────────────
function renderMeta(runs){
  set('fc-n',runs.length.toLocaleString());
  if(runs.length){set('meta-range',`${runs.length.toLocaleString()} runs · ${runs[0].date} → ${runs[runs.length-1].date}`);}
}

// ── full render ────────────────────────────────────────────────────────────
function renderAll(){
  const runs=filt();
  renderMeta(runs);
  renderDiscovery();
  renderPipeline(runs);
  renderTickets(runs);
  renderCode(runs);
  renderFailures(runs);
  renderRunsTable(runs);
}

// ── table sort ─────────────────────────────────────────────────────────────
document.querySelectorAll('th[data-col]').forEach(th=>{
  th.addEventListener('click',()=>{
    const col=th.dataset.col;
    if(ST.sortCol===col)ST.sortDir*=-1;else{ST.sortCol=col;ST.sortDir=-1;}
    document.querySelectorAll('th').forEach(t=>{t.classList.remove('sorted');const i=t.querySelector('.si');if(i)i.textContent='↕';});
    th.classList.add('sorted');const icon=th.querySelector('.si');if(icon)icon.textContent=ST.sortDir===-1?'↓':'↑';
    renderRunsTable(filt());
  });
});

// ── nav scroll ─────────────────────────────────────────────────────────────
document.querySelectorAll('.nav-link').forEach(l=>{
  l.addEventListener('click',()=>{
    document.getElementById(l.dataset.sec)?.scrollIntoView({behavior:'smooth'});
    document.querySelectorAll('.nav-link').forEach(x=>x.classList.remove('active'));
    l.classList.add('active');
  });
});

// ── filters ────────────────────────────────────────────────────────────────
document.getElementById('f-suite').addEventListener('change',   e=>{ST.suite=e.target.value;renderAll();});
document.getElementById('f-agent').addEventListener('change',   e=>{ST.agent=e.target.value;renderAll();});
document.getElementById('f-outcome').addEventListener('change', e=>{ST.outcome=e.target.value;renderAll();});
document.getElementById('f-search').addEventListener('input',   e=>{ST.search=e.target.value;renderAll();});

renderAll();
</script>
</body>
</html>"""


def build_html(runs: list[dict], backlog: dict) -> str:
    html = _HTML.replace("__RUNS_DATA__",    json.dumps(runs,    default=str), 1)
    html =  html.replace("__BACKLOG_DATA__",  json.dumps(backlog, default=str), 1)
    return html


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--runs-dir", default="runs",
                    help="path to runs/ directory  (default: ./runs)")
    ap.add_argument("--output", default="run_dashboard.html",
                    help="output HTML file  (default: ./run_dashboard.html)")
    ap.add_argument("--open", action="store_true",
                    help="open the dashboard in a browser after writing")
    args = ap.parse_args()

    runs_dir = Path(args.runs_dir)
    print(f"Scanning {runs_dir} …", file=sys.stderr)
    runs    = collect_runs(runs_dir)
    backlog = load_backlog(runs_dir)

    if not runs:
        print("No runs found – check --runs-dir.", file=sys.stderr)
        sys.exit(1)

    bl_n = len(backlog.get("tickets", []))
    print(f"  {len(runs)} runs  ·  {bl_n} backlog tickets", file=sys.stderr)

    html = build_html(runs, backlog)
    out  = Path(args.output)
    out.write_text(html, encoding="utf-8")
    size_kb = len(html.encode()) // 1024
    print(f"Dashboard written -> {out}  ({size_kb} KB)")

    if args.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
