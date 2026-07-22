from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest


def _load_module() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "update_backlog_depth_dashboard.py"
    spec = importlib.util.spec_from_file_location(
        "update_backlog_depth_dashboard_metrics_entrypoint", module_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_existing_entrypoint_renders_generated_schema_v4_metrics(tmp_path: Path) -> None:
    mod = _load_module()
    source = tmp_path / "cohort_metrics.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "metric_version": "lifecycle_case_metrics_v1",
                "cohort_id": "shadow",
                "case_count": 0,
                "disposition_counts": {},
                "by_disposition": {},
                "version_boundaries": {
                    "mixed_system_fingerprints": False,
                    "system_fingerprint_count": 0,
                },
                "version_warnings": [],
            }
        ),
        encoding="utf-8",
    )
    html_output = tmp_path / "dashboard.html"
    json_output = tmp_path / "dashboard.json"
    arguments = [
        "--cohort-metrics",
        str(source),
        "--metrics-html-output",
        str(html_output),
        "--metrics-json-output",
        str(json_output),
    ]

    assert mod.main(arguments) == 0
    assert json.loads(json_output.read_text(encoding="utf-8"))["schema_version"] == 4
    assert mod.main([*arguments, "--check"]) == 0


def test_schema_v3_receipt_writes_require_explicit_legacy_flag(tmp_path: Path) -> None:
    mod = _load_module()

    with pytest.raises(mod.DashboardContractError, match="legacy evidence only"):
        mod.main(["--receipt", str(tmp_path / "receipt.json")])
