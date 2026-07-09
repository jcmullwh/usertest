# `usertest-implement` CLI

`usertest-implement` runs a coding agent to implement **one exported backlog ticket** in a target repo while
preserving the standard `runner_core` run artifacts plus ticket linkage artifacts (`ticket_ref.json`,
`timing.json`, and optionally git/push/PR metadata).

---

## Requirements

- Python 3.11+
- `git` (required for `--commit/--push`)
- Optional: GitHub CLI (`gh`) (required for `--pr`)
  - `gh` runs on the **host** (even when `--exec-backend docker` is used).
  - Ensure `gh` is on `PATH` and authenticated (`gh auth login`).
- Optional: `docker` (required for `--exec-backend docker`)

Commit identity:

- By default, `--commit` uses `usertest-implement <usertest-implement@local>` so agent commits are easy to spot.
- Override via `--git-user-name` / `--git-user-email` (for example, to use a bot identity or a GitHub noreply email).

Verification gate:

- When using `--commit/--push/--pr`, `usertest-implement` configures a required verification step before handing off
  (default: `scripts/smoke.ps1` on Windows local runs, `scripts/smoke.sh` otherwise, then
  `python tools/scaffold/scaffold.py run --all --skip-missing install`, then `lint`, then `test`).
- Same-repo Docker maintenance runs switch the smoke command to
  `bash ./scripts/smoke.sh --skip-install --use-pythonpath` so the maintenance image handles base
  environment setup while scaffold remains the install contract.
- If the verification gate fails, `usertest-implement` exits non-zero and refuses to `--commit/--push/--pr`
  (unless you pass `--skip-verify`, debugging only).
- Override the gate with `--verify-command "<cmd>"` (repeatable) and optional `--verify-timeout-seconds`.
- By default, `--verify-reuse auto` makes the final verification wait runner-owned. When the
  agent returns its final JSON report, the runner requests verification once through the broker,
  waits for completion outside the model transcript, and finalizes automatically if it passes.
  If the agent explicitly requested verification through the broker before returning, the runner
  can still select that broker result when the workspace hash matches.
- The final handoff prompt includes a compact `verification_timing_profile.json` generated from recent
  verification artifacts (excluding `_workspaces`). It distinguishes expected wait time from the high
  hang guard and tells agents not to issue repeated wait/poll actions for normal verifier completion.
- Use `--verify-reuse off` to force the older behavior and always run a separate post-agent verification pass.
- Disable the default gate with `--skip-verify` (debugging only; expect CI failures).
- `runner_core` may run follow-up attempts automatically when verification fails; see `agent_attempts.json`
  for the attempt sequence, `verification.json` for the selected verification result, and
  `verification_reuse.json` for the reuse/fallback decision log.

Maintenance install cache (Docker + warm cache):

- `usertest-implement` defaults to `--exec-cache warm`.
- When Docker + warm cache are active, `usertest-implement` also enables maintenance venv cache reuse by default
  (`--maintenance-venv-cache`), so scaffold install tasks can restore per-project `.venv` snapshots from `/cache`.
- Same-repo Docker runs now default to `--exec-docker-profile maintenance`, which resolves a dedicated
  maintenance image (`local -> pull -> build`) and bind-mounts matching cached project `.venv`
  directories directly into `/workspace/<project>/.venv` instead of copying them into each fresh workspace.
- On cache miss, the maintenance image can seed `.venv` directories from `/opt/usertest_maint_seed`
  before scaffold falls through to a real `pdm install`.
- Disable this behavior with `--no-maintenance-venv-cache` (forces full reinstall behavior).
- Cache root inside the container: `/cache/usertest_maint_venvs`.
- Default host cache root: `<repo_root>/runs/_cache/usertest_implement`.
- Inspect retained maintenance images with:
  - `usertest-implement maintenance-images list`
- Prune old maintenance-image tags with:
  - `usertest-implement maintenance-images cleanup --dry-run`
  - `usertest-implement maintenance-images cleanup`
- Automatic best-effort local image cleanup also runs after maintenance-image resolution using
  `configs/maintenance_docker.yaml`.
- Batch runs record the current Docker serialization audit in `batch_state.json`,
  `batch_summary.json`, and `docker_resource_plan.json`. Under current defaults it remains
  `parallel_safe: false` because image resolution is per-ticket, cleanup runs on prepare, and warm
  maintenance venv cache hits are mounted writable.

Docker execution profile:

- `--exec-docker-profile maintenance` is only valid for same-repo maintenance targets.
- `--exec-docker-profile standard` forces the existing generic `sandbox_cli` path even for same-repo
  runs.
- If `--exec-docker-profile` is omitted, `usertest-implement` selects:
  - `maintenance` for same-repo Docker runs
  - `standard` for external-target Docker runs

CI gate (before PR creation):

- When using `--pr`, `usertest-implement` waits for GitHub Actions workflow `CI` to pass on the pushed branch
  before running `gh pr create`.
