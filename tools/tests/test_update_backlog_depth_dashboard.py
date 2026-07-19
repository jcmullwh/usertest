from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest


def _load_module() -> ModuleType:
    module_path = (
        Path(__file__).resolve().parents[1] / "update_backlog_depth_dashboard.py"
    )
    spec = importlib.util.spec_from_file_location(
        "update_backlog_depth_dashboard", module_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _measure(count: int | None, summary: str) -> dict[str, object]:
    return {
        "count": count,
        "count_kind": "deduplicated_clusters",
        "summary": summary,
    }


def _run(
    run_id: str,
    *,
    entry_kind: str = "lifecycle_run",
    parent_run_id: str | None = None,
    errors: int = 0,
) -> dict[str, object]:
    run: dict[str, object] = {
        "run_id": run_id,
        "entry_kind": entry_kind,
        "label": f"Label {run_id}",
        "scope": f"Scope {run_id}",
        "timing": {
            "elapsed_seconds": 1.5,
            "coverage": "complete",
            "display": "1.50 s",
            "note": "Complete wall time.",
        },
        "furthest_stage": "Stage 3",
        "current_state": f"State {run_id}",
        "outcome": f"Outcome {run_id}",
        "errors": _measure(errors, f"Errors {run_id}"),
        "automatic_self_corrections": _measure(0, f"Corrections {run_id}"),
        "supervisor_interventions": _measure(1, f"Interventions {run_id}"),
        "next_action": f"Next {run_id}",
        "tone": "warn",
        "data_quality": "retained fixture evidence",
        "source_ids": ["source-1"],
    }
    if parent_run_id is not None:
        run["parent_run_id"] = parent_run_id
    if entry_kind == "lifecycle_run":
        run["lifecycle_id"] = f"lifecycle:{run_id}"
        run["lifecycle_kind"] = "case"
        run["rework"] = {
            "author_invocations": 1,
            "continuation_launches": 0,
            "stage_reruns": 0,
            "full_restarts": 0,
            "summary": f"Rework {run_id}",
        }
    return run


def _dashboard(*runs: dict[str, object], current: str = "current") -> dict[str, object]:
    return {
        "schema_version": 3,
        "as_of": "2026-07-16T00:00:00Z",
        "current_run_id": current,
        "counting_rule": "One visible row is one lifecycle run.",
        "summary_cards": [
            {
                "label": f"Card {index}",
                "value": "Value",
                "detail": "Detail",
                "tone": "warn",
            }
            for index in range(4)
        ],
        "runs": list(runs),
    }


def test_supporting_records_are_retained_but_not_rendered_as_peer_rows() -> None:
    mod = _load_module()
    current = _run("current", errors=2)
    support = _run(
        "preflight",
        entry_kind="supporting_activity",
        parent_run_id="current",
        errors=7,
    )
    baseline = _run("baseline", entry_kind="baseline", errors=5)
    dashboard = _dashboard(baseline, current, support)

    mod.validate_dashboard(dashboard, source_ids={"source-1"})
    rendered = mod._render_dashboard(dashboard)

    assert "Label current" in rendered
    assert "Label preflight" not in rendered
    assert "Label baseline" not in rendered
    assert rendered.count("<tr class=") == 1
    assert "1 lifecycle runs" in rendered
    assert "1 supporting records" in rendered
    assert "1 historical baseline" in rendered
    assert "<strong>2</strong>" in rendered
    assert "<strong>7</strong>" not in rendered


def test_supporting_record_may_follow_current_lifecycle_run() -> None:
    mod = _load_module()
    dashboard = _dashboard(
        _run("current"),
        _run(
            "later-audit",
            entry_kind="supporting_activity",
            parent_run_id="current",
        ),
    )

    validated = mod.validate_dashboard(dashboard, source_ids={"source-1"})

    assert validated["current_run_id"] == "current"


@pytest.mark.parametrize(
    ("support_parent", "extra_run", "message"),
    [
        ("missing", None, "references missing parent_run_id"),
        (
            "other-support",
            _run(
                "other-support",
                entry_kind="supporting_activity",
                parent_run_id="current",
            ),
            "parent_run_id must identify a lifecycle_run",
        ),
        ("self", None, "parent_run_id must identify a lifecycle_run"),
    ],
)
def test_supporting_parent_must_be_an_existing_lifecycle_run(
    support_parent: str,
    extra_run: dict[str, object] | None,
    message: str,
) -> None:
    mod = _load_module()
    support = _run(
        "self" if support_parent == "self" else "support",
        entry_kind="supporting_activity",
        parent_run_id=support_parent,
    )
    runs = [_run("current")]
    if extra_run is not None:
        runs.append(extra_run)
    runs.append(support)
    dashboard = _dashboard(*runs)

    with pytest.raises(mod.DashboardContractError, match=message):
        mod.validate_dashboard(dashboard, source_ids={"source-1"})


def test_current_run_id_must_identify_a_lifecycle_run() -> None:
    mod = _load_module()
    dashboard = _dashboard(
        _run("actual"),
        _run(
            "current-support",
            entry_kind="supporting_activity",
            parent_run_id="actual",
        ),
        current="current-support",
    )

    with pytest.raises(
        mod.DashboardContractError,
        match="current_run_id must identify a lifecycle_run",
    ):
        mod.validate_dashboard(dashboard, source_ids={"source-1"})


def test_supporting_receipt_preserves_current_run_and_summary_cards(
    tmp_path: Path,
) -> None:
    mod = _load_module()
    dashboard = _dashboard(_run("current"))
    payload = {"sources": [{"id": "source-1"}], "operational_dashboard": dashboard}
    expected_cards = deepcopy(dashboard["summary_cards"])
    receipt = {
        "schema_version": 3,
        "run": _run(
            "audit",
            entry_kind="supporting_activity",
            parent_run_id="current",
        ),
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    mod._apply_receipt(payload, receipt_path)
    validated = mod.validate_dashboard(
        payload["operational_dashboard"], source_ids={"source-1"}
    )

    assert validated["current_run_id"] == "current"
    assert validated["summary_cards"] == expected_cards
    assert validated["runs"][-1]["run_id"] == "audit"


def test_checked_in_dashboard_has_only_lifecycle_rows_in_generated_html() -> None:
    mod = _load_module()
    payload = mod._load_json(mod.DEFAULT_JSON)
    source_ids = {
        str(source["id"])
        for source in payload["sources"]
        if isinstance(source, dict) and source.get("id")
    }
    dashboard = mod.validate_dashboard(
        payload["operational_dashboard"], source_ids=source_ids
    )

    lifecycle_runs = mod._lifecycle_runs(dashboard)
    assert len(lifecycle_runs) == 15
    assert len({run["lifecycle_id"] for run in lifecycle_runs}) == 15
    assert mod._entry_count(dashboard, "supporting_activity") >= 1
    assert mod._entry_count(dashboard, "baseline") == 1
    assert dashboard["current_run_id"] == lifecycle_runs[-1]["run_id"]
    current = lifecycle_runs[-1]
    assert current["lifecycle_id"] == "pipeline-cycle:383cf41f:20260719"
    assert current["timing"]["start_at"] == "2026-07-19T13:15:00Z"
    assert current["timing"]["end_at"] is None
    assert current["rework"]["author_invocations"] == 43
    assert current["rework"]["continuation_launches"] == 11
    assert current["rework"]["stage_reruns"] == 6
    assert current["rework"]["full_restarts"] == 0
    assert current["errors"]["count"] == 25
    assert current["automatic_self_corrections"]["count"] == 7
    assert current["supervisor_interventions"]["count"] == 18
    assert current["furthest_stage"] == (
        "Corrected Stage 2 for the complete canonical frontier; three selected cases "
        "have completed their correct terminal Stage-3-to-ticket path"
    )

    rendered = mod._render_dashboard(dashboard)
    html_text = mod.DEFAULT_HTML.read_text(encoding="utf-8")
    assert mod._replace_generated_block(html_text, rendered) == html_text
    assert "Attempt-16 zero-model preflight 1" not in rendered
    assert "Single real-case Stage 3 run" not in rendered
    assert "Stage 3 same-author correction, attempt 14" not in rendered
    assert "Stage 3 same-author correction, attempt 15" not in rendered
    assert "Sources:" not in rendered
    assert "Restarts / rework" in rendered
    assert str(lifecycle_runs[-1]["label"]) in rendered


def test_lifecycle_requires_unique_identity_and_rework() -> None:
    mod = _load_module()
    first = _run("first")
    second = _run("second")
    second["lifecycle_id"] = first["lifecycle_id"]

    with pytest.raises(mod.DashboardContractError, match="duplicate.*lifecycle_id"):
        mod.validate_dashboard(
            _dashboard(first, second, current="second"), source_ids={"source-1"}
        )

    missing_rework = _run("current")
    missing_rework.pop("rework")
    with pytest.raises(mod.DashboardContractError, match=r"runs\[0\]\.rework"):
        mod.validate_dashboard(_dashboard(missing_rework), source_ids={"source-1"})


def test_deduplicated_cluster_ids_must_be_unique_and_match_count() -> None:
    mod = _load_module()
    current = _run("current")
    current["errors"] = {
        "count": 2,
        "count_kind": "deduplicated_clusters",
        "summary": "Two reconciled clusters.",
        "cluster_ids": ["cluster:one", "cluster:two"],
    }
    mod.validate_dashboard(_dashboard(current), source_ids={"source-1"})

    current["errors"]["cluster_ids"] = ["cluster:one", "cluster:one"]
    with pytest.raises(mod.DashboardContractError, match="cluster_ids must be unique"):
        mod.validate_dashboard(_dashboard(current), source_ids={"source-1"})

    current["errors"]["cluster_ids"] = ["cluster:one"]
    with pytest.raises(
        mod.DashboardContractError,
        match="count must equal the number of cluster_ids",
    ):
        mod.validate_dashboard(_dashboard(current), source_ids={"source-1"})


def test_renderer_keeps_detailed_provenance_out_of_the_visible_table() -> None:
    mod = _load_module()
    current = _run("current")
    current["scope"] = "PRIVATE SCOPE DETAIL"
    current["data_quality"] = "PRIVATE HASH AND TEST DETAIL"
    current["source_ids"] = ["source-1", "private-source-id"]
    current["current_state"] = "word " * 100

    rendered = mod._render_dashboard(_dashboard(current))

    assert "PRIVATE SCOPE DETAIL" not in rendered
    assert "PRIVATE HASH AND TEST DETAIL" not in rendered
    assert "private-source-id" not in rendered
    assert "…" in rendered


def test_legacy_measure_is_visibly_unknown_not_reinterpreted() -> None:
    mod = _load_module()
    legacy = {"count": 47, "count_kind": "raw findings", "summary": "Raw detail"}

    rendered = mod._render_measure(legacy)

    assert "Unknown" in rendered
    assert "legacy unreconciled" in rendered
    assert "47" not in rendered


def test_active_lifecycle_time_must_match_dashboard_as_of() -> None:
    mod = _load_module()
    current = _run("current")
    current["timing"] = {
        "start_at": "2026-07-15T23:59:58.500000Z",
        "end_at": None,
        "elapsed_seconds": 1.5,
        "coverage": "active",
        "display": "1.50 s",
        "note": "Active wall time.",
    }
    dashboard = _dashboard(current)

    mod.validate_dashboard(dashboard, source_ids={"source-1"})

    current["timing"]["elapsed_seconds"] = 1.4
    with pytest.raises(mod.DashboardContractError, match="as_of minus start_at"):
        mod.validate_dashboard(dashboard, source_ids={"source-1"})
