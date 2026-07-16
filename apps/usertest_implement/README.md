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
- A run whose `ticket_resume_state.json` is `verification_failed_resume_ready` can be re-entered
  without replaying the original full ticket prompt:
  `usertest-implement resume --run-dir <run_dir>`. The resume command builds a focused prompt from
  `verification.json`, `verification_reuse.json`, `agent_attempts.json`, `workspace_ref.json`,
  `ticket_ref.json`, and any prior report output. It uses the same kept workspace when available;
  otherwise it checks out the recorded branch from the inferred or explicitly supplied repo
  (`--repo`/`--ref`).

Maintenance install cache (Docker + warm cache):

- `usertest-implement` defaults to `--exec-cache warm`.
- When Docker + warm cache are active, `usertest-implement` also enables maintenance venv cache reuse by default
  (`--maintenance-venv-cache`), so scaffold install tasks can restore per-project `.venv` snapshots from `/cache`.
- Same-repo Docker runs now default to `--exec-docker-profile maintenance`, which resolves a dedicated
  maintenance image (`local -> pull -> build`), copies matching cached project `.venv` directories
  to per-run writable locations, and mounts those copies at `/workspace/<project>/.venv`. This keeps
  cache hits fast without sharing one writable host `.venv` cache path across concurrent workers.
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
- Batch preflight resolves the maintenance image once when Docker maintenance profile is active and
  persists `preflight/maintenance_image.json` with the env hash, immutable image ref, source
  (`local`, `pulled`, or `built`), pull/build artifacts, and timing. Ticket runs launched by that
  batch pass the metadata via `--exec-maintenance-image-metadata` so they reuse the image ref without
  repeating pull/build/tag work.
- Batch runs record the current Docker serialization audit in `batch_state.json`,
  `batch_summary.json`, and `docker_resource_plan.json`. When the resource plan is
  `parallel_safe: true`, the scheduler omits only the Docker-wide `batch_resource:docker` conflict
  key; per-domain and per-subsystem ticket conflict keys still serialize overlapping work. Unsafe
  Docker plans keep the Docker-wide guard. Each batch launch wave records the safe/unsafe decision
  and whether the Docker guard was applied. Warm maintenance venv cache hits use per-worker writable
  copies, and the selected cache strategy is recorded in maintenance profile artifacts.

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
- Review approval is bound to the exact PR head commit. A pushed commit invalidates the approval and
  requires a new `review run` before merge.
- `review merge` refreshes PR metadata after merge, records the actual merge commit and target
  branch, embeds a validated outcome record, and then moves the ticket to `5 - complete`.
- Merge records at least `implemented`. It records `tests_verified` only when the implementation
  run retains a passing `verification.json` with configured commands and successful results. Green
  PR checks remain CI evidence, not automatic test evidence. Neither state by itself claims that the
  originating problem is resolved; original-scenario and live-runtime proof remain separate.
- The generic `tickets move` command cannot move a ticket into `5 - complete`, because it has no
  reviewed merge provenance or verification evidence.

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
- `exec_keep_container: false` so normal cleanup force-removes execution containers and Docker
  auto-removes them when they stop; use `--exec-keep-container` only for deliberate debugging.

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

Generated stage-6 tickets start from their exact researched repository revision. Before
review, the runner requires all planned production target paths to be touched and binds
the passing verification run to the exact PR head. Extra files and wider hunks are shown
to the semantic reviewer instead of being rejected mechanically. A high or critical
actionable review finding vetoes merge even if the model also emits an approval decision.

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
The refresh is one exclusive, fail-closed transaction: preliminary shadow, fresh intent snapshot,
UX review, two fresh stable qualifying shadows, and immediate export. Every step uses the same
repository, research ref, breadth profile, agent/model, and action ledgers. The per-scope refresh
lock prevents concurrent writers. Unrelated IDEA or release PRs do not impose a repository-wide
refresh veto; active generated work is suppressed by canonical case instead. The export stays
locked unless the last two fresh cycles pass all depth invariants with a stable projection.

It exports only `ready_for_ticket` items, then selects the next local plan ticket that is both
`Export kind: implementation` and `Stage: ready_for_ticket`. Use `--no-refresh-backlog` for a fast path
that only selects from existing `.agents/plans/*` tickets that match the same stage-6 implementation gate.

