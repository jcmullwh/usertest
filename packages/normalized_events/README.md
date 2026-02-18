# normalized_events

Small, stdlib-only helpers for the repo's "normalized events" JSONL contract.

This package is intentionally minimal so it can act as a shared contract between:

- `agent_adapters` (event normalization)
- `runner_core` (run orchestration)
- `reporter` (metrics + rendering)

---

## Install

Distribution name: `normalized_events`
Import package: `normalized_events`

### Standalone package checkout (recommended first path)

Run from this package directory:

```bash
pdm install
pdm run smoke
pdm run test
pdm run lint
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
  "normalized_events==<version>"
```

Snapshot publishing status: `incubator` (see `docs/monorepo-packages.md`).

## Canonical smoke

Run from this package directory:

```bash
pdm run smoke
pdm run smoke_extended
```

`pdm run smoke` is the deterministic first-success check. `pdm run smoke_extended` keeps a second
tier for broader validation passes.

## Event envelope

Each normalized event is a JSON object with:

- `ts`: timestamp string (UTC-ish)
- `type`: event type string
- `data`: event payload object

## JSONL helpers

- `write_events_jsonl(path, events)`: writes one JSON object per line
- `iter_events_jsonl(path)`: iterates parsed JSON objects from a `*.jsonl` file

This package is intended to be a neutral contract shared by `agent_adapters`, `reporter`, and `runner_core`.

## Contract checks

Use the fixture-backed adapter regression test to catch normalized-event drift:

`python -m pytest -q packages/agent_adapters/tests/test_golden_normalization_fixtures.py`

See `docs/design/event-model.md` for event semantics and `examples/golden_runs/` for fixture data.

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

```bash
python tools/scaffold/scaffold.py run install --project normalized_events
python tools/scaffold/scaffold.py run test --project normalized_events
python tools/scaffold/scaffold.py run lint --project normalized_events
```
