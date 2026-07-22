# `run_artifacts`

`run_artifacts` contains reusable primitives for working with **run directories**:

- capturing text artifacts with explicit truncation + provenance
- iterating and writing run-history JSONL files
- shaping and sanitizing structured failure events

It is shared by:

- `reporter`
- backlog tooling (`backlog_core`, `usertest-backlog`)
- CLI apps that compile/analyze run history

The package also owns the dependency-free lifecycle telemetry contract used to account for case,
shared-work, model-usage, error, intervention, and manual-action lineage across those tools.

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

Capture a text artifact with explicit truncation metadata:

```python
from pathlib import Path

from run_artifacts import TextCapturePolicy, capture_text_artifact

policy = TextCapturePolicy(max_excerpt_bytes=10_000, head_bytes=5_000, tail_bytes=5_000)
result = capture_text_artifact(Path("runs/.../agent_stderr.txt"), policy=policy)

print(result.artifact.path, result.artifact.exists, result.excerpt.truncated if result.excerpt else None)
```

Iterate a compiled report history file (JSONL):

```python
from run_artifacts import iter_report_history

for record in iter_report_history("runs/usertest/report_history.jsonl"):
    print(record.get("run_rel"), record.get("status"))
```

Compile run directories into JSONL history (large text embeddings are truncated, not dropped):

```python
from pathlib import Path

from run_artifacts import write_report_history_jsonl

counts = write_report_history_jsonl(
    Path("runs/usertest"),
    out_path=Path("runs/usertest/_compiled/all.report_history.jsonl"),
    embed="definitions",
    max_embed_bytes=200_000,
)
print(counts)
```

---

## Public API

### Artifact capture

- `TextCapturePolicy`
- `capture_text_artifact(path, policy=...)`
- `CaptureResult`, `ArtifactRef`, `TextExcerpt`

### Run history

- `iter_report_history(source, target_slug=..., repo_input=..., embed=..., max_embed_bytes=...)`
- `write_report_history_jsonl(runs_dir, out_path=..., target_slug=..., repo_input=..., embed=..., max_embed_bytes=...)`

### Lifecycle telemetry

- `LifecycleContext`, `LifecycleEvent`, and `LifecycleManifest`
- `ModelUsageReceipt`, `ErrorCluster`, `Intervention`, and `ManualAction`
- `make_lifecycle_event(...)` and `append_lifecycle_event(...)`
- `serialize_lifecycle_context(...)`, `lifecycle_context_env(...)`, and
  `load_context_from_env(...)`
- `write_lifecycle_manifest(...)` and `write_content_addressed_model_usage_receipt(...)`
- `redact_command(...)`, `fingerprint_command(...)`, and `command_family(...)`

The event log writer uses an exclusive sidecar lock, a single append payload, and `fsync`. A partial
unterminated tail left by a crashed writer is repaired at the next append; corruption in a completed
line remains a hard error. Event ids cannot change content, and repeated idempotency keys are not
appended again.

Pass lifecycle context to child processes without an application dependency:

```python
from run_artifacts import LifecycleContext, lifecycle_context_env, load_context_from_env

context = LifecycleContext(
    case_lifecycle_id="case-42-attempt-2",
    case_id="case-42",
    work_unit_id="qualification-stage-1",
)
child_environment = lifecycle_context_env(context)
same_context = load_context_from_env(child_environment, required=True)
```

`ModelUsageReceipt.usage_semantics` must be explicit. A `session_cumulative` receipt publishes only
the validated difference between matching baseline and observed token dimensions;
`unattributable` receipts preserve observations but cannot publish attributable token totals.

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
