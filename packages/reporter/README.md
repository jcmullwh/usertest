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
