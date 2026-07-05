# Usertest validity hardening

## Problem

The previous defaults optimized for **first sign of life** more than **credible evidence that the repo works in a representative way**.

That was useful as a probe, but too weak as the default posture for adoption-oriented usertesting. In practice it encouraged results such as:

- `--help` or trivial output being treated as “success”
- checked-in fixtures standing in for fresh runs
- side paths counting even when the primary workflow remained untested
- policy/docs defaults pointing users toward minimal evidence instead of representative validation

## Changes in this hardening pass

### 1) New default persona + mission

The built-in defaults now point to:

- persona: `representative_workflow_evaluator`
- mission: `verify_install_to_result`

That pair is meant to answer: “Can a real user follow the documented path and get a representative result?”

### 2) Preflight is separated conceptually from usertesting

`first_output_smoke` remains available, but it is explicitly positioned as a **preflight probe** rather than an adoption-quality validation.

### 3) Stronger evidence contract

Representative task-run missions now use:

- prompt template: `inline_report_evidence_v1.prompt.md`
- report schema: `task_run_evidence_v1.schema.json`

The evidence schema requires:

- a representative-workflow explanation
- concrete step evidence for each attempt
- explicit verification checks
- outputs grounded in the current run

### 4) Repo-local defaults favor the primary user journey

When this repository is itself the target, `.usertest/catalog.yaml` now defaults to:

- persona: `repo_adoption_gatekeeper`
- mission: `self_end_to_end_run_single_target`

## Rollout note

If preserving historical trendlines for `first_output_smoke` matters, consider one of these before rollout:

1. duplicate the old prompt under a new legacy/preflight mission ID, then apply the in-place clarifications here, or
2. keep `first_output_smoke` unchanged and introduce a new explicit preflight mission ID instead.

The included patch takes the simpler route: it clarifies and demotes `first_output_smoke` in place.

## Validation checklist

After applying this change set, validate all of the following:

- default `run` examples point to representative validation, not just sign-of-life
- `first_output_smoke` is clearly framed as preflight-only
- representative reports include:
  - why the chosen workflow is representative
  - exact commands and evidence
  - at least one explicit verification check
- self-testing this repo defaults to `self_end_to_end_run_single_target`