### Automated backlog batch

`usertest-implement batch run` separates the clean code checkout (`--repo-root`) from the configured
historical-data, implementation-artifact, ledger, and queue owner (`defaults.owner_root`). It fetches `defaults.wave_base_ref`, resolves
one exact commit, pins every research source to that commit, and rejects any generated plan whose
stage-6 target revision differs before claiming the ticket. The same exact revision is passed to
implementation. The default batch intentionally has no per-ticket timeout; a timeout applies only
when `ticket_timeout_seconds` is explicitly configured.

The default phases drain blocker/high, medium, and low automated work. IDEA-originated tickets are
not batch candidates and are not reviewed, merged, or finalized by the automated loop. A generated
same-bucket historical duplicate is repaired before strict ticket indexing only when its generated
provenance is explicit; manual, IDEA, and unreadable copies are protected.

At the end of a pass, `terminal_proof.json` distinguishes successful queue movement from actual
completion. It requires a fresh zero-export shadow result for every source, the exact wave revision,
no nonterminal canonical cases, and no active generated plan files. A pass that created work for PR
review records `awaiting_terminal_proof` and lets the outer review/outcome loop continue. Only a
hash-verified passing terminal proof stops that loop as `completed`.

### Adopt an existing implementation PR

When implementation and PR creation happened outside the original runner handoff, reconcile them
without starting another agent turn or pretending that the reconciliation command committed,
pushed, or created the PR:

```bash
usertest-implement handoff adopt-pr \
  --owner-root /path/to/ticket-owner \
  --ticket-path "/path/to/ticket-owner/.agents/plans/2 - ready/<ticket>.md" \
  --source-run-dir /path/to/implemented-local-run \
  --runs-dir /path/to/runs/usertest_implement \
  --pr-url https://github.com/owner/repo/pull/123 \
  --base-branch dev \
  --remote-name origin
```

The source must be `implemented_local`, ticket-bound, and tied to the current clean branch head.
The command checks the open PR before and after verification and rejects a different repository,
branch, head, base, or moving PR binding. If the source verification ran a broader command set, it
captures only the exact stage-6 plan commands with the source run's positive timeout and Python
toolchain. It writes a separate adoption run and updates the attempt ledger. It does not invoke a
model, mutate the ticket, move queue state, create an outcome record, push, merge, or write GitHub.
An adopted ready ticket remains in its existing bucket; this command does not broaden `review run`
eligibility.

### Review stage

PR-backed implementation tickets do not go straight from implementation to complete.

- `usertest-implement run --commit --push --pr` stops at `4 - for_review`
- `usertest-implement review run` checks the PR against the selected ticket approach and publishes the review comments directly onto the PR
- `usertest-implement review merge` merges only when the review says the PR is merge-ready
- `5 - complete` is reserved for merged tickets with a validated embedded outcome record

Example review flow:

```bash
usertest-implement review run --ticket-path ".agents/plans/4 - for_review/<ticket>.md"
usertest-implement review status --ticket-path ".agents/plans/4 - for_review/<ticket>.md"
usertest-implement review merge --ticket-path ".agents/plans/4 - for_review/<ticket>.md"
```

`review merge` records the PR's post-merge commit, base branch, reviewed head SHA, and explicit
passing checks. It uses `gh pr merge --match-head-commit` so a commit pushed after review cannot be
merged accidentally. The initial outcome is `tests_verified` only when the selected case-aware plan
contains a hashed stage-6 verification-command contract and the retained runner artifact proves
that every command in that exact contract passed. The export body hash, local plan hash, case/plan
identity, command-contract hash, runner ticket reference, and review reference must all agree.
Legacy plans can still be implemented, but generic verification cannot promote them to
`tests_verified` unless they carry an explicit bound contract. Generic green CI, skipped checks,
neutral checks, and caller-supplied evidence labels do not count as test evidence.

