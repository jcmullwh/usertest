# `run_artifacts`

`run_artifacts` contains reusable primitives for working with **run directories**:

- capturing text artifacts with explicit truncation + provenance
- iterating and writing run-history JSONL files
- shaping and sanitizing structured failure events

It is shared by:

- `reporter`
- backlog tooling (`backlog_core`, `usertest-backlog`)
- CLI apps that compile/analyze run history

---

## Install

Distribution name: `run_artifacts`

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

From a private GitLab PyPI registry (if you publish it):

```bash
pip install \
  --index-url "https://<gitlab-host>/api/v4/projects/<project_id>/packages/pypi/simple" \
  --extra-index-url "https://pypi.org/simple" \
  "run_artifacts==<version>"
```

> Publishing note
>
> This package is currently treated as **internal** unless opted into snapshot publishing via
> `[tool.monorepo].status` in `pyproject.toml`. See `docs/monorepo-packages.md`.

---

## Quickstart

Capture a text artifact with explicit truncation metadata:

```python
from pathlib import Path

from run_artifacts import TextCapturePolicy, capture_text_artifact

policy = TextCapturePolicy(max_excerpt_bytes=10_000, head_bytes=5_000, tail_bytes=5_000)
result = capture_text_artifact(Path("runs/.../agent_stderr.txt"), capture_policy=policy)

print(result.artifact.path, result.artifact.exists, result.excerpt.truncated if result.excerpt else None)
```

Iterate a compiled report history file (JSONL):

```python
from run_artifacts import iter_report_history

for record in iter_report_history("runs/usertest/report_history.jsonl"):
    print(record.get("run_rel"), record.get("status"))
```

---

## Public API

### Artifact capture

- `TextCapturePolicy`
- `capture_text_artifact(path, capture_policy=...)`
- `CaptureResult`, `ArtifactRef`, `TextExcerpt`

### Run history

- `iter_report_history(path)`
- `write_report_history_jsonl(path, records)`

### Failure shaping

- `classify_failure_kind(error)`
- `sanitize_error(error)`
- `render_failure_text(error)`
- `extract_error_artifacts(error)`

---

## Design notes

This package is intentionally strict about “silent loss”:

- existing artifacts should not be silently dropped
- truncation should be explicit and accounted for

Related design doc:

- `docs/design/backlog_capture_principles.md`

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
python tools/scaffold/scaffold.py run install --project run_artifacts
python tools/scaffold/scaffold.py run test --project run_artifacts
python tools/scaffold/scaffold.py run lint --project run_artifacts
```
