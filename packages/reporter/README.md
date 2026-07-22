# `reporter`

This package provides:

- Normalized event helpers (re-exported from `normalized_events` as `reporter.normalized_events` for compatibility)
- Metrics computation over normalized events (`reporter.metrics.compute_metrics`)
- JSON schema validation for `report.json` (`reporter.schema.validate_report`)
- Markdown rendering for humans (`reporter.render.render_report_markdown`)

It is used by `runner_core` and the `usertest` / `usertest-backlog` CLIs, but can also be used as a
standalone library for post-processing run artifacts.

---

## Install

Distribution name: `reporter`
Import package: `reporter`

### Standalone package checkout (recommended first path)

Run from this package directory:

```bash
pdm install
pdm run smoke
pdm run test
pdm run lint
```

Dependencies for standalone use:
- `reporter` imports `normalized_events` and `run_artifacts` at runtime.
- If your package index does not provide those internal packages, install local checkouts first.
- From a sibling checkout layout, run:

```bash
python -m pip install -e ../normalized_events -e ../run_artifacts
```

If you need only a runtime install (without dev tooling commands), use:

```bash
python -m pip install -e .
```

From a private GitLab PyPI registry (snapshot publishing):

```bash
pip install \
  --index-url "https://<gitlab-host>/api/v4/projects/<project_id>/packages/pypi/simple" \
  --extra-index-url "https://pypi.org/simple" \
  "reporter==<version>"
```

Snapshot publishing status: `incubator` (see `docs/monorepo-packages.md`).

---

## Canonical smoke

Run from this package directory:

```bash
pdm run smoke
pdm run smoke_extended
```

`pdm run smoke` is the deterministic first-success check. `pdm run smoke_extended` keeps a second
tier for broader validation passes.

---

## Quickstart

Validate a report JSON file and render a markdown report:

```python
import json
from pathlib import Path

from reporter import load_schema, render_report_markdown, validate_report

run_dir = Path("runs/usertest/.../seed0")
report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
schema = load_schema(Path("configs/report_schemas/default_report.schema.json"))
validate_report(report, schema)

markdown = render_report_markdown(report)
print(markdown)
```

## Normalized events

The canonical home of the normalized-events contract (envelope + JSONL helpers) is the `normalized_events`
package. This package continues to expose `reporter.normalized_events` as a thin re-export for backwards
compatibility.

See `docs/design/event-model.md` for the current event model.

Golden run fixtures for offline validation live in `examples/golden_runs/`. The CLI fixture test
that recomputes metrics/report artifacts is:

`python -m pytest -q apps/usertest/tests/test_golden_fixture.py`

---

## Public API

Common entry points:

- `compute_metrics(events_iterable)`
- `validate_report(report, schema)`
- `render_report_markdown(report)`
- `analyze_report_history(history_records)`
- `write_issue_analysis(path, analysis)`
- `load_lifecycle_events(jsonl_or_events)`
- `aggregate_case_metrics(jsonl_or_events)`
- `aggregate_cohort_metrics(jsonl_or_case_report)`
- `compare_cohorts(before, after)`
- `discover_lifecycle_event_logs(roots)`
- `materialize_lifecycle_metrics(event_sources=..., output_dir=...)`

## Lifecycle case metrics

`reporter.case_metrics` is a pure event-dictionary/JSONL aggregator. It intentionally does not
import `run_artifacts`; a producer or materializer should resolve referenced token receipts and put
their exact token dimensions on `model.invocation.completed` before aggregation.

Case aggregation is keyed by `case_lifecycle_id` while retaining the stable `case_id`. Costs are
identified by `work_unit_id`. `shared_work_id`, beneficiary lifecycle IDs, and dependency IDs
control attribution without copying shared cost:

- direct: case-owned work only;
- inclusive: direct work plus shared/reused work and causal dependency closure;
- all-in: inclusive work plus support, manual, supervisor, and all-in dependency closure.

Cohorts union work-unit IDs before summing. Consequently, a shared Stage 1/2 unit appears in every
beneficiary's inclusive lineage but only once in a cohort total.

The exact disposition categories are `already_addressed`, `non_actionable`, `duplicate`,
`superseded`, `pr`, and `failed_incomplete`. Active lifecycles have no disposition and are reported
separately. See [`docs/case-metrics-v1.md`](docs/case-metrics-v1.md) for the complete event,
accounting, timing, resolution, certification, and comparison contracts.

---

## Development

### Standalone package checkout (recommended first path)

Run from this package directory:

```bash
pdm install
pdm run smoke
pdm run smoke_extended
pdm run test
pdm run lint
```

### Monorepo contributor workflow

Run from the monorepo root:

- Run tests: `python tools/scaffold/scaffold.py run test --project reporter`
- Run lint: `python tools/scaffold/scaffold.py run lint --project reporter`
