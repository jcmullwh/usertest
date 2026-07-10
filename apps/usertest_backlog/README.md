# `usertest-backlog` CLI

`usertest-backlog` is the backlog-focused companion CLI.

Use it when you already have usertest runs and you want to:

- compile run directories into a history file
- analyze outcomes across many runs
- build/review backlog documents
- export tickets
- triage PRs
- triage issue backlogs into dedupe + theme clusters

If you're looking to *run* usertests, use `usertest` instead.

---

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
python -m pip install -e apps/usertest_backlog
```

Confirm:

```bash
python -m usertest_backlog.cli --help
# If PATH already exposes the console script: usertest-backlog --help
```

---

## Core commands

### Reports workflows

Commands are grouped under `reports`:

- `compile`: scan run directories and write a JSONL history file
- `analyze`: analyze a history file and write an issue analysis summary
- `window`: summarize the last N runs vs previous N runs (timing + outcomes + regressions)
- `review-ux`: UX-focused review of reports
- `export-tickets`: write ticket export JSON/Markdown and synchronize configured local plan files
- `backlog`: render backlog documents

Example:

```bash
usertest-backlog reports compile --repo-root . --runs-dir runs/usertest --out runs/usertest/report_history.jsonl
usertest-backlog reports analyze --repo-root . --history runs/usertest/report_history.jsonl
usertest-backlog reports window --repo-root . --runs-dir runs/usertest --last 12 --baseline 12
```

Notes:

- `reports analyze --history <path.jsonl>` reads the compiled JSONL directly.
- When `--history` is used and `--out-json/--out-md` are omitted, outputs are written next
  to the history file as `<stem>.issue_analysis.json` and `<stem>.issue_analysis.md`.
- `reports backlog` runs the **six-stage backlog pipeline** and writes inspectable stage artifacts
  alongside the final backlog:

  - `*.problem_records.json` / `*.problem_records.md`
  - `*.problem_records.evidence_receipt.json` (full-read and exact disposition proof)
  - `*.prioritized_problems.json` / `*.prioritized_problems.md`
  - `*.research.json` / `*.research.md`
  - `*.solution_options.json` / `*.solution_options.md`
  - `*.solution_selection.json` / `*.solution_selection.md`
  - `*.change_plans.json` / `*.change_plans.md`
  - `*.case_registry.json`
  - `*.backlog.json` / `*.backlog.md`

  `review-ux` is driven by stage 5 (`*.solution_selection.json`) rather than early-stage tickets.
- `reports backlog --dry-run` is offline: it does not invoke an agent. Stages 1-2 emit
  deterministic fixture artifacts, stage 3 records blocked research, and stages 4-6 remain empty.
  Its stage-1 receipt explicitly records that no model read attestation occurred and is never
  shadow/export eligible. Dry-run validates orchestration and evidence gates; it never synthesizes
  implementation readiness.
- `reports backlog --research-ref <git-ref>` overrides the configured stage-3 source ref. Live
  research resolves that ref before acquisition and binds every downstream artifact to the resolved
  commit. The default is `backlog_research.source_ref` in `configs/backlog_research.yaml`.
- Stage-3 clean replays use the explicit `backlog_research.replay_executor` contract. The repo
  default is `platform_router`: platform-neutral/Linux evidence runs in Docker with
  `--network none` and no forwarded host environment, while an explicitly Windows-only
  experiment may use `trusted_host` for an existing local `--repo-input` inside one of
  `replay_trusted_host_roots`. Missing, invalid, or mismatched routing configuration fails closed.
  The selected boundary is retained in the stage input and replay evidence receipts.
- Post-research consolidation requires the same repository revision and a runner-verified causal
  signature that includes the controlled causal branch; a shared path or symbol alone cannot merge
  cases. The canonical dossier retains and revalidates every member proof and outcome oracle. Stage
  5 selects one supported positive outcome contract per retained oracle, and stage 6 emits a signed
  multi-scenario replay when a canonical case represents more than one original scenario.
- `reports backlog --shadow` runs the full agent-backed pipeline without updating the atom-action
  ledger or exporting tickets. The export gate uses `required_consecutive_shadow_cycles`; with
  `require_exact_export_projection: true`, every cycle in that streak must share the same
  canonical case and causal plan intent: evidence, mechanism binding, exact targets, and
  executable oracles. Generated titles, prose, fingerprints, and content-addressed plan revision
  IDs are not stability signals. The latest backlog and full export projection remain byte-bound
  to export independently of that cross-cycle semantic comparison.
- Backlog mining knobs from the legacy one-pass miner (`--miners`, `--coverage-miners`,
  `--bagging-miners`, `--orphan-pass`, `--labelers`, merge flags) are still accepted but are
  ignored by the six-stage pipeline (the CLI prints a note when they are non-default).
- `reports backlog` excludes operationally ticketed/queued/actioned atoms by default, while
  canonical case lineage prevents derived research or implementation evidence from becoming a new
  issue by default. An explicit `novel_case` disposition and rationale is required for a distinct
  research- or implementation-infrastructure failure. Queue state does not mean resolved; durable
  outcomes distinguish tested, live-verified, resolved, mitigated, duplicate, superseded, and
  unverified work.
  A complete plan-folder scan also resets a legacy `actioned` atom to `new` when neither a
  surviving plan nor a provenance-verified terminal outcome exists. This fail-open recovery does
  not apply to IDEA-originated records and preserves the old ledger fields as audit history.
  To regenerate the backlog while keeping only actioned work excluded, use
  `--carryover-actioned-only` (demotes `ticketed`/`queued` atoms back to `new` before filtering).

### PR triage

```bash
usertest-backlog triage-prs --in apps/usertest_backlog/tests/fixtures/pr_list.json
```

### Backlog triage (themes)

```bash
usertest-backlog triage-backlog \
  --in apps/usertest_backlog/tests/fixtures/sample_issue_backlog.json
```

Optional flags:

- `--group-key <field>`: compute cross-group coverage using a specific field. If omitted,
  `package` is used automatically when present.
- `--out-json`, `--out-md`, `--out-xlsx`: override output paths. Defaults are based on the
  input filename (`.triage_backlog.json` and `.triage_backlog.md`).
- `--dedupe-overall-threshold`, `--theme-overall-threshold`, `--theme-k`,
  `--theme-representative-threshold`: tune clustering behavior.

Embedding/runtime notes:

- Real embedding runs require `OPENAI_API_KEY`.
- Set `TRIAGE_ENGINE_EMBED_CACHE_PATH` to reuse embeddings via an on-disk SQLite cache across
  repeated runs.
- XLSX output requires `openpyxl` to be installed in the environment.

---

## Configuration

This CLI relies on:

- run artifact contract (`docs/design/run-artifacts.md`)
- backlog policy and prompt manifests under `configs/`
- repo-local tracking under `.agents/` (plans, todos, actions ledgers; typically local-only / git-ignored)

Operational notes (security, CI guidance) live under `docs/ops/`.

---

## Development

From the repo root:

```bash
# Run the repo bootstrap once first if this is a fresh checkout:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1
#   # or: bash ./scripts/smoke.sh
python tools/scaffold/scaffold.py run install --project usertest_backlog
python tools/scaffold/scaffold.py run test --project usertest_backlog
python tools/scaffold/scaffold.py run lint --project usertest_backlog
```

Smoke tests:

```bash
python -m pytest -q apps/usertest_backlog/tests/test_smoke.py
```
