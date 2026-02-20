from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reporter.analysis import analyze_report_history


def _utc_now_z() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    values_sorted = sorted(values)
    n = len(values_sorted)
    mid = n // 2
    if n % 2 == 1:
        return float(values_sorted[mid])
    return float((values_sorted[mid - 1] + values_sorted[mid]) / 2.0)


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _get_id(container: Any, key: str) -> str | None:
    if not isinstance(container, dict):
        return None
    return _coerce_str(container.get(key))


def _record_run_id(record: dict[str, Any]) -> str:
    run_dir_raw = record.get("run_dir")
    run_dir = str(run_dir_raw) if isinstance(run_dir_raw, str) else "<unknown>"
    run_rel_raw = record.get("run_rel")
    run_rel = run_rel_raw if isinstance(run_rel_raw, str) and run_rel_raw else None
    return str(run_rel or run_dir)


def _record_run_rel(record: dict[str, Any]) -> str:
    run_rel_raw = record.get("run_rel")
    if isinstance(run_rel_raw, str) and run_rel_raw:
        return run_rel_raw
    return _record_run_id(record)


def _resolve_persona_mission(record: dict[str, Any]) -> tuple[str, str]:
    effective = record.get("effective_run_spec")
    target_ref = record.get("target_ref")

    persona_id = (
        _get_id(effective, "persona_id")
        or _get_id(target_ref, "persona_id")
        or _get_id(target_ref, "requested_persona_id")
        or "unknown"
    )
    mission_id = (
        _get_id(effective, "mission_id")
        or _get_id(target_ref, "mission_id")
        or _get_id(target_ref, "requested_mission_id")
        or "unknown"
    )
    return persona_id, mission_id


def _extract_run_wall_seconds(record: dict[str, Any]) -> float | None:
    run_meta = record.get("run_meta")
    if not isinstance(run_meta, dict):
        return None
    value = run_meta.get("run_wall_seconds")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _extract_attempt_count(record: dict[str, Any]) -> int | None:
    agent_attempts = record.get("agent_attempts")
    if not isinstance(agent_attempts, dict):
        return None
    attempts = agent_attempts.get("attempts")
    if isinstance(attempts, list):
        return len(attempts)
    return None