- Override with `--skip-ci-wait` (debugging only; expect PR checks to fail) and `--ci-timeout-seconds`.
- If you still want a PR even when CI fails, use `--draft-pr-on-ci-failure` to create a draft PR.
- CI gate metadata is written to `ci_gate.json` in the run directory (including when skipped).

Implementation review gate (before merge):

- `usertest-implement run --commit --push --pr` stops at `4 - for_review` once the PR is created.
- It does not mark the ticket complete and it does not merge the PR.
- Use `usertest-implement review run` to review the PR against the ticket's selected approach.
- Use `usertest-implement review merge` only after review approval and green CI.
- `5 - complete` is reserved for merged tickets.

Quick checks:

```bash
git --version
gh --version
gh auth status
```

Install `gh` (examples):

- Windows: `winget install --id GitHub.cli`
- macOS: `brew install gh`
- Debian/Ubuntu: `sudo apt-get install gh`

If `gh` is installed but not found, ensure its install directory is on `PATH` (Windows default:
`C:\\Program Files\\GitHub CLI`).

## Install

From a monorepo checkout, prefer the repo bootstrap/smoke flow first so the wrapper resolves a
usable Python before installs:

- **Windows PowerShell:** `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1`
- **macOS / Linux:** `bash ./scripts/smoke.sh`

That path installs the shared requirements plus the local editable apps/packages used by this repo.

Advanced/manual fallback if you already have a known-good interpreter and intentionally want only
this app installed:

```bash
python -m pip install -r requirements-dev.txt
python -m pip install -e apps/usertest_implement
```

Confirm:

```bash
python -m usertest_implement.cli --help
# If PATH already exposes the console script: usertest-implement --help
```

---

## Usage

### Settings profiles

`usertest-implement` can load execution and handoff defaults from
[configs/usertest_implement_settings.yaml](I:/code/usertest/configs/usertest_implement_settings.yaml).

- Use `--settings <path>` to point at a different file.
- Use `--settings-profile <name>` to select a profile from that file.
- If `--settings` is omitted, `usertest-implement` auto-loads
  `configs/usertest_implement_settings.yaml` when it exists.
- Explicit CLI flags override settings-file values.

This is the intended place to make `commit/push/pr`, Docker profile, cache mode, verification reuse,
and `tickets run-next` defaults reviewable instead of hardcoded in helper scripts.

The settings file also makes the verification contract explicit:

- `verification_profile: default_handoff` means "use the standard runner-owned smoke/install/lint/test gate for the current backend/profile".
- `verification_commands: []` means "no ad hoc overrides"; it does not mean "run no verification".

The repo default profile also pins maintenance-oriented execution defaults for `usertest-implement`:

- `persona_id: thoughtful_maintainer`
- `mission_id: implement_maintenance_backlog_ticket_v1`

If the settings file is absent, the CLI falls back to those same maintenance defaults instead of inheriting the
global catalog quick-start persona.

### Implement a specific ticket

From a ticket markdown file (for example in `.agents/plans/2 - ready/`):

```bash
usertest-implement run --ticket-path ".agents/plans/2 - ready/<ticket>.md"
```

To use a named execution profile from the settings file:

```bash
usertest-implement run --settings-profile my_profile --ticket-path ".agents/plans/2 - ready/<ticket>.md"
```

`usertest-implement` only accepts stage-6 implementation tickets (`Export kind: implementation`, `Stage: ready_for_ticket`).

Or from a tickets export JSON:

```bash
usertest-implement run --tickets-export runs/usertest/<target>/_compiled/<scope>.tickets_export.json --fingerprint <fp>
```

### Standard flow (refresh + implement next)

This is the recommended “just keep shipping” loop:

```bash
usertest-implement tickets run-next --backlog-target <target_slug>
```

It runs the backlog refresh steps via `usertest-backlog` (backlog → intent-snapshot → review-ux → export-tickets),
exports only `ready_for_ticket` items, then selects the next local plan ticket that is both
`Export kind: implementation` and `Stage: ready_for_ticket`. Use `--no-refresh-backlog` for a fast path
that only selects from existing `.agents/plans/*` tickets that match the same stage-6 implementation gate.

### Review stage

PR-backed implementation tickets do not go straight from implementation to complete.

- `usertest-implement run --commit --push --pr` stops at `4 - for_review`
- `usertest-implement review run` checks the PR against the selected ticket approach and publishes the review comments directly onto the PR
- `usertest-implement review merge` merges only when the review says the PR is merge-ready
- `5 - complete` is reserved for merged tickets

Example review flow:

```bash
usertest-implement review run --ticket-path ".agents/plans/4 - for_review/<ticket>.md"
usertest-implement review status --ticket-path ".agents/plans/4 - for_review/<ticket>.md"
usertest-implement review merge --ticket-path ".agents/plans/4 - for_review/<ticket>.md"
```

The review stage is intentionally narrow. It checks:

- alignment to the ticket's selected approach
- unnecessary added scope
- implementation defects and regressions
- CI truth and PR mergeability

It does not re-decide the backlog ticket's solution.
