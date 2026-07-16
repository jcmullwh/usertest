# CLI reference

This repo ships three end-user CLIs:

- `usertest` (app: `apps/usertest`) – run usertests and render run artifacts
- `usertest-backlog` (app: `apps/usertest_backlog`) – compile/analyze/export run history and triage PRs
- `usertest-implement` (app: `apps/usertest_implement`) – implement one exported backlog ticket in a target repo

If you’re unsure where to start, read `docs/tutorials/getting-started.md`.

---

## `usertest`

Entry points:

- `usertest …` (installed script)
- `python -m usertest.cli …` (same environment, useful when PATH is stale)

### Core commands

- `usertest run`
  - Run a single target repo.
  - Writes a run directory under `runs/usertest/…` (treat as sensitive by default; see `docs/ops/security.md`).
- `usertest batch`
  - Run multiple targets from a YAML file.
  - Validation runs in phases: (1) parse/shape checks of `targets.yaml`, then (2) catalog/policy/environment checks before any execution.
  - Inspection mode: `usertest batch --targets <file> --print-requests` prints resolved requests as JSON and exits without executing.
- `usertest report`
  - Re-render `report.md` / `report.json` for an existing run directory.

### Discovery commands

- `usertest personas list`
- `usertest missions list`

These reflect the merged catalog (runner defaults + target `.usertest/catalog.yaml` if present).

### Scaffolding command

- `usertest init-usertest`
  - Initialize `.usertest/` inside a *local* target repo.
  - Produces a starter `catalog.yaml` and optional sandbox install manifest.

### Reports pipeline

- `usertest reports compile`
  - Compile run directories into a JSONL history.
- `usertest reports analyze`
  - Analyze a history file and produce an issue summary.

Most backlog/ticket workflows have moved to `usertest-backlog`.

---

## `usertest-backlog`

Entry points:

- `usertest-backlog …` (installed script)
- `python -m usertest_backlog.cli …` (same environment, useful when PATH is stale)

### Reports workflows

Commands are grouped under `usertest-backlog reports`:

- `compile` – build a run history file
- `analyze` – analyze outcomes
- `intent-snapshot` – snapshot a repo intent for analysis
- `review-ux` – UX-focused review of reports
- `export-tickets` – write ticket export JSON/Markdown and synchronize configured local plan files
- `backlog` – build/render backlog documents

### Six-stage backlog pipeline (`reports backlog`)

`usertest-backlog reports backlog` runs a six-stage, inspectable pipeline and writes stage artifacts
next to the final backlog output:

- `*.problem_records.json` / `*.problem_records.md`
- `*.prioritized_problems.json` / `*.prioritized_problems.md`
- `*.research.json` / `*.research.md`
- `*.solution_options.json` / `*.solution_options.md`
- `*.solution_selection.json` / `*.solution_selection.md`
- `*.change_plans.json` / `*.change_plans.md`
- `*.backlog.json` / `*.backlog.md`

`--dry-run` is offline. It writes deterministic problem and prioritization artifacts, then records a
blocked research proof because no reproduction or repository inspection occurred. The research gate
therefore leaves optioning, selection, and planning empty. This validates orchestration and fail-closed
stage behavior without manufacturing implementation readiness.

`--research-ref <git-ref>` selects the source-of-truth revision for research. When omitted, the CLI
uses `backlog_research.source_ref` from `configs/backlog_research.yaml`. A live stage-3 run is blocked
if neither is configured. The ref is resolved before acquisition and the resulting commit is carried
through research, optioning, selection, and planning.

`configs/backlog_research.yaml` also selects the clean-replay executor. The default
`platform_router` sends platform-neutral/Linux evidence to the configured Docker image with
networking disabled and no inherited host environment. An explicitly Windows-only experiment may
use `trusted_host` only for an existing local `--repo-input` under a non-empty
`replay_trusted_host_roots` allowlist. Invalid, absent, or platform-mismatched routing is
fail-closed.

`--shadow` runs all six stages and records depth-invariant and stability hashes, but does not update
the atom-action ledger or export tickets. It cannot be combined with `--dry-run`. With the default
`configs/backlog_export_gate.yaml`, `reports export-tickets` refuses to mutate plan folders until the
configured number of consecutive passing shadow cycles share a stable source-observation atom
corpus, canonical case/plan-intent projection, complete pipeline source/configuration manifest, and stage-3 proof
basis. The proof basis binds origin artifact and assignment hashes, repository revision, verified
experiment/state/output receipts, causal and control links, inspected source, and the actual Docker
image ID observed by each replay. Mutable tags, run-local paths, container names, and generation
timestamps do not substitute for or perturb that immutable basis; a missing Docker image ID fails
closed. The atom corpus hash covers evidence content, severity, and lineage, not only atom IDs. When
`require_exact_export_projection` remains as a compatibility setting, but cross-cycle exactness is
defined over canonical intent: source evidence, case identity, verified mechanism binding, target
paths/symbols, and executable before/after oracles. Generated prose, fingerprints, and plan revision
IDs do not reset the streak. The latest validated backlog file and complete rendered export
projection are still byte-bound to export, so any edit after validation locks the gate. Newly generated derived research evidence does
not by itself reset the stability counter when its verified proof basis is unchanged. Shadow state
schema 7 and cycle schema 5 reject older state without migration; archive prior state and run a fresh
qualifying streak.

