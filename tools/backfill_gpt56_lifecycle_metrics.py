"""Backfill legacy schema-v3 dashboard evidence into lifecycle telemetry.

This importer is intentionally conservative. It preserves structured historical
counts and timestamps, derives case dispositions only from explicit retained
case/source evidence, and leaves token, origin, and manual-action fields unknown
when the legacy receipt did not retain them. It never promotes hand-authored
totals to authoritative v4 data.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _source in reversed(
    (
        _REPO_ROOT / "packages" / "run_artifacts" / "src",
        _REPO_ROOT / "packages" / "token_monitoring" / "src",
    )
):
    if str(_source) not in sys.path:
        sys.path.insert(0, str(_source))

from run_artifacts.lifecycle_events import (  # noqa: E402
    LifecycleContext,
    append_lifecycle_events,
    canonical_sha256,
    make_lifecycle_event,
)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x00".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}:{sha256(payload).hexdigest()}"


def _measure(run: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = run.get(name)
    return value if isinstance(value, Mapping) else {}


def _cluster_ids(
    measure: Mapping[str, Any], *, run_id: str, family: str
) -> list[str]:
    raw = measure.get("cluster_ids")
    if isinstance(raw, list):
        return [str(item) for item in raw if isinstance(item, str) and item.strip()]
    count = measure.get("count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return [f"legacy:{run_id}:{family}:{index + 1}" for index in range(count)]
    return []


def _case_disposition(
    run: Mapping[str, Any], *, sources_by_id: Mapping[str, Mapping[str, Any]]
) -> tuple[str | None, list[str]]:
    if run.get("lifecycle_kind") != "case":
        return None, []
    source_ids = [
        str(item)
        for item in run.get("source_ids", [])
        if isinstance(item, str) and item.strip()
    ]
    pr_evidence: list[str] = []
    for source_id in source_ids:
        source = sources_by_id.get(source_id, {})
        pr_url = source.get("pr_url")
        if isinstance(pr_url, str) and pr_url.strip():
            pr_evidence.append(f"sources[{source_id}].pr_url")
            continue
        url = source.get("url")
        if isinstance(url, str) and "/pull/" in url:
            pr_evidence.append(f"sources[{source_id}].url")
    if pr_evidence:
        return "pr", pr_evidence

    current_state = str(run.get("current_state") or "")
    normalized = current_state.casefold()
    if "not_required/non_actionable" in normalized:
        return "non_actionable", ["current_state:not_required/non_actionable"]
    if "not_required/already_addressed" in normalized:
        return "already_addressed", ["current_state:not_required/already_addressed"]
    if "already_addressed" in normalized:
        return "already_addressed", ["current_state:already_addressed"]
    return None, []


def _selected_by_since(run: Mapping[str, Any], since: datetime | None) -> bool:
    if since is None:
        return True
    timing = run.get("timing")
    timing = timing if isinstance(timing, Mapping) else {}
    observed = _parse_time(timing.get("end_at")) or _parse_time(timing.get("start_at"))
    return observed is not None and observed >= since


def backfill_dashboard(
    *,
    dashboard_path: Path,
    output_dir: Path,
    since: datetime | None = None,
    run_ids: set[str] | None = None,
) -> dict[str, Any]:
    source = _read_object(dashboard_path)
    dashboard = source.get("operational_dashboard")
    if not isinstance(dashboard, Mapping) or dashboard.get("schema_version") != 3:
        raise ValueError("backfill requires operational_dashboard schema_version 3")
    runs = dashboard.get("runs")
    if not isinstance(runs, list):
        raise ValueError("operational_dashboard.runs must be a list")
    output_dir.mkdir(parents=True, exist_ok=True)
    final_events_path = output_dir / "lifecycle_events.jsonl"
    events_path = output_dir / ".lifecycle_events.jsonl.backfill.tmp"
    events_path.unlink(missing_ok=True)
    retained_events: list[Any] = []
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    raw_sources = source.get("sources")
    source_rows = raw_sources if isinstance(raw_sources, list) else []
    sources_by_id = {
        str(item["id"]): item
        for item in source_rows
        if isinstance(item, Mapping)
        and isinstance(item.get("id"), str)
    }

    for raw in runs:
        if not isinstance(raw, Mapping) or raw.get("entry_kind") != "lifecycle_run":
            continue
        run_id = str(raw.get("run_id") or "").strip()
        if not run_id:
            continue
        if run_ids is not None and run_id not in run_ids:
            skipped.append({"run_id": run_id, "reason": "not_explicitly_selected"})
            continue
        if not _selected_by_since(raw, since):
            skipped.append({"run_id": run_id, "reason": "outside_or_unknown_time_window"})
            continue

        timing = raw.get("timing")
        timing = timing if isinstance(timing, Mapping) else {}
        started = _parse_time(timing.get("start_at"))
        ended = _parse_time(timing.get("end_at"))
        fallback = ended or started or _parse_time(dashboard.get("as_of"))
        if fallback is None:
            skipped.append({"run_id": run_id, "reason": "no_timestamp_for_event_contract"})
            continue
        lifecycle_kind = str(raw.get("lifecycle_kind") or "pipeline_cycle")
        source_lifecycle_id = str(raw.get("lifecycle_id") or run_id)
        disposition, disposition_evidence = _case_disposition(
            raw, sources_by_id=sources_by_id
        )
        context = LifecycleContext(
            case_lifecycle_id=(
                f"legacy:{source_lifecycle_id}" if lifecycle_kind == "case" else None
            ),
            case_id=(
                str(raw.get("case_id"))
                if lifecycle_kind == "case" and isinstance(raw.get("case_id"), str)
                else None
            ),
            cycle_id=(
                f"legacy:{source_lifecycle_id}"
                if lifecycle_kind != "case"
                else _stable_id("legacy-cycle", run_id)
            ),
            stage="legacy_backfill",
            work_unit_id=_stable_id("legacy-work", run_id),
            # Dashboard v3 did not retain the required code/model/prompt/config/policy
            # fingerprint. The source schema is provenance, not a substitute fingerprint.
            system_fingerprint={},
        )
        common = {
            "actor_type": "unknown",
            "initiator_type": "unknown",
            "root_initiator_type": "unknown",
            "origin": "unknown_external",
            "provenance_quality": "operator_attested",
        }
        cohort_attributes = {
            "lifecycle_kind": lifecycle_kind,
            "case_cohort_eligible": lifecycle_kind == "case",
        }
        if started is not None:
            retained_events.append(
                make_lifecycle_event(
                    "lifecycle.opened",
                    context,
                    idempotency_key=f"legacy:{run_id}:opened",
                    occurred_at=_iso(started),
                    started_at=_iso(started),
                    attributes={
                        **cohort_attributes,
                        "legacy_run_id": run_id,
                        "origin_telemetry_complete": False,
                    },
                    **common,
                ),
            )

        error_ids = _cluster_ids(
            _measure(raw, "errors"), run_id=run_id, family="error"
        )
        self_healed_ids = set(
            _cluster_ids(
                _measure(raw, "automatic_self_corrections"),
                run_id=run_id,
                family="self-healed",
            )
        )
        intervention_ids = _cluster_ids(
            _measure(raw, "supervisor_interventions"),
            run_id=run_id,
            family="supervisor-intervention",
        )
        intervention_error_ids = set(intervention_ids) & set(error_ids)
        for cluster_id in error_ids:
            retained_events.append(
                make_lifecycle_event(
                    "error.occurred",
                    context,
                    idempotency_key=f"legacy:{run_id}:error:{cluster_id}",
                    occurred_at=_iso(fallback),
                    error_cluster_id=cluster_id,
                    attributes={
                        **cohort_attributes,
                        "error_kind": "legacy_attested_cluster",
                        "legacy_self_healed": cluster_id in self_healed_ids,
                        "resolution_evidence_unknown": (
                            cluster_id not in self_healed_ids
                            and cluster_id not in intervention_error_ids
                        ),
                        "resolution_mode": (
                            "open" if cluster_id not in self_healed_ids else "unknown"
                        ),
                    },
                    **common,
                ),
            )

        for cluster_id in sorted(set(error_ids) & self_healed_ids):
            retained_events.append(
                make_lifecycle_event(
                    "error.resolved",
                    context,
                    idempotency_key=f"legacy:{run_id}:self-healed:{cluster_id}",
                    occurred_at=_iso(fallback),
                    error_cluster_id=cluster_id,
                    attributes={
                        **cohort_attributes,
                        "resolution_mode": "self_healed_same_author",
                        "resolution_cost_attribution_complete": False,
                    },
                    **common,
                ),
            )

        for intervention_id in intervention_ids:
            intervention_context = LifecycleContext.from_dict(
                {
                    **context.to_dict(),
                    "work_unit_id": _stable_id(
                        "legacy-intervention-work", run_id, intervention_id
                    ),
                }
            )
            retained_events.append(
                make_lifecycle_event(
                    "intervention.completed",
                    intervention_context,
                    idempotency_key=(
                        f"legacy:{run_id}:intervention:{intervention_id}"
                    ),
                    occurred_at=_iso(fallback),
                    actor_type="supervising_agent",
                    initiator_type="supervising_agent",
                    root_initiator_type="supervising_agent",
                    origin="supervising_agent",
                    intervention_id=intervention_id,
                    provenance_quality="operator_attested",
                    attributes={
                        **cohort_attributes,
                        "intervention_kind": "legacy_attested_cluster",
                        "required_for_progress": True,
                        "active_seconds": None,
                        "error_cluster_ids": (
                            [intervention_id]
                            if intervention_id in intervention_error_ids
                            else []
                        ),
                    },
                ),
            )
            action_id = _stable_id("legacy-action", run_id, intervention_id)
            action_context = LifecycleContext.from_dict(
                {
                    **intervention_context.to_dict(),
                    "parent_action_id": action_id,
                }
            )
            retained_events.append(
                make_lifecycle_event(
                    "action.completed",
                    action_context,
                    idempotency_key=(
                        f"legacy:{run_id}:intervention-action:{intervention_id}"
                    ),
                    occurred_at=_iso(fallback),
                    started_at=_iso(fallback),
                    ended_at=_iso(fallback),
                    actor_type="supervising_agent",
                    initiator_type="supervising_agent",
                    root_initiator_type="supervising_agent",
                    origin="supervising_agent",
                    intervention_id=intervention_id,
                    provenance_quality="operator_attested",
                    attributes={
                        **cohort_attributes,
                        "action_id": action_id,
                        "action_family": "adjudication",
                        "operation": "legacy_supervisor_intervention",
                        "interface": "schema_v3_dashboard_backfill",
                        "required_for_progress": True,
                        "active_seconds": None,
                        "active_seconds_source": "unknown",
                        "resource_time_unknown": True,
                        "resource_time_unknown_reason": (
                            "legacy_manual_action_active_time_not_retained"
                        ),
                        "legacy_action_cardinality": (
                            "minimum_one_action_per_intervention"
                        ),
                        "manual_action_telemetry_complete": False,
                    },
                ),
            )

        rework = raw.get("rework")
        rework = rework if isinstance(rework, Mapping) else {}
        invocation_count = rework.get("author_invocations")
        if isinstance(invocation_count, int) and not isinstance(invocation_count, bool):
            for index in range(invocation_count):
                invocation_context = LifecycleContext.from_dict(
                    {
                        **context.to_dict(),
                        "invocation_id": f"legacy:{run_id}:invocation:{index + 1}",
                        "work_unit_id": _stable_id(
                            "legacy-invocation-work", run_id, index + 1
                        ),
                    }
                )
                retained_events.append(
                    make_lifecycle_event(
                        "model.invocation.completed",
                        invocation_context,
                        idempotency_key=f"legacy:{run_id}:invocation:{index + 1}",
                        occurred_at=_iso(fallback),
                        attributes={
                            **cohort_attributes,
                            "usage_semantics": "unattributable",
                            "token_usage": None,
                            "legacy_timestamp_exact": False,
                        },
                        **common,
                    ),
                )

        if disposition is not None:
            retained_events.append(
                make_lifecycle_event(
                    "disposition.verified",
                    context,
                    idempotency_key=f"legacy:{run_id}:disposition:{disposition}",
                    occurred_at=_iso(ended or fallback),
                    attributes={
                        **cohort_attributes,
                        "disposition": disposition,
                        "verified": True,
                        "closure_valid": True,
                        "disposition_evidence": disposition_evidence,
                        "historical_product_implementation_cost_included": (
                            disposition == "pr"
                        ),
                    },
                    **{
                        **common,
                        "provenance_quality": "artifact_derived",
                    },
                ),
            )

        if ended is not None:
            retained_events.append(
                make_lifecycle_event(
                    "lifecycle.closed",
                    context,
                    idempotency_key=f"legacy:{run_id}:closed",
                    occurred_at=_iso(ended),
                    started_at=_iso(started) if started is not None else _iso(ended),
                    ended_at=_iso(ended),
                    attributes={
                        **cohort_attributes,
                        "legacy_run_id": run_id,
                        "disposition": disposition,
                        # Every legacy row represents case work, but schema v3 did
                        # not retain its complete token or active-time receipts.
                        # Mark the synthetic base work unit unknown so an empty
                        # invocation list can never be misreported as zero cost.
                        "cost_unknown": True,
                        "cost_unknown_reason": (
                            "historical_token_and_active_time_not_retained"
                        ),
                        "closure_correctness": (
                            "artifact_derived" if disposition is not None else "unknown"
                        ),
                        "attested_self_healed_cluster_ids": sorted(
                            self_healed_ids - set(error_ids)
                        ),
                        "manual_action_telemetry_complete": False,
                    },
                    **common,
                ),
            )

        selected.append(
            {
                "run_id": run_id,
                "source_lifecycle_id": source_lifecycle_id,
                "provenance": {
                    "timing": (
                        "operator_attested" if started is not None or ended is not None else "unknown"
                    ),
                    "error_clusters": (
                        "operator_attested" if error_ids else "unknown"
                    ),
                    "self_healed_errors": (
                        "operator_attested" if self_healed_ids else "unknown"
                    ),
                    "interventions": (
                        "operator_attested" if intervention_ids else "unknown"
                    ),
                    "tokens": "unknown",
                    "manual_actions": (
                        "operator_attested_minimum"
                        if intervention_ids
                        else "unknown"
                    ),
                    "origin": "unknown",
                    "disposition": (
                        "artifact_derived" if disposition is not None else "unknown"
                    ),
                },
                "disposition": disposition,
                "disposition_evidence": disposition_evidence,
            }
        )

    append_lifecycle_events(events_path, retained_events)
    events_path.replace(final_events_path)
    source_digest = sha256(dashboard_path.read_bytes()).hexdigest()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "legacy_lifecycle_backfill_manifest",
        "cohort_label": "gpt-5.6-approximate-window",
        "selection_basis": {
            "since": _iso(since) if since is not None else None,
            "run_ids": sorted(run_ids) if run_ids is not None else None,
            "note": "The window is operator-selected and is not asserted as an exact release timestamp.",
        },
        "source": {
            "path": str(dashboard_path.resolve()),
            "sha256": source_digest,
            "schema_version": 3,
        },
        "event_log_path": str(final_events_path.resolve()),
        "selected_runs": selected,
        "skipped_runs": skipped,
        "certification": {
            "eligible": False,
            "reason": "legacy manual origin, token, action, and disposition evidence is incomplete",
        },
    }
    manifest["content_sha256"] = canonical_sha256(manifest)
    (output_dir / "backfill_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--since")
    parser.add_argument("--run-id", action="append", default=[])
    args = parser.parse_args()
    since = _parse_time(args.since) if args.since else None
    if args.since and since is None:
        raise ValueError("--since must be an ISO-8601 timestamp with timezone")
    backfill_dashboard(
        dashboard_path=args.dashboard,
        output_dir=args.output_dir,
        since=since,
        run_ids=set(args.run_id) if args.run_id else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