def _scoreboard(records: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    wall_seconds: list[float] = []
    attempt_counts: list[float] = []

    for record in records:
        status = _coerce_str(record.get("status")) or "unknown"
        status_counts[status] += 1

        seconds = _extract_run_wall_seconds(record)
        if seconds is not None:
            wall_seconds.append(seconds)

        attempt_count = _extract_attempt_count(record)
        if attempt_count is not None:
            attempt_counts.append(float(attempt_count))

    runs = len(records)
    ok_runs = int(status_counts.get("ok", 0))
    ok_rate = (ok_runs / runs) if runs else None

    return {
        "runs": runs,
        "status_counts": dict(sorted(status_counts.items())),
        "ok_rate": ok_rate,
        "timing_coverage_runs": len(wall_seconds),
        "median_run_wall_seconds": _median(wall_seconds),
        "median_attempts_per_run": _median(attempt_counts),
    }


def _delta(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    for key in ("runs", "ok_rate", "timing_coverage_runs", "median_run_wall_seconds", "median_attempts_per_run"):
        cur = current.get(key)
        base = baseline.get(key)
        if isinstance(cur, (int, float)) and isinstance(base, (int, float)):
            delta[key] = float(cur) - float(base)
        else:
            delta[key] = None

    cur_status = current.get("status_counts")
    base_status = baseline.get("status_counts")
    if isinstance(cur_status, dict) and isinstance(base_status, dict):
        status_delta: dict[str, int] = {}
        keys = set(cur_status.keys()) | set(base_status.keys())
        for status in sorted(k for k in keys if isinstance(k, str)):
            cur_v = cur_status.get(status)
            base_v = base_status.get(status)
            if isinstance(cur_v, int) and isinstance(base_v, int):
                status_delta[status] = cur_v - base_v
            elif isinstance(cur_v, int) and base_v is None:
                status_delta[status] = cur_v
            elif isinstance(base_v, int) and cur_v is None:
                status_delta[status] = -base_v
        delta["status_counts"] = status_delta
    else:
        delta["status_counts"] = {}

    return delta


def build_window_summary(
    *,
    current_records: list[dict[str, Any]],
    baseline_records: list[dict[str, Any]],
    repo_root: Path | None,
    issue_actions_path: Path | None,
    window_size: int,
    baseline_size: int,
) -> dict[str, Any]:
    current = _scoreboard(current_records)
    baseline = _scoreboard(baseline_records)
    delta = _delta(current, baseline)

    notes: list[str] = []
    if len(current_records) < window_size:
        notes.append(f"current window smaller than requested: {len(current_records)} < {window_size}")
    if len(baseline_records) < baseline_size:
        notes.append(
            f"baseline window smaller than requested: {len(baseline_records)} < {baseline_size}"
        )
    for label, scoreboard in (("current", current), ("baseline", baseline)):
        runs = scoreboard.get("runs")
        timing = scoreboard.get("timing_coverage_runs")
        if isinstance(runs, int) and isinstance(timing, int) and timing < runs:
            notes.append(f"timing missing for {runs - timing} {label} runs")

    current_runs: list[dict[str, Any]] = []
    baseline_runs: list[dict[str, Any]] = []
    run_id_to_persona_mission: dict[str, tuple[str, str]] = {}

    for record in [*baseline_records, *current_records]:
        run_id = _record_run_id(record)
        run_id_to_persona_mission[run_id] = _resolve_persona_mission(record)

    def _digest(record: dict[str, Any]) -> dict[str, Any]:
        persona_id, mission_id = _resolve_persona_mission(record)
        return {
            "run_rel": _record_run_rel(record),
            "run_id": _record_run_id(record),
            "status": _coerce_str(record.get("status")) or "unknown",
            "persona_id": persona_id,
            "mission_id": mission_id,
            "run_wall_seconds": _extract_run_wall_seconds(record),
            "attempts": _extract_attempt_count(record),
        }

    baseline_run_rels: list[str] = []
    current_run_rels: list[str] = []
    for record in baseline_records:
        digest = _digest(record)
        baseline_runs.append(digest)
        baseline_run_rels.append(str(digest["run_rel"]))
    for record in current_records:
        digest = _digest(record)
        current_runs.append(digest)
        current_run_rels.append(str(digest["run_rel"]))

    def _persona_mission_breakdown(records: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            grouped[_resolve_persona_mission(record)].append(record)

        out: dict[tuple[str, str], dict[str, Any]] = {}
        for key, items in grouped.items():
            out[key] = _scoreboard(items)
        return out

    current_pm = _persona_mission_breakdown(current_records)
    baseline_pm = _persona_mission_breakdown(baseline_records)
    pm_keys = set(current_pm.keys()) | set(baseline_pm.keys())
    persona_mission: list[dict[str, Any]] = []
    for persona_id, mission_id in sorted(pm_keys):
        cur = current_pm.get((persona_id, mission_id), _scoreboard([]))
        base = baseline_pm.get((persona_id, mission_id), _scoreboard([]))
        persona_mission.append(
            {
                "persona_id": persona_id,
                "mission_id": mission_id,
                "current": cur,
                "baseline": base,
                "delta": _delta(cur, base),
            }
        )

    current_analysis = analyze_report_history(
        current_records,
        repo_root=repo_root,
        issue_actions_path=issue_actions_path,
    )
    baseline_analysis = analyze_report_history(
        baseline_records,
        repo_root=repo_root,
        issue_actions_path=issue_actions_path,
    )

    def _theme_map(analysis: dict[str, Any]) -> dict[str, dict[str, Any]]:
        themes_raw = analysis.get("themes")
        themes = themes_raw if isinstance(themes_raw, list) else []
        out: dict[str, dict[str, Any]] = {}
        for item in themes:
            if not isinstance(item, dict):
                continue
            theme_id = _coerce_str(item.get("theme_id"))
            if theme_id is None:
                continue
            out[theme_id] = item
        return out

    cur_themes = _theme_map(current_analysis)
    base_themes = _theme_map(baseline_analysis)
    theme_ids = sorted(set(cur_themes.keys()) | set(base_themes.keys()))

    def _theme_persona_mission_breadth(theme: dict[str, Any]) -> int:
        signals_raw = theme.get("signals")
        signals = signals_raw if isinstance(signals_raw, list) else []
        keys: set[tuple[str, str]] = set()
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            run_id = _coerce_str(signal.get("run_id"))
            if run_id is None:
                continue
            key = run_id_to_persona_mission.get(run_id)
            if key is not None:
                keys.add(key)
        return len(keys)

    themes: list[dict[str, Any]] = []
    for theme_id in theme_ids:
        cur_item = cur_themes.get(theme_id, {})
        base_item = base_themes.get(theme_id, {})
        title = _coerce_str(cur_item.get("title")) or _coerce_str(base_item.get("title")) or theme_id

        def _theme_metrics(item: dict[str, Any]) -> dict[str, Any]:
            return {
                "runs_citing": int(item.get("runs_citing") or 0),
                "mentions": int(item.get("mentions") or 0),
                "unaddressed_mentions": int(item.get("unaddressed_mentions") or 0),
                "addressed_mentions": int(item.get("addressed_mentions") or 0),
                "persona_mission_breadth": _theme_persona_mission_breadth(item) if item else 0,
            }

        cur_metrics = _theme_metrics(cur_item if isinstance(cur_item, dict) else {})
        base_metrics = _theme_metrics(base_item if isinstance(base_item, dict) else {})
        delta_metrics: dict[str, Any] = {}
        for key in cur_metrics.keys():
            cur_v = cur_metrics.get(key)
            base_v = base_metrics.get(key)
            if isinstance(cur_v, int) and isinstance(base_v, int):
                delta_metrics[key] = cur_v - base_v
            else:
                delta_metrics[key] = None

        themes.append(
            {
                "theme_id": theme_id,
                "title": title,
                "current": cur_metrics,
                "baseline": base_metrics,
                "delta": delta_metrics,
            }
        )

    return {
        "schema_version": 1,
        "generated_at_utc": _utc_now_z(),
        "selection": {
            "window_size": int(window_size),
            "baseline_size": int(baseline_size),
            "current_run_rels": current_run_rels,
            "baseline_run_rels": baseline_run_rels,
            "current_runs": current_runs,
            "baseline_runs": baseline_runs,
        },
        "summary": {
            "current": current,
            "baseline": baseline,
            "delta": delta,
        },
        "persona_mission": persona_mission,
        "themes": themes,
        "notes": notes,
    }


def render_window_summary_markdown(summary: dict[str, Any], *, title: str) -> str:
    selection = summary.get("selection")
    selection_dict = selection if isinstance(selection, dict) else {}
    generated = summary.get("generated_at_utc")
    notes_raw = summary.get("notes")
    notes = notes_raw if isinstance(notes_raw, list) else []

    summary_block = summary.get("summary")
    summary_dict = summary_block if isinstance(summary_block, dict) else {}
    cur = summary_dict.get("current")
    base = summary_dict.get("baseline")
    delta = summary_dict.get("delta")
    cur_dict = cur if isinstance(cur, dict) else {}
    base_dict = base if isinstance(base, dict) else {}
    delta_dict = delta if isinstance(delta, dict) else {}

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    if isinstance(generated, str) and generated:
        lines.append(f"Generated: `{generated}`")
        lines.append("")

    lines.append("## Selection")
    window_size = selection_dict.get("window_size")
    baseline_size = selection_dict.get("baseline_size")
    lines.append(f"- window_size: `{window_size}`")
    lines.append(f"- baseline_size: `{baseline_size}`")

    cur_rels = selection_dict.get("current_run_rels")
    base_rels = selection_dict.get("baseline_run_rels")
    if isinstance(cur_rels, list):
        lines.append("- current_run_rels:")
        for item in cur_rels:
            if isinstance(item, str) and item:
                lines.append(f"  - `{item}`")
    if isinstance(base_rels, list):
        lines.append("- baseline_run_rels:")
        for item in base_rels:
            if isinstance(item, str) and item:
                lines.append(f"  - `{item}`")
    lines.append("")

    def _fmt_rate(value: Any) -> str:
        if isinstance(value, (int, float)):
            return f"{float(value):.2f}"
        return "n/a"

    def _fmt_seconds(value: Any) -> str:
        if isinstance(value, (int, float)):
            return f"{float(value):.2f}"
        return "n/a"

    lines.append("## Scoreboard")
    lines.append(
        "Current window: "
        f"runs={cur_dict.get('runs')}, "
        f"ok_rate={_fmt_rate(cur_dict.get('ok_rate'))}, "
        f"median_wall_seconds={_fmt_seconds(cur_dict.get('median_run_wall_seconds'))} "
        f"(timing coverage {cur_dict.get('timing_coverage_runs')}/{cur_dict.get('runs')})"
    )
    lines.append(
        "Baseline window: "
        f"runs={base_dict.get('runs')}, "
        f"ok_rate={_fmt_rate(base_dict.get('ok_rate'))}, "
        f"median_wall_seconds={_fmt_seconds(base_dict.get('median_run_wall_seconds'))} "
        f"(timing coverage {base_dict.get('timing_coverage_runs')}/{base_dict.get('runs')})"
    )
    lines.append(
        "Delta: "
        f"ok_rate={_fmt_rate(delta_dict.get('ok_rate'))}, "
        f"median_wall_seconds={_fmt_seconds(delta_dict.get('median_run_wall_seconds'))}"
    )
    lines.append("")

    pm_raw = summary.get("persona_mission")
    pm = pm_raw if isinstance(pm_raw, list) else []
    if pm:
        lines.append("## Persona/Mission Breakdown")
        lines.append("| persona_id | mission_id | runs (cur/base) | ok_rate (cur/base) | median_seconds (cur/base) |")
        lines.append("| --- | --- | --- | --- | --- |")

        def _pm_sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
            cur = item.get("current")
            cur_runs = cur.get("runs") if isinstance(cur, dict) else 0
            persona_id = str(item.get("persona_id") or "")
            mission_id = str(item.get("mission_id") or "")
            return (-int(cur_runs) if isinstance(cur_runs, int) else 0, persona_id, mission_id)

        pm_items = [x for x in pm if isinstance(x, dict)]
        pm_items.sort(key=_pm_sort_key)
        omitted = max(0, len(pm_items) - 20)
        for item in pm_items[:20]:
            persona_id = str(item.get("persona_id") or "unknown")
            mission_id = str(item.get("mission_id") or "unknown")
            cur = item.get("current")
            base = item.get("baseline")
            cur_dict = cur if isinstance(cur, dict) else {}
            base_dict = base if isinstance(base, dict) else {}
            lines.append(
                f"| `{persona_id}` | `{mission_id}` | "
                f"{cur_dict.get('runs')}/{base_dict.get('runs')} | "
                f"{_fmt_rate(cur_dict.get('ok_rate'))}/{_fmt_rate(base_dict.get('ok_rate'))} | "
                f"{_fmt_seconds(cur_dict.get('median_run_wall_seconds'))}/{_fmt_seconds(base_dict.get('median_run_wall_seconds'))} |"
            )
        if omitted:
            lines.append("")
            lines.append(f"_({omitted} more persona/mission pairs omitted)_")
        lines.append("")

    themes_raw = summary.get("themes")
    themes = themes_raw if isinstance(themes_raw, list) else []
    theme_items = [x for x in themes if isinstance(x, dict)]

    def _theme_get(item: dict[str, Any], block: str, key: str) -> int:
        obj = item.get(block)
        if not isinstance(obj, dict):
            return 0
        value = obj.get(key)
        return int(value) if isinstance(value, int) else 0

    regressions = [
        item
        for item in theme_items
        if _theme_get(item, "delta", "runs_citing") > 0
        or _theme_get(item, "delta", "unaddressed_mentions") > 0
    ]
    regressions.sort(
        key=lambda item: (
            -_theme_get(item, "delta", "runs_citing"),
            -_theme_get(item, "delta", "unaddressed_mentions"),
            str(item.get("title") or ""),
        )
    )
    if regressions:
        lines.append("## Top regressions (themes)")
        for item in regressions[:12]:
            title = str(item.get("title") or item.get("theme_id") or "unknown")
            cur_runs = _theme_get(item, "current", "runs_citing")
            base_runs = _theme_get(item, "baseline", "runs_citing")
            cur_unaddr = _theme_get(item, "current", "unaddressed_mentions")
            base_unaddr = _theme_get(item, "baseline", "unaddressed_mentions")
            cur_breadth = _theme_get(item, "current", "persona_mission_breadth")
            base_breadth = _theme_get(item, "baseline", "persona_mission_breadth")
            lines.append(
                f"- {title}: runs_citing {base_runs} -> {cur_runs} (+{cur_runs - base_runs}), "
                f"unaddressed_mentions {base_unaddr} -> {cur_unaddr} (+{cur_unaddr - base_unaddr}), "
                f"breadth {base_breadth} -> {cur_breadth} (+{cur_breadth - base_breadth})"
            )
        lines.append("")

    cross_cutting = [
        item
        for item in theme_items
        if _theme_get(item, "current", "persona_mission_breadth") >= 2
        and _theme_get(item, "current", "unaddressed_mentions") > 0
    ]
    cross_cutting.sort(
        key=lambda item: (
            -_theme_get(item, "current", "persona_mission_breadth"),
            -_theme_get(item, "current", "unaddressed_mentions"),
            str(item.get("title") or ""),
        )
    )
    if cross_cutting:
        lines.append("## Top cross-cutting unaddressed themes")
        for item in cross_cutting[:12]:
            title = str(item.get("title") or item.get("theme_id") or "unknown")
            breadth = _theme_get(item, "current", "persona_mission_breadth")
            unaddr = _theme_get(item, "current", "unaddressed_mentions")
            runs = _theme_get(item, "current", "runs_citing")
            lines.append(f"- {title}: breadth={breadth}, unaddressed_mentions={unaddr}, runs_citing={runs}")
        lines.append("")

    current_runs_raw = selection_dict.get("current_runs")
    current_runs = current_runs_raw if isinstance(current_runs_raw, list) else []
    slow_candidates: list[tuple[float, dict[str, Any]]] = []
    for item in current_runs:
        if not isinstance(item, dict):
            continue
        seconds = item.get("run_wall_seconds")
        if isinstance(seconds, (int, float)):
            slow_candidates.append((float(seconds), item))
    slow_candidates.sort(key=lambda pair: -pair[0])
    if slow_candidates:
        lines.append("## Slowest runs (current window)")
        for seconds, item in slow_candidates[:5]:
            run_rel = str(item.get("run_rel") or item.get("run_id") or "<unknown>")
            status = str(item.get("status") or "unknown")
            lines.append(f"- `{run_rel}`: status={status}, wall_seconds={seconds:.2f}")
        lines.append("")

    if notes:
        lines.append("## Notes")
        for note in notes:
            if isinstance(note, str) and note.strip():
                lines.append(f"- {note.strip()}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_window_summary(
    summary: dict[str, Any],
    *,
    out_json_path: Path,
    out_md_path: Path,
    title: str,
) -> None:
    out_json_path.parent.mkdir(parents=True, exist_ok=True)
    out_json_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    out_md_path.parent.mkdir(parents=True, exist_ok=True)
    out_md_path.write_text(
        render_window_summary_markdown(summary, title=title),
        encoding="utf-8",
        newline="\n",
    )

