# Release notes: v0.2.0

This changelog summarizes the `v0.2.0` work currently on `dev` since the last shared commit with
`main` (`8fd5377`, "Merge pull request #5 from jcmullwh/Update-README"). It covers the release
candidate represented by `dev` at `f1b77a6` on 2026-03-15 and is intended to accompany the merge
back to `main`.

Scope at a glance:

- 269 commits ahead of `main`
- 91 first-parent PR merges into `dev`
- Major workstreams across onboarding, runner reliability, backlog automation, implementation
  workflow automation, and maintenance/ops tooling

## Highlights

### Easier first-run onboarding and stronger cross-platform UX

- Added first-run `doctor` and one-command `smoke` wrappers for Windows PowerShell and
  macOS/Linux, with clearer preflight guidance and optional strict tool checks.
- Promoted `snapshot_repo` into a first-class, shareable workflow with dry-run/listing support and
  clearer inclusion/exclusion reporting.
- Hardened Windows behavior across smoke/setup flows by avoiding fragile bash assumptions, fixing
  command/path quoting, and steering users toward PowerShell-safe entrypoints.
- Improved from-source and offline success paths so report rerendering, smoke runs, and import
  checks fail fast with actionable remediation instead of opaque environment errors.

### Runner execution and verification are much more deterministic

- Introduced a canonical Python toolchain capability contract used across preflight, runtime,
  smoke, wrapper launches, and verification.
- Centralized final verification behind a verification broker with a more explicit lifecycle,
  bounded completion behavior, and better broker reuse.
- Standardized agent-visible path handling and Windows path normalization across runner and adapter
  flows.
- Improved failure reporting by preserving stdout/stderr, classifying quota and rejection cases
  more accurately, distinguishing missing vs unreadable terminal artifacts, and keeping richer run
  metadata and metrics.

### Backlog generation matured into an inspectable six-stage pipeline

- `usertest-backlog reports backlog` now produces a six-stage pipeline with explicit stage
  contracts, inspectable artifacts, relation review, prioritization, solution optioning, and
  solution selection.
- Added triage-atoms workflows, ticket linking, ticket assembly, and stronger backlog policy logic
  to reduce duplicate or low-signal work.
- Introduced ready-ticket queue management, including promotion into the ready queue and persistence
  of ready-ticket conflict metadata.
- Expanded exported ticket context and improved deferred export/action-ledger behavior so downstream
  implementation runs have more complete planning context.

### Implementation workflow now includes review and maintenance automation

- Added a maintenance batch runner plus continuous-loop/watchdog tooling for same-repo maintenance
  work.
- Added settings-driven defaults and maintenance-oriented execution profiles for
  `usertest-implement`, including dedicated Docker maintenance profiles and maintenance image
  handling.
- Added implementation review automation: PRs can stop in `for_review`, reviews can be run against
  the selected ticket approach, and review output can now be posted directly as PR comments.
- Improved rerun handoff behavior, verification gates, ticket selection, and backlog refresh flows
  used by implementation automation.

### Maintenance, CI, docs, and regression coverage all expanded

- Added maintenance image publishing/cleanup support, install cache fingerprinting, and more robust
  scaffold prerequisite handling.
- Expanded security and operations docs around run artifact sensitivity and safe sharing.
- Significantly increased offline, fixture-based, and contract-level regression coverage across
  runner, adapters, scaffold, backlog, and implementation packages.

## Notable behavior changes to call out in the release

- `usertest-implement run --commit --push --pr` now stops at review (`4 - for_review`) instead of
  treating PR creation as the final handoff.
- Same-repo Docker maintenance runs now favor the maintenance profile/caching path by default.
- `doctor` is more tolerant by default when `pip` is missing; strict enforcement is now opt-in.
- Review UX and backlog export flows now depend on the newer staged backlog artifacts and richer
  ticket context.

## Suggested release framing

This release is primarily a reliability and workflow-automation milestone. The biggest user-visible
changes are the smoother first-run experience, much stronger Windows/Python toolchain handling, the
new six-stage backlog pipeline, and end-to-end implementation review automation for PR-driven
maintenance work.