For generated plans, merge finalization does not stop at `tests_verified`. It creates a temporary
clean detached worktree at the exact merged commit and automatically runs the plan's hashed
original-scenario role with no default timeout. For a post-research consolidated case that role is
a signed multi-scenario oracle: every retained child replay and its stage-5-selected positive
contract must pass, so proving only the canonical symptom cannot close absorbed cases. It runs the live role only when the researched
case crosses a real runtime boundary, and it runs the mitigation-effect role only when
`before_after_reproduction.expected_outcome_state` is `mitigated`. The durable result becomes
`resolved` only after the original scenario (and required live boundary) demonstrates the planned
correct behavior; a planned mitigation becomes `mitigated` and explicitly does not claim root
resolution. Failed, timed-out, or false predicates retain their runner artifact and leave the case
`unverified`. The already-merged finalizer and centralized backlog refresh retry that proof. While
proof is operationally pending, export suppresses only that canonical case and continues unrelated
backlog work. If the original-scenario predicates actually fail, that durable failure may return the
case to research for a revised causal solution instead of silently retrying the same plan.

Advance a merged ticket only when new proof exists. This command transitions the embedded ticket
outcome and implementation ledger together; it refuses missing, conflicting, or illegal state
transitions:

```bash
usertest-implement outcome advance \
  --ticket-path ".agents/plans/5 - complete/<ticket>.md" \
  --state tests_verified \
  --evidence-json outcome-evidence.json
```

`outcome-evidence.json` is an object with newly established evidence plus explicit remaining risks
and recurrence status. Evidence entries are appended without discarding prior proof:

```json
{
  "test_evidence": [
    {
      "kind": "runner_verification",
      "reference": "runs/implementation/verification.json",
      "result": "passed",
      "runner_receipt": {
        "run_dir": "runs/implementation",
        "verification_sha256": "<64-character sha256>",
        "evidence_kind": "test"
      }
    }
  ],
  "remaining_risks": ["Original scenario replay remains pending"]
}
```

Do not hand-author the receipt. The command re-opens the referenced implementation run and verifies
its ticket reference, plan provenance, exact configured/executed command coverage, target-scope
contract, terminal status, artifact hashes, and command safety.

Stage 6 also carries dedicated original-scenario, live, mitigation-effect, and recurrence roles.
Run one only from a checkout whose `HEAD` is the recorded merged commit; the default has no arbitrary
timeout. The runner executes the exact role commands, evaluates the hashed machine predicates, and
writes an advance-ready evidence JSON under the configured runs root:

```bash
usertest-implement outcome run-role \
  --ticket-path ".agents/plans/5 - complete/<ticket>.md" \
  --role original_scenario \
  --workspace /path/to/checkout-at-merged-commit

usertest-implement outcome advance \
  --ticket-path ".agents/plans/5 - complete/<ticket>.md" \
  --state original_scenario_verified \
  --evidence-json <printed-outcome-evidence.json>

# Recurrence is supplied by the centralized refresh, not a planner-invented test.
usertest-implement outcome run-role \
  --ticket-path ".agents/plans/5 - complete/<ticket>.md" \
  --role recurrence \
  --workspace /path/to/checkout-at-merged-commit \
  --recurrence-refresh-receipt runs/<target>/_compiled/<target>.refresh_receipt.json
```

The same flow applies to `live`, `mitigation_effect`, and `recurrence`. Generic test receipts cannot
be relabeled as these roles. A timeout, cancelled command, predicate mismatch, changed snapshot,
wrong commit, wrong tested implementation head, wrong case/plan, or wrong target-scope hash remains
blocked. Recurrence additionally requires two distinct shadow cycles generated after the prior
outcome, the exact retained case and atom snapshots, at least one actual source-observation run
after that outcome, and an unchanged plan-time case evidence baseline with no recurrence reopen.
Two immediate refreshes over an unchanged corpus prove pipeline stability, not non-recurrence.
When no later source window exists, a directly proven fix may record recurrence as
`not_observed/no_new_source_window` with that limitation in `remaining_risks`; it must not attach a
passing recurrence receipt. Runtime-bound resolution still requires live proof. `mitigated`
requires both bound test evidence and a dedicated mitigation-effect receipt.
Merge retries preserve an outcome that has already advanced beyond `tests_verified`.

The review stage is intentionally narrow. It checks:

- alignment to the ticket's selected approach
- unnecessary added scope
- implementation defects and regressions
- CI truth and PR mergeability

It does not re-decide the backlog ticket's solution.
