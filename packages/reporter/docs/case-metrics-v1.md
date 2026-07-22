# Lifecycle case metrics v1

The `lifecycle_case_metrics_v1` report is deterministic over lifecycle event dictionaries. Input
may be an iterable of mappings or one or more JSONL files. Custom producer values may be at the
top level, in `context`, or in `attributes`; legacy `data` and `payload` containers are also read.

## Identity and canonical events

The aggregation identity is `case_lifecycle_id`. The stable problem identity is `case_id` and is
retained separately. Shared work uses `shared_work_id`; `beneficiary_case_lifecycle_ids` attach it
to each case without duplicating cohort cost. `dependency_ids` enter inclusive closure, while
`all_in_dependency_ids` add outside/support dependencies only to all-in accounting.

Canonical event types are:

- `lifecycle.opened` / `lifecycle.closed`
- `stage.started` / `stage.completed`
- `work.started` / `work.created` / `work.completed` / `work.reused`
- `model.invocation.started` / `model.invocation.completed`
- `error.occurred` / `error.resolved`
- `intervention.started` / `intervention.completed`
- `action.started` / `action.completed`
- `disposition.reached` / `disposition.verified`
- `delivery.started` / `delivery.completed`
- `outcome.verified`

Underscore and producer-specific event aliases are normalized, but final disposition values are
restricted to six exact categories: `already_addressed`, `non_actionable`, `duplicate`,
`superseded`, `pr`, and `failed_incomplete`. `pull_request` is a producer alias for `pr`. An open
lifecycle is not coerced to `failed_incomplete`; a terminal lifecycle with no valid disposition is.

## Cost and time

Token dimensions are `total_tokens`, `input_tokens`, `cached_input_tokens`,
`uncached_input_tokens`, `output_tokens`, and `reasoning_output_tokens`. Cached input remains part
of input and total tokens. Conflicting totals or cache dimensions fail reconciliation.

Canonical time fields are `started_at`, `ended_at`, `active_seconds`, `machine_wait_seconds`, and
`external_wait_seconds`. Resource time is summed; wall time is the union of work intervals. Gaps
inside the observed interval span and gaps between lineage opening and final disposition are named
`unclassified`, never inferred to be idle. The case timing block reports:

- atom to disposition;
- admission to disposition;
- lineage to disposition;
- PR creation to outcome verification;
- summed active, manual-active, machine-wait, and external-wait time;
- interval-union wall time and unclassified time.

`disposition.verified` fixes the PR disposition boundary at verified PR creation.
`outcome.verified` is later post-disposition accounting and does not extend atom/admission/lineage
to disposition.

## Errors and manual work

Error clusters use exactly eight resolution modes:

- `self_healed_same_author`
- `self_healed_controller`
- `resolved_supervisor`
- `resolved_human`
- `resolved_external`
- `tolerated_nonblocking`
- `unresolved_terminal`
- `open`

The first two form the self-healed group. The next three are externally resolved and therefore
could not self-heal. `open` remains distinct from a terminal unresolved cluster. Interventions and
manual actions are deduplicated by their IDs and retain actor, milestone, avoidability,
required-for-progress, timestamps, and active seconds. `supervising_agent` normalizes to the
supervisor actor.

## Automation score

`automation_score_v1` uses fixed milestone paths by exact disposition. Scores are percentages from
0 through 100. Gross automation penalizes every manual milestone. Avoidable automation removes an
explicitly unavoidable manual milestone from its denominator. A failed/invalid lifecycle scores
zero; an active lifecycle is pending and has no score.

Certification is withheld when origin telemetry is unknown, an origin or required milestone is
missing, disposition is unverified, manual avoidability is unclassified, milestone order is
invalid, or accounting fails reconciliation. The numeric score is retained alongside explicit
withholding reasons and must not be presented as certified.

## Cohorts and comparisons

`aggregate_cohort_metrics` unions work-unit IDs for nonduplicative direct, inclusive, and all-in
totals. Every exact disposition includes median, nearest-rank p75, nearest-rank p90, and totals for
tokens, timing boundaries, resource/interval time, errors, interventions, and manual actions.

`compare_cohorts` emits before/after fingerprints, absolute and percentage deltas, configured
objective direction, observed direction, completeness, and reconciliation. These are factual
deltas only; the report expressly makes no causal claim. Percentage delta is absent when the
baseline is zero rather than manufacturing an infinite percentage.
