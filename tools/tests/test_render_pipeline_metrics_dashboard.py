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
        Path(__file__).resolve().parents[1] / "render_pipeline_metrics_dashboard.py"
    )
    spec = importlib.util.spec_from_file_location(
        "render_pipeline_metrics_dashboard", module_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _distribution(
    *,
    count: int = 2,
    total: float = 30,
    median: float = 15,
    p75: float = 18,
    p90: float = 20,
) -> dict[str, int | float]:
    return {
        "count": count,
        "total": total,
        "median": median,
        "p75": p75,
        "p90": p90,
    }


def _source() -> dict[str, object]:
    return {
        "schema_version": 1,
        "metric_version": "lifecycle_case_metrics_v1",
        "cohort_id": "gpt-5.6-shadow-1",
        "generated_at": "2026-07-21T14:00:00Z",
        "version_boundaries": {
            "mixed_system_fingerprints": True,
            "system_fingerprint_count": 2,
        },
        "version_warnings": [{"code": "mixed_system_fingerprints"}],
        "by_disposition": {
            "already_addressed": {
                "case_count": 2,
                "case_distributions": {
                    "raw_to_disposition_seconds": _distribution(total=100, median=48),
                    "lifecycle_wall_seconds": _distribution(total=90, median=44),
                    "lineage_to_disposition_seconds": _distribution(
                        total=110, median=52
                    ),
                    "direct_active_seconds": _distribution(total=30, median=15),
                    "direct_total_tokens": _distribution(total=2_000, median=1_000),
                    "inclusive_total_tokens": _distribution(total=2_600, median=1_300),
                    "all_in_total_tokens": _distribution(total=3_000, median=1_500),
                    "error_clusters": _distribution(total=4, median=2),
                    "supervisor_interventions": _distribution(total=1, median=0.5),
                    "manual_actions": _distribution(total=3, median=1.5),
                    "manual_active_seconds": _distribution(total=600, median=300),
                },
                "nonduplicative_accounting": {
                    "all_in": {"gross": {"total_tokens": 2_300}}
                },
                "errors": {
                    "cluster_count": 4,
                    "occurrence_count": 7,
                    "self_healed_cluster_count": 2,
                    "externally_resolved_cluster_count": 1,
                    "unresolved_terminal_cluster_count": 1,
                },
                "interventions": {"count": 1},
                "manual_actions": {
                    "count": 3,
                    "required_for_progress_count": 2,
                    "policy_mandated_count": 1,
                    "active_seconds": _distribution(total=600, median=300),
                },
                "automation_score_v1": {
                    "certified_case_count": 1,
                    "withheld_case_count": 1,
                    "gross": _distribution(total=150, median=75, p90=90),
                    "avoidable": _distribution(total=170, median=85, p90=95),
                    "touchless_terminal_yield": 0.5,
                    "pipeline_autonomous_rate": 0.5,
                    "human_touch_free_rate": 0.5,
                },
                "completeness": {
                    "case_count": 2,
                    "counts": {"required_milestones_complete": 1},
                    "ratios": {"required_milestones_complete": 0.5},
                    "complete": False,
                },
            },
            "pr": {
                "case_count": 1,
                "case_distributions": {
                    "lifecycle_wall_seconds": _distribution(
                        count=1, total=7200, median=7200, p75=7200, p90=7200
                    ),
                    "all_in_total_tokens": _distribution(
                        count=1, total=12_345, median=12_345, p75=12_345, p90=12_345
                    ),
                },
            },
        },
        "comparisons": [
            {
                "comparison_id": "before-after-1",
                "before_cohort_id": "gpt-5.6-shadow-0",
                "after_cohort_id": "gpt-5.6-shadow-1",
                "before_fingerprint": "fp-old",
                "after_fingerprint": "fp-new",
                "disposition": "already_addressed",
                "metric": "all_in_total_tokens.median",
                "objective": "decrease",
                "before": 2_000,
                "after": 1_500,
                "before_sample_size": 3,
                "after_sample_size": 2,
                "coverage": 0.8,
            }
        ],
        "recommendations": ["This untrusted field must never be rendered."],
    }


def test_projection_preserves_accounting_views_and_unknowns() -> None:
    mod = _load_module()

    projection = mod.build_dashboard_projection(_source())

    assert projection["schema_version"] == 4
    assert projection["document_type"] == "generated_pipeline_metrics_dashboard"
    assert projection["source"]["mixed_version_lifecycle"] is True
    assert projection["source"]["version_warning_codes"] == [
        "mixed_system_fingerprints"
    ]
    assert [row["disposition"] for row in projection["dispositions"]] == list(
        mod.DISPOSITIONS
    )
    already = projection["dispositions"][0]
    assert already["tokens"]["direct"]["median"] == 1_000
    assert already["tokens"]["inclusive"]["median"] == 1_300
    assert already["tokens"]["nonduplicative"]["total"] == 2_300
    assert already["tokens"]["all_in"]["median"] == 1_500
    assert already["errors"]["self_healed_clusters"] == 2
    assert already["interventions"]["supervising_agent"]["total"] == 1
    assert already["manual_actions"]["required_for_progress"] == 2
    assert already["automation_score_v1"]["gross"]["median"] == 75
    assert already["completeness"]["coverage"] == 0.5

    absent = projection["dispositions"][1]
    assert absent["disposition"] == "non_actionable"
    assert absent["case_count"] == 0
    assert absent["tokens"]["all_in"]["total"] is None
    assert absent["timing"]["raw_to_disposition_seconds"]["median"] is None
    assert absent["completeness"]["status"] == "unknown"


def test_empty_distributions_render_as_unknown_instead_of_measured_zero() -> None:
    mod = _load_module()
    source = _source()
    already = source["by_disposition"]["already_addressed"]
    empty = {
        "count": 0,
        "total": 0.0,
        "median": None,
        "p75": None,
        "p90": None,
    }
    already["case_distributions"]["all_in_total_tokens"] = empty
    already["case_distributions"]["manual_actions"] = empty
    already["case_distributions"]["manual_active_seconds"] = empty

    projection = mod.build_dashboard_projection(source)
    row = projection["dispositions"][0]

    assert row["tokens"]["all_in"]["total"] is None
    assert row["manual_actions"]["actions"]["total"] is None
    assert row["manual_actions"]["active_seconds"]["total"] is None


def test_active_cohort_burden_is_visible_before_final_disposition() -> None:
    mod = _load_module()
    source = _source()
    source.update(
        {
            "case_count": 5,
            "active_case_count": 5,
            "active_case_lifecycle_ids": [f"lifecycle:{index}" for index in range(5)],
            "by_disposition": {},
            "case_distributions": {
                "error_clusters": _distribution(
                    count=5, total=3, median=0, p75=1, p90=2
                ),
                "supervisor_interventions": _distribution(
                    count=5, total=0, median=0, p75=0, p90=0
                ),
                "manual_actions": _distribution(
                    count=5, total=133, median=8, p75=41, p90=76
                ),
                "manual_active_seconds": {
                    "count": 0,
                    "total": 0.0,
                    "median": None,
                    "p75": None,
                    "p90": None,
                },
            },
            "errors": {
                "cluster_count": 3,
                "occurrence_count": 3,
                "self_healed_cluster_count": 0,
                "externally_resolved_cluster_count": 3,
                "unresolved_terminal_cluster_count": 0,
            },
            "interventions": {"count": 0},
            "manual_actions": {
                "count": 133,
                "required_for_progress_count": 122,
                "policy_mandated_count": 0,
                "passive_observation_count": 6,
                "measurement_administration_count": 26,
                "avoidable_count": 133,
                "unavoidable_count": 0,
                "unclassified_count": 0,
            },
            "automation_score_v1": {
                "certified_case_count": 0,
                "withheld_case_count": 5,
                "terminal_case_count": 0,
            },
            "completeness": {
                "case_count": 5,
                "complete": False,
                "ratios": {"required_milestones_complete": 0.0},
            },
            "reconciliation": {"ok": False},
        }
    )

    projection = mod.build_dashboard_projection(source)
    summary = projection["cohort_summary"]

    assert summary["case_count"] == 5
    assert summary["active_case_count"] == 5
    assert summary["disposition_pending_case_count"] == 5
    assert summary["errors"]["clusters"]["total"] == 3
    assert summary["errors"]["occurrences"] == 3
    assert summary["manual_actions"]["actions"]["total"] == 133
    assert summary["manual_actions"]["required_for_progress"] == 122
    assert summary["automation_score_v1"]["withheld_case_count"] == 5
    assert summary["reconciliation_ok"] is False

    html = mod.render_dashboard_html(projection)
    assert "All observed cases" in html
    assert "Active lifecycles" in html
    assert "Disposition pending" in html
    assert "Error clusters" in html
    assert "Manual actions" in html
    assert ">133<" in html
    assert ">incomplete<" in html


def test_comparison_deltas_are_computed_factually() -> None:
    mod = _load_module()

    comparison = mod.build_dashboard_projection(_source())["comparisons"][0]

    assert comparison["absolute_delta"] == -500
    assert comparison["percentage_delta"] == -25
    assert comparison["observed_direction"] == "improved"
    assert comparison["before_sample_size"] == 3
    assert comparison["after_sample_size"] == 2


def test_reporter_compare_cohorts_document_is_flattened_without_causal_claims() -> None:
    mod = _load_module()
    source = _source()
    source.pop("comparisons")
    comparison_document = {
        "schema_version": 1,
        "metric_version": "lifecycle_case_metrics_v1",
        "comparison_kind": "factual_before_after",
        "before": {
            "cohort_id": "before",
            "case_count": 4,
            "system_fingerprints": ["fp-old"],
        },
        "after": {
            "cohort_id": "after",
            "case_count": 5,
            "system_fingerprints": ["fp-new"],
        },
        "system_fingerprint_comparison": {
            "before": ["fp-old"],
            "after": ["fp-new"],
        },
        "factual_deltas": {
            "case_count": 1,
            "disposition_counts": {"pr": 1},
            "errors": {"cluster_count": -2},
            "manual_actions": {"count": -3},
        },
        "per_disposition": {
            "pr": {
                "before_case_count": 1,
                "after_case_count": 2,
                "case_count_delta": 1,
                "accounting_delta": {"all_in": {"gross": {"total_tokens": -500}}},
            }
        },
    }

    projection = mod.build_dashboard_projection(
        source, comparison_values=[comparison_document]
    )

    pr_count = next(
        row
        for row in projection["comparisons"]
        if row["disposition"] == "pr" and row["metric"] == "case_count"
    )
    assert pr_count["absolute_delta"] == 1
    assert pr_count["before_sample_size"] == 1
    assert pr_count["after_sample_size"] == 2
    accounting = next(
        row
        for row in projection["comparisons"]
        if row["disposition"] == "pr"
        and row["metric"] == "accounting_delta.all_in.gross.total_tokens"
    )
    assert accounting["absolute_delta"] == -500
    assert accounting["observed_direction"] == "unknown"
    assert accounting["before_fingerprint"] == "fp-old"
    assert len(projection["source"]["comparison_sha256s"]) == 1


def test_html_is_standalone_and_does_not_render_recommendation_fields() -> None:
    mod = _load_module()
    projection = mod.build_dashboard_projection(_source())

    html = mod.render_dashboard_html(projection)

    assert html.startswith("<!doctype html>")
    assert "Already Addressed" in html
    assert ">PR<" in html
    assert "Direct tokens" in html
    assert "Supervisor interventions" in html
    assert "Automation avoidable" in html
    assert "Telemetry completeness" in html
    assert "Version boundary warning" in html
    assert "mixed_system_fingerprints" in html
    assert "unknown" in html
    assert "fp-old" in html
    assert "improved" in html
    assert "This untrusted field must never be rendered." not in html
    assert "<script" not in html


def test_projection_rejects_invalid_or_ambiguous_source_values() -> None:
    mod = _load_module()
    bad_count = deepcopy(_source())
    bad_count["by_disposition"]["already_addressed"]["case_count"] = -1

    with pytest.raises(
        mod.GeneratedDashboardContractError, match="must not be negative"
    ):
        mod.build_dashboard_projection(bad_count)

    bad_disposition = deepcopy(_source())
    bad_disposition["by_disposition"]["mystery"] = {"case_count": 1}
    with pytest.raises(
        mod.GeneratedDashboardContractError, match="unsupported disposition"
    ):
        mod.build_dashboard_projection(bad_disposition)


def test_materialization_is_deterministic_and_check_detects_stale_output(
    tmp_path: Path,
) -> None:
    mod = _load_module()
    source_path = tmp_path / "cohort_metrics.json"
    html_path = tmp_path / "dashboard.html"
    json_path = tmp_path / "dashboard.json"
    source_path.write_text(json.dumps(_source()), encoding="utf-8")

    projection = mod.materialize_dashboard(
        source_path,
        html_output=html_path,
        json_output=json_path,
    )
    first_html = html_path.read_bytes()
    first_json = json_path.read_bytes()

    mod.materialize_dashboard(
        source_path,
        html_output=html_path,
        json_output=json_path,
    )
    assert html_path.read_bytes() == first_html
    assert json_path.read_bytes() == first_json
    assert json.loads(first_json)["source"]["sha256"] == projection["source"]["sha256"]

    mod.materialize_dashboard(
        source_path,
        html_output=html_path,
        json_output=json_path,
        check=True,
    )
    html_path.write_text("stale", encoding="utf-8")
    with pytest.raises(mod.GeneratedDashboardContractError, match="stale or missing"):
        mod.materialize_dashboard(
            source_path,
            html_output=html_path,
            json_output=json_path,
            check=True,
        )


def test_cli_writes_default_artifact_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load_module()
    source_path = tmp_path / "cohort_metrics.json"
    source_path.write_text(json.dumps(_source()), encoding="utf-8")

    assert mod.main([str(source_path)]) == 0
    assert (tmp_path / "metrics_dashboard.json").is_file()
    assert (tmp_path / "metrics_dashboard.html").is_file()
    assert "wrote" in capsys.readouterr().out
    assert mod.main([str(source_path), "--check"]) == 0
