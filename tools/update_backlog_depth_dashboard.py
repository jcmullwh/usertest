"""Render the backlog-depth operational dashboard from its JSON ledger.

The companion JSON remains the source of truth.  This script deliberately
renders only lifecycle-level run summaries; detailed benchmark measurements
stay in the rest of the metrics document.

Future automation can upsert one run by passing a small receipt shaped as::

    {
      "schema_version": 3,
      "as_of": "2026-07-16T03:17:11Z",
      "current_run_id": "run-id",
      "summary_cards": [ ...all four operational summary cards... ],
      "run": { ...one operational_dashboard run object... }
    }

Use ``--check`` in validation to prove that the JSON is valid and the checked-in
HTML block exactly matches the rendered ledger.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from html import escape
import json
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSON = (
    ROOT / "docs/design/historical-automated-backlog-depth-remediation-metrics.json"
)
DEFAULT_HTML = (
    ROOT / "docs/design/historical-automated-backlog-depth-remediation-metrics.html"
)
START_MARKER = "        <!-- BEGIN GENERATED OPERATIONAL DASHBOARD -->"
END_MARKER = "        <!-- END GENERATED OPERATIONAL DASHBOARD -->"
ENTRY_KINDS = {"baseline", "lifecycle_run", "supporting_activity"}
LIFECYCLE_KINDS = {"pipeline_cycle", "case", "delivery"}
DEDUPLICATED_CLUSTER_UNIT = "deduplicated_clusters"


class DashboardContractError(ValueError):
    """Raised when a dashboard ledger or update receipt is malformed."""


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DashboardContractError(f"{label} must be an object")
    return value


def _nonempty_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DashboardContractError(f"{label} must be non-empty text")
    return value.strip()


def _validate_measure(value: object, *, label: str) -> None:
    measure = _mapping(value, label=label)
    count = measure.get("count")
    if count is not None and (
        not isinstance(count, int) or isinstance(count, bool) or count < 0
    ):
        raise DashboardContractError(
            f"{label}.count must be null or a non-negative integer"
        )
    _nonempty_text(measure.get("count_kind"), label=f"{label}.count_kind")
    _nonempty_text(measure.get("summary"), label=f"{label}.summary")
    cluster_ids = measure.get("cluster_ids")
    if cluster_ids is None:
        return
    if measure.get("count_kind") != DEDUPLICATED_CLUSTER_UNIT:
        raise DashboardContractError(
            f"{label}.cluster_ids require deduplicated cluster units"
        )
    if not isinstance(cluster_ids, list) or any(
        not isinstance(item, str) or not item.strip() for item in cluster_ids
    ):
        raise DashboardContractError(
            f"{label}.cluster_ids must be a list of non-empty strings"
        )
    if len(cluster_ids) != len(set(cluster_ids)):
        raise DashboardContractError(f"{label}.cluster_ids must be unique")
    if count != len(cluster_ids):
        raise DashboardContractError(
            f"{label}.count must equal the number of cluster_ids"
        )


def _validate_nullable_count(value: object, *, label: str) -> None:
    if value is not None and (
        not isinstance(value, int) or isinstance(value, bool) or value < 0
    ):
        raise DashboardContractError(
            f"{label} must be null or a non-negative integer"
        )


def _validate_rework(value: object, *, label: str) -> None:
    rework = _mapping(value, label=label)
    for field in (
        "author_invocations",
        "continuation_launches",
        "stage_reruns",
        "full_restarts",
    ):
        _validate_nullable_count(rework.get(field), label=f"{label}.{field}")
    _nonempty_text(rework.get("summary"), label=f"{label}.summary")


def _validate_run(value: object, *, index: int | None = None) -> Mapping[str, Any]:
    prefix = f"runs[{index}]" if index is not None else "run"
    run = _mapping(value, label=prefix)
    for field in (
        "run_id",
        "label",
        "scope",
        "furthest_stage",
        "current_state",
        "outcome",
        "next_action",
        "tone",
        "data_quality",
    ):
        _nonempty_text(run.get(field), label=f"{prefix}.{field}")
    if run.get("tone") not in {"good", "warn", "bad", "info", "neutral"}:
        raise DashboardContractError(f"{prefix}.tone is not a supported dashboard tone")

    entry_kind = _nonempty_text(run.get("entry_kind"), label=f"{prefix}.entry_kind")
    if entry_kind not in ENTRY_KINDS:
        raise DashboardContractError(f"{prefix}.entry_kind is not supported")
    parent_run_id = run.get("parent_run_id")
    if entry_kind == "supporting_activity":
        _nonempty_text(parent_run_id, label=f"{prefix}.parent_run_id")
    elif parent_run_id is not None:
        raise DashboardContractError(
            f"{prefix}.parent_run_id is only valid for supporting_activity entries"
        )
    if entry_kind == "lifecycle_run":
        _nonempty_text(run.get("lifecycle_id"), label=f"{prefix}.lifecycle_id")
        lifecycle_kind = _nonempty_text(
            run.get("lifecycle_kind"), label=f"{prefix}.lifecycle_kind"
        )
        if lifecycle_kind not in LIFECYCLE_KINDS:
            raise DashboardContractError(
                f"{prefix}.lifecycle_kind is not supported"
            )
        _validate_rework(run.get("rework"), label=f"{prefix}.rework")

    timing = _mapping(run.get("timing"), label=f"{prefix}.timing")
    _nonempty_text(timing.get("display"), label=f"{prefix}.timing.display")
    coverage = _nonempty_text(timing.get("coverage"), label=f"{prefix}.timing.coverage")
    if coverage not in {
        "complete",
        "measured_substeps",
        "artifact_window",
        "unknown",
        "active",
    }:
        raise DashboardContractError(f"{prefix}.timing.coverage is invalid")
    elapsed = timing.get("elapsed_seconds")
    if elapsed is not None and (
        not isinstance(elapsed, (int, float))
        or isinstance(elapsed, bool)
        or elapsed < 0
    ):
        raise DashboardContractError(
            f"{prefix}.timing.elapsed_seconds must be null or a non-negative number"
        )

    _validate_measure(run.get("errors"), label=f"{prefix}.errors")
    _validate_measure(
        run.get("automatic_self_corrections"),
        label=f"{prefix}.automatic_self_corrections",
    )
    _validate_measure(
        run.get("supervisor_interventions"),
        label=f"{prefix}.supervisor_interventions",
    )
    source_ids = run.get("source_ids")
    if not isinstance(source_ids, list) or not source_ids:
        raise DashboardContractError(f"{prefix}.source_ids must be a non-empty list")
    for source_index, source_id in enumerate(source_ids):
        _nonempty_text(source_id, label=f"{prefix}.source_ids[{source_index}]")
    return run


def _validate_summary_cards(value: object, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != 4:
        raise DashboardContractError(f"{label} must contain four cards")
    cards: list[dict[str, Any]] = []
    for index, value in enumerate(value):
        card = dict(_mapping(value, label=f"{label}[{index}]"))
        for field in ("label", "value", "detail", "tone"):
            _nonempty_text(card.get(field), label=f"{label}[{index}].{field}")
        cards.append(card)
    return cards


def validate_dashboard(value: object, *, source_ids: set[str]) -> Mapping[str, Any]:
    dashboard = _mapping(value, label="operational_dashboard")
    if dashboard.get("schema_version") != 3:
        raise DashboardContractError(
            "operational_dashboard.schema_version must equal 3"
        )
    as_of = _parse_datetime(
        _nonempty_text(dashboard.get("as_of"), label="operational_dashboard.as_of"),
        label="operational_dashboard.as_of",
    )
    current_run_id = _nonempty_text(
        dashboard.get("current_run_id"), label="operational_dashboard.current_run_id"
    )
    _nonempty_text(
        dashboard.get("counting_rule"), label="operational_dashboard.counting_rule"
    )
    _validate_summary_cards(
        dashboard.get("summary_cards"), label="operational_dashboard.summary_cards"
    )

    runs = dashboard.get("runs")
    if not isinstance(runs, list) or not runs:
        raise DashboardContractError(
            "operational_dashboard.runs must be a non-empty list"
        )
    seen: set[str] = set()
    lifecycle_ids: set[str] = set()
    runs_by_id: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(runs):
        run = _validate_run(value, index=index)
        run_id = str(run["run_id"])
        if run_id in seen:
            raise DashboardContractError(f"duplicate operational run_id: {run_id}")
        seen.add(run_id)
        runs_by_id[run_id] = run
        if run["entry_kind"] == "lifecycle_run":
            lifecycle_id = str(run["lifecycle_id"])
            if lifecycle_id in lifecycle_ids:
                raise DashboardContractError(
                    f"duplicate operational lifecycle_id: {lifecycle_id}"
                )
            lifecycle_ids.add(lifecycle_id)
        missing_sources = sorted(set(run["source_ids"]) - source_ids)
        if missing_sources:
            raise DashboardContractError(
                f"{run_id} references missing source IDs: {', '.join(missing_sources)}"
            )

    for run_id, run in runs_by_id.items():
        if run["entry_kind"] != "supporting_activity":
            continue
        parent_run_id = str(run["parent_run_id"])
        parent = runs_by_id.get(parent_run_id)
        if parent is None:
            raise DashboardContractError(
                f"{run_id} references missing parent_run_id: {parent_run_id}"
            )
        if parent["entry_kind"] != "lifecycle_run":
            raise DashboardContractError(
                f"{run_id} parent_run_id must identify a lifecycle_run"
            )

    current_run = runs_by_id.get(current_run_id)
    if current_run is None:
        raise DashboardContractError(
            "operational_dashboard.current_run_id must identify one retained run"
        )
    if current_run["entry_kind"] != "lifecycle_run":
        raise DashboardContractError(
            "operational_dashboard.current_run_id must identify a lifecycle_run"
        )
    lifecycle_runs = _lifecycle_runs(dashboard)
    if not lifecycle_runs:
        raise DashboardContractError(
            "operational_dashboard.runs must contain at least one lifecycle_run"
        )
    if str(lifecycle_runs[-1]["run_id"]) != current_run_id:
        raise DashboardContractError(
            "the current operational run must be the final lifecycle_run"
        )
    for run in lifecycle_runs:
        timing = run["timing"]
        if timing["coverage"] != "active":
            continue
        if str(run["run_id"]) != current_run_id:
            raise DashboardContractError(
                "only the current lifecycle_run may have active timing"
            )
        start_at = _parse_datetime(
            _nonempty_text(
                timing.get("start_at"), label=f"{run['run_id']}.timing.start_at"
            ),
            label=f"{run['run_id']}.timing.start_at",
        )
        if timing.get("end_at") is not None:
            raise DashboardContractError("active timing.end_at must be null")
        elapsed = timing.get("elapsed_seconds")
        if elapsed is None or abs((as_of - start_at).total_seconds() - elapsed) > 0.01:
            raise DashboardContractError(
                "active timing.elapsed_seconds must equal as_of minus start_at"
            )
    return dashboard


def _parse_datetime(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DashboardContractError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise DashboardContractError(f"{label} must include a timezone")
    return parsed


def _lifecycle_runs(dashboard: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        run
        for run in dashboard["runs"]
        if run.get("entry_kind") == "lifecycle_run"
    ]


def _entry_count(dashboard: Mapping[str, Any], entry_kind: str) -> int:
    return sum(1 for run in dashboard["runs"] if run.get("entry_kind") == entry_kind)


def _short_text(value: object, *, limit: int = 180) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    boundary = text.rfind(" ", 0, limit - 1)
    if boundary < limit // 2:
        boundary = limit - 1
    return text[:boundary].rstrip() + "…"


def _render_measure(value: Mapping[str, Any]) -> str:
    if value.get("count_kind") != DEDUPLICATED_CLUSTER_UNIT:
        return (
            '<strong>Unknown</strong> <span class="ops-kind">legacy unreconciled</span>'
            '<span class="ops-detail">Detailed evidence remains in JSON; no '
            "cross-run cluster total is inferred.</span>"
        )
    count = value.get("count")
    prefix = "Unknown" if count is None else str(count)
    return (
        f"<strong>{escape(prefix)}</strong> "
        '<span class="ops-kind">deduplicated clusters</span>'
        f'<span class="ops-detail">{escape(_short_text(value["summary"]))}</span>'
    )


def _render_rework(value: Mapping[str, Any]) -> str:
    def count(field: str) -> str:
        item = value.get(field)
        return "Unknown" if item is None else str(item)

    return (
        f'<strong>{escape(count("author_invocations"))}</strong> author calls; '
        f'{escape(count("continuation_launches"))} continuations; '
        f'{escape(count("stage_reruns"))} stage reruns; '
        f'{escape(count("full_restarts"))} full restarts'
        f'<span class="ops-detail">{escape(_short_text(value["summary"]))}</span>'
    )


def _render_dashboard(dashboard: Mapping[str, Any]) -> str:
    cards = []
    for card in dashboard["summary_cards"]:
        cards.append(
            '            <div class="stat {tone}">\n'
            '              <span class="label">{label}</span>\n'
            '              <span class="value">{value}</span>\n'
            '              <span class="detail">{detail}</span>\n'
            "            </div>".format(
                tone=escape(str(card["tone"]), quote=True),
                label=escape(str(card["label"])),
                value=escape(str(card["value"])),
                detail=escape(str(card["detail"])),
            )
        )

    rows = []
    current_run_id = str(dashboard["current_run_id"])
    for run in _lifecycle_runs(dashboard):
        current_class = " ops-current" if str(run["run_id"]) == current_run_id else ""
        timing = run["timing"]
        rows.append(
            '              <tr class="{current_class}">\n'
            '                <td><strong>{label}</strong><span class="ops-kind">{lifecycle_kind}</span></td>\n'
            '                <td><strong>{timing_display}</strong><span class="ops-kind">{timing_coverage}</span></td>\n'
            '                <td><span class="tag {tone}">{furthest_stage}</span><span class="ops-detail">{current_state}</span></td>\n'
            '                <td><strong>{outcome}</strong></td>\n'
            "                <td>{errors}</td>\n"
            "                <td>{self_corrections}</td>\n"
            "                <td>{interventions}</td>\n"
            "                <td>{rework}</td>\n"
            '                <td>{next_action}</td>\n'
            "              </tr>".format(
                current_class=("ops-current" if current_class else ""),
                label=escape(str(run["label"])),
                lifecycle_kind=escape(str(run["lifecycle_kind"]).replace("_", " ")),
                timing_display=escape(str(timing["display"])),
                timing_coverage=escape(str(timing["coverage"])),
                tone=escape(str(run["tone"]), quote=True),
                furthest_stage=escape(str(run["furthest_stage"])),
                current_state=escape(_short_text(run["current_state"])),
                outcome=escape(str(run["outcome"])),
                errors=_render_measure(run["errors"]),
                self_corrections=_render_measure(run["automatic_self_corrections"]),
                interventions=_render_measure(run["supervisor_interventions"]),
                rework=_render_rework(run["rework"]),
                next_action=escape(_short_text(run["next_action"])),
            )
        )

    return "\n".join(
        [
            START_MARKER,
            '        <section id="status-change">',
            '          <span id="operational-dashboard" aria-hidden="true"></span>',
            "          <h2>Operational progress dashboard</h2>",
            (
                "          <p>This is the current top-level run ledger. It tracks whether the "
                "pipeline is producing real, researched work and moving it to an honestly "
                "verified disposition. Detailed benchmark and contract evidence remains below; "
                "those lower sections preserve their own point-in-time snapshots and may describe "
                "an earlier lifecycle state.</p>"
            ),
            '          <div class="notice info"><strong>Counting rule:</strong> '
            + escape(str(dashboard["counting_rule"]))
            + "</div>",
            '          <div class="status-grid ops-summary">',
            *cards,
            "          </div>",
            '          <div class="table-wrap ops-table-wrap">',
            '            <table class="ops-table">',
            "              <thead><tr><th>Lifecycle</th><th>Time</th><th>Reached / state</th><th>Outcome</th><th>Error clusters</th><th>Automatic repairs</th><th>Supervisor interventions</th><th>Restarts / rework</th><th>Next gate</th></tr></thead>",
            "              <tbody>",
            *rows,
            "              </tbody>",
            "            </table>",
            "          </div>",
            (
                '          <p class="small muted">Dashboard as of '
                + escape(str(dashboard["as_of"]))
                + ". Showing "
                + str(len(_lifecycle_runs(dashboard)))
                + " lifecycle runs; detailed JSON provenance retains "
                + str(_entry_count(dashboard, "supporting_activity"))
                + " supporting records and "
                + str(_entry_count(dashboard, "baseline"))
                + " historical baseline. Unknown means "
                "the retained evidence cannot support a count; it never means zero. "
                "Measured-substep durations are not comparable to complete wall time.</p>"
            ),
            "        </section>",
            END_MARKER,
        ]
    )


def _replace_generated_block(html_text: str, rendered: str) -> str:
    if html_text.count(START_MARKER) != 1 or html_text.count(END_MARKER) != 1:
        raise DashboardContractError(
            "HTML must contain exactly one dashboard marker pair"
        )
    prefix, remainder = html_text.split(START_MARKER, 1)
    _old, suffix = remainder.split(END_MARKER, 1)
    return prefix + rendered + suffix


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise DashboardContractError(f"{path} must contain a top-level object")
    return payload


def _apply_receipt(payload: dict[str, Any], receipt_path: Path) -> None:
    receipt = _mapping(_load_json(receipt_path), label="receipt")
    if receipt.get("schema_version") != 3:
        raise DashboardContractError("receipt.schema_version must equal 3")
    run = dict(_validate_run(receipt.get("run")))
    dashboard = dict(
        _mapping(payload.get("operational_dashboard"), label="operational_dashboard")
    )
    runs = list(dashboard.get("runs") or [])
    run_id = str(run["run_id"])
    replaced = False
    for index, existing in enumerate(runs):
        if isinstance(existing, Mapping) and existing.get("run_id") == run_id:
            runs[index] = run
            replaced = True
            break
    if not replaced:
        runs.append(run)
    dashboard["runs"] = runs
    if receipt.get("current_run_id") is not None:
        dashboard["current_run_id"] = _nonempty_text(
            receipt.get("current_run_id"), label="receipt.current_run_id"
        )
        dashboard["summary_cards"] = _validate_summary_cards(
            receipt.get("summary_cards"), label="receipt.summary_cards"
        )
    if receipt.get("as_of") is not None:
        dashboard["as_of"] = _nonempty_text(receipt.get("as_of"), label="receipt.as_of")
    payload["operational_dashboard"] = dashboard


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    payload = _load_json(args.json)
    if args.receipt is not None:
        if args.check:
            raise DashboardContractError("--receipt and --check are mutually exclusive")
        _apply_receipt(payload, args.receipt)

    source_ids = {
        str(source.get("id"))
        for source in payload.get("sources", [])
        if isinstance(source, Mapping) and source.get("id")
    }
    dashboard = validate_dashboard(
        payload.get("operational_dashboard"), source_ids=source_ids
    )
    rendered = _render_dashboard(dashboard)
    html_text = args.html.read_text(encoding="utf-8")
    updated_html = _replace_generated_block(html_text, rendered)

    if args.check:
        if updated_html != html_text:
            raise DashboardContractError(
                "checked-in dashboard HTML is stale; run tools/update_backlog_depth_dashboard.py"
            )
        return 0

    if args.receipt is not None:
        args.json.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    if updated_html != html_text:
        args.html.write_text(updated_html, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
