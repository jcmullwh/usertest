# Automated pipeline lifecycle metrics

This subsystem is observational. It records, validates, aggregates, and renders lifecycle
measurements. It does not create cases, suggest changes, choose remediations, or modify pipeline
behavior in response to a metric.

## Authoritative and derived artifacts

Each instrumented run directory retains:

- `lifecycle_events.jsonl`: crash-safe, idempotent source events;
- `lifecycle_manifest.json`: lifecycle status and source reconciliation;
- `model_usage_receipts/<sha256>/model_usage_receipt.json`: one content-addressed receipt per
  invocation;
- `case_metrics.json`: deterministic per-lifecycle metrics;
- `cohort_metrics.json`: disposition-stratified, nonduplicative cohort metrics;
- `cohort_comparison.json`: optional factual before/after fingerprint comparison.

`case_metrics.json` and `cohort_metrics.json` are generated products. For new cases, hand-authored
totals are not authoritative. Missing or ambiguous evidence is retained as `null`/unknown and
withholds only the affected certified metric; it does not block operational disposition.

## Automatic collection and refresh

Instrumentation is attached to the top-level runner, backlog model invocations, pipeline stage
transitions and reuse, ticket persistence, implementation/resume state, verification, Git/PR/CI
delivery events, supervisor corrections, and the continuous controller. A verified controller
context is required before a child launch is classified as automatic.

Every event write attempts an immediate, non-gating materialization. The continuous controller
also runs the staleness-aware refresh after each pass. Active, incomplete, and unreconciled
manifests are refreshed daily; source telemetry and metric-version changes refresh immediately.

For an independent scheduled task, run:

```powershell
python tools/refresh_pipeline_metrics.py `
  --root runs `
  --output-dir runs/_pipeline_metrics `
  --cohort-id current
```

The command only reads lifecycle telemetry and writes metric artifacts.

## Measuring boundary work

Use the public CLI for required work outside an already-instrumented boundary:

```powershell
usertest telemetry exec `
  --events runs/example/lifecycle_events.jsonl `
  --case-lifecycle-id case-42-attempt-2 `
  --case-id case-42 `
  --work-unit-id pr-create-42 `
  --actor supervising_agent `
  --action-family pull_request `
  --work-scope outside_platform `
  -- gh pr create --draft
```

The command is executed with propagated lifecycle context. The persisted command is redacted and
fingerprinted. A direct CLI launch with no verified controller parent stays `unknown_external`
unless `--actor` supplies a proven human or supervising-agent origin.

For unavoidable browser, UI, approval, or other external actions, record the completed action:

```powershell
usertest telemetry action record `
  --events runs/example/lifecycle_events.jsonl `
  --case-lifecycle-id case-42-attempt-2 `
  --case-id case-42 `
  --work-unit-id review-approval-42 `
  --actor human `
  --action-family review `
  --started-at 2026-07-21T12:00:00Z `
  --active-seconds 45 `
  --external-wait-seconds 600 `
  --wait-category approval `
  --result approved
```

Use `--dependency-work-unit-id` for required retained/shared work and
`--all-in-dependency-work-unit-id` for directly required supervising-agent or outside-platform
work. General platform engineering is left without a case beneficiary and is not amortized into
the cohort.

## Materialization and dashboard rendering

Materialize any set of retained streams directly:

```powershell
usertest telemetry materialize `
  --discover-root runs `
  --output-dir runs/_pipeline_metrics `
  --cohort-id current `
  --compare-to runs/_pipeline_metrics/prior/cohort_metrics.json
```

Render the generated schema-v4 dashboard through the existing dashboard entry point:

```powershell
python tools/update_backlog_depth_dashboard.py `
  --cohort-metrics runs/_pipeline_metrics/cohort_metrics.json `
  --comparison runs/_pipeline_metrics/cohort_comparison.json `
  --metrics-html-output runs/_pipeline_metrics/metrics_dashboard.html `
  --metrics-json-output runs/_pipeline_metrics/metrics_dashboard.json
```

Its schema-v3 ledger mode is legacy-only. Applying a historical receipt requires the explicit
`--allow-legacy-receipt` migration flag; schema-v3 receipts are not authoritative for new cases.

The report presents exact values for small cohorts; mature cohorts add median, p75, and p90
distributions. Every result remains separated by the six disposition categories and bound to its
system fingerprint. Comparisons publish sample sizes, completeness, absolute/percentage deltas,
and objective direction only; they do not infer causes or recommend changes.

## Historical GPT-5.6 import

The approximate GPT-5.6 window can be rebuilt from retained schema-v3 evidence:

```powershell
python tools/backfill_gpt56_lifecycle_metrics.py `
  --dashboard docs/design/historical-automated-backlog-depth-remediation-metrics.json `
  --output-dir docs/design/gpt-5.6-lifecycle-metrics-backfill `
  --since 2026-07-07T00:00:00-04:00
```

The date is an explicitly operator-selected approximate window, not an asserted release instant.
The importer rebuilds deterministically and preserves provenance as authoritative,
artifact-derived, operator-attested, inferred, or unknown. Legacy token usage, manual origin, and
disposition evidence that cannot be proven stay unknown, so the generated historical cohort is
expected to be uncertified.
