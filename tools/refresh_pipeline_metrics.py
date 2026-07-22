from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _source in reversed(
    (
        _REPO_ROOT / "packages" / "normalized_events" / "src",
        _REPO_ROOT / "packages" / "run_artifacts" / "src",
        _REPO_ROOT / "packages" / "reporter" / "src",
    )
):
    if str(_source) not in sys.path:
        sys.path.insert(0, str(_source))

from reporter.case_metrics import (  # noqa: E402
    AUTOMATION_SCORE_VERSION,
    CASE_METRICS_VERSION,
)
from reporter.materialize import (  # noqa: E402
    CASE_METRICS_FILENAME,
    COHORT_METRICS_FILENAME,
    discover_lifecycle_event_logs,
    materialize_lifecycle_metrics,
)

_OPEN_MANIFEST_STATUSES = {"active", "incomplete", "unreconciled"}
_DASHBOARD_JSON_FILENAME = "metrics_dashboard.json"
_DASHBOARD_HTML_FILENAME = "metrics_dashboard.html"
_DASHBOARD_RENDERER = _REPO_ROOT / "tools" / "render_pipeline_metrics_dashboard.py"


@dataclass(frozen=True)
class RefreshDecision:
    refresh: bool
    reasons: tuple[str, ...]
    event_sources: tuple[Path, ...]
    open_manifest_count: int


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _open_manifest_count(roots: Sequence[Path]) -> int:
    manifests: set[Path] = set()
    for root in roots:
        resolved = root.resolve()
        if resolved.is_file():
            resolved = resolved.parent
        if not resolved.exists():
            continue
        manifests.update(
            path.resolve()
            for path in resolved.rglob("lifecycle_manifest.json")
            if path.is_file() and not path.is_symlink()
        )
    count = 0
    for path in manifests:
        payload = _read_json(path)
        if payload is not None and payload.get("status") in _OPEN_MANIFEST_STATUSES:
            count += 1
    return count


def decide_refresh(
    *,
    roots: Sequence[Path],
    output_dir: Path,
    stale_after: timedelta,
    now: datetime,
    force: bool = False,
) -> RefreshDecision:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    if stale_after.total_seconds() < 0:
        raise ValueError("stale_after must be non-negative")
    sources = tuple(discover_lifecycle_event_logs(roots))
    open_count = _open_manifest_count(roots)
    reasons: list[str] = []
    if force:
        reasons.append("forced")
    case_path = output_dir.resolve() / CASE_METRICS_FILENAME
    cohort_path = output_dir.resolve() / COHORT_METRICS_FILENAME
    dashboard_json_path = output_dir.resolve() / _DASHBOARD_JSON_FILENAME
    dashboard_html_path = output_dir.resolve() / _DASHBOARD_HTML_FILENAME
    derived_paths = (
        case_path,
        cohort_path,
        dashboard_json_path,
        dashboard_html_path,
    )
    if any(not path.is_file() for path in derived_paths):
        reasons.append("derived_artifact_missing")

    case_payload = _read_json(case_path) if case_path.is_file() else None
    if (
        case_payload is not None
        and case_payload.get("metric_version") != CASE_METRICS_VERSION
    ):
        reasons.append("metric_definition_changed")
    cohort_payload = _read_json(cohort_path) if cohort_path.is_file() else None
    automation = (
        cohort_payload.get("automation_score_v1")
        if cohort_payload is not None
        else None
    )
    if cohort_payload is not None and (
        not isinstance(automation, dict)
        or automation.get("version") != AUTOMATION_SCORE_VERSION
    ):
        reasons.append("score_definition_changed")
    dashboard_payload = (
        _read_json(dashboard_json_path) if dashboard_json_path.is_file() else None
    )
    if dashboard_payload is not None and dashboard_payload.get("schema_version") != 4:
        reasons.append("dashboard_definition_changed")

    if sources and all(path.is_file() for path in derived_paths):
        derived_mtime = min(path.stat().st_mtime for path in derived_paths)
        if any(source.stat().st_mtime > derived_mtime for source in sources):
            reasons.append("source_telemetry_changed")
        derived_at = datetime.fromtimestamp(derived_mtime, tz=timezone.utc)
        if open_count and now.astimezone(timezone.utc) - derived_at >= stale_after:
            reasons.append("open_lifecycle_daily_refresh_due")

    return RefreshDecision(
        refresh=bool(reasons),
        reasons=tuple(dict.fromkeys(reasons)),
        event_sources=sources,
        open_manifest_count=open_count,
    )


def _render_dashboard(
    *,
    cohort_metrics_path: Path,
    output_dir: Path,
    comparison_path: Path | None,
) -> tuple[Path, Path]:
    json_path = output_dir.resolve() / _DASHBOARD_JSON_FILENAME
    html_path = output_dir.resolve() / _DASHBOARD_HTML_FILENAME
    command = [
        sys.executable,
        str(_DASHBOARD_RENDERER),
        str(cohort_metrics_path),
        "--json-output",
        str(json_path),
        "--html-output",
        str(html_path),
    ]
    if comparison_path is not None:
        command.extend(("--comparison", str(comparison_path)))
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"generated dashboard refresh failed: {detail}")
    return json_path, html_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh observational pipeline metrics; this command never creates or "
            "executes remediation work."
        )
    )
    parser.add_argument("--root", action="append", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--cohort-id")
    parser.add_argument("--compare-to", type=Path)
    parser.add_argument("--stale-after-hours", type=float, default=24.0)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    decision = decide_refresh(
        roots=args.root,
        output_dir=args.output_dir,
        stale_after=timedelta(hours=args.stale_after_hours),
        now=datetime.now(timezone.utc),
        force=args.force,
    )
    if not decision.event_sources:
        raise SystemExit("no lifecycle_events.jsonl streams were discovered")
    if decision.refresh:
        result = materialize_lifecycle_metrics(
            event_sources=decision.event_sources,
            output_dir=args.output_dir,
            cohort_id=args.cohort_id,
            comparison_cohort=args.compare_to,
        )
        dashboard_json_path, dashboard_html_path = _render_dashboard(
            cohort_metrics_path=result.cohort_metrics_path,
            output_dir=args.output_dir,
            comparison_path=result.comparison_path,
        )
        payload = {
            "status": "refreshed",
            "reasons": list(decision.reasons),
            "open_manifest_count": decision.open_manifest_count,
            "source_event_count": result.source_event_count,
            "retained_event_count": result.retained_event_count,
            "case_metrics": str(result.case_metrics_path),
            "cohort_metrics": str(result.cohort_metrics_path),
            "metrics_dashboard_json": str(dashboard_json_path),
            "metrics_dashboard_html": str(dashboard_html_path),
        }
    else:
        payload = {
            "status": "current",
            "reasons": [],
            "open_manifest_count": decision.open_manifest_count,
            "event_stream_count": len(decision.event_sources),
        }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