Note: stage 1 problem mining writes its atom payload into each miner workspace as a small
`atoms.json` manifest plus chunk files under `atoms_chunks/` (for example:
`*.backlog_artifacts/problem_mining/**/workspace/atoms.json` and
`*.backlog_artifacts/problem_mining/**/workspace/atoms_chunks/atoms_001.json`). Prompts instruct
the model to read the manifest and then the chunk files. This avoids oversized prompts while
preserving full evidence text. The canonical atom stream is still written as JSONL in
`*.backlog.atoms.jsonl`.

### PR triage

- `usertest-backlog triage-prs`

---

## `usertest-implement`

Entry points:

- `usertest-implement …` (installed script)
- `python -m usertest_implement.cli …` (same environment, useful when PATH is stale)

### Core command

- `usertest-implement run`
  - Implement a single exported backlog ticket in a target repo.
  - Writes a run directory under `runs/usertest_implement/…` with ticket linkage artifacts (treat as sensitive by default; see `docs/ops/security.md`).
  - Git finalization is controlled by existing flags and by the auto-loaded
    `configs/usertest_implement_settings.yaml` profile. The parser defaults are local, but the
    default settings profile enables `commit: true`, `push: true`, and `pr: true`.
    - `--commit` / `--no-commit` creates or disables a branch + commit in the kept workspace.
    - `--push` / `--no-push` pushes or disables pushing the branch to the configured remote.
    - `--pr` / `--no-pr` attempts or disables best-effort PR creation using GitHub CLI (`gh`).
      (`gh` must be on `PATH` and authenticated.)
- `usertest-implement resume --run-dir <run_dir>`
  - Re-enters a run whose `ticket_resume_state.json` is
    `verification_failed_resume_ready`.
  - Builds a focused verification-failure prompt from the recorded verification, reuse, attempt,
    workspace, ticket, and prior-report artifacts instead of replaying the original full ticket
    prompt.
  - Reuses the recorded workspace when it still exists, or checks out the recorded branch from the
    inferred/overridden repo (`--repo`, `--ref`) when the workspace is gone.

### Reports utilities

- `usertest-implement reports summarize`
  - Summarize implementation runs into JSONL for analysis.

### Ticket queue helpers

- `usertest-implement tickets list|next|move`
  - Work with `.agents/plans/*` ticket queues.
- `usertest-implement tickets run-next`
  - Standard flow: refresh backlog exports (including `review-ux`) and implement the next ticket (research-first).
  - With the default settings profile, this flow commits, pushes, and opens a PR unless you pass
    the existing `--no-commit`, `--no-push`, and/or `--no-pr` flags.
  - `--dry-run` stops implementation/finalization, but the default backlog refresh still runs
    unless you also pass `--no-refresh-backlog`.
- `usertest-implement batch run --config configs/backlog_implement_batch.yaml`
  - Uses the clean `--repo-root` for code/config/venvs and `defaults.owner_root` for historical runs,
    local plan queues, action ledgers, and batch receipts.
  - Fetches and records one exact `wave_base_ref` revision. Research, every generated plan target,
    and implementation must use that same commit.
  - Drains blocker/high, medium, and low automated work. Non-generated and IDEA tickets are ignored.
  - Writes `terminal_proof.json`; only a fresh stable zero export, terminal canonical case graph, and
    empty generated queue constitute completion. A pass awaiting PR/outcome reconciliation remains
    `awaiting_terminal_proof` rather than claiming the backlog is resolved.
- `usertest-implement batch status|recover --owner-root <path>`
  - Reads or recovers the batch state stored under the data owner rather than assuming it is the
    clean code checkout.
- `usertest-implement review run`
  - Runs the implementation-review agent and posts the resulting PR review comment by default.
  - `--dry-run` prints the review request without running the agent or publishing the PR review.

---

## Common flags and concepts

### `--repo-root`

Path to *this* runner repo’s root. Used to locate `configs/`, prompt templates, schemas, etc.

### `--repo`

Notes:

- For discovery-style commands that only need to read a target's `.usertest/catalog.yaml` (for example `usertest personas list`, `usertest missions list`, and `usertest lint`), local paths are read in-place, while git URLs are cloned to a temp dir.
- For `usertest run`, the target is acquired into a workspace directory (clone/copy) for isolation before execution.

The target under test. Can be:

- local path
- git URL
- `pip:<package>` / `pdm:<spec>` for “fresh install” evaluations

### `--agent`

Which adapter to use (`codex`, `claude`, `gemini`). Configured in `configs/agents.yaml`.

When `--model` is omitted, the adapter uses its `configs/agents.yaml` `default_model` if one is
configured.

### `--policy`

Execution policy for the agent (`safe`, `inspect`, `write`). Configured in `configs/policies.yaml`.

---

## Always prefer `--help` for exact flags

These CLIs evolve quickly.

Use:

```bash
python -m usertest.cli --help
python -m usertest.cli run --help
python -m usertest_backlog.cli --help
python -m usertest_implement.cli --help

# If PATH already exposes the console scripts:
usertest --help
usertest-backlog --help
usertest-implement --help
```
