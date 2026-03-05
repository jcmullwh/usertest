# Backlog six-stage pipeline

## Overview

The `usertest-backlog reports backlog` command produces a six-stage, inspectable pipeline
rather than jumping from raw evidence atoms directly to tickets with proposed fixes.
Each stage writes its own JSON and Markdown artifact so a developer can inspect the full
chain from evidence to actionable change plan.

## Old pipeline (one-pass)

```
atoms → miner prompts → tickets (with proposed_fix) → labeler → policy → backlog.json
```

Problems with the old approach:
- The miner prompt asked for a solution before a problem was formally identified.
- `change_surface` labeling ran on raw guesses rather than on a chosen approach.
- Deduplication was one-layer; there was no notion of "same cause, different problem."
- All policy gating happened before research confirmed or bounded the issue.

## New pipeline (six stages)

```
atoms
  ↓  stage 1: problem mining
problem_records.json / .md
  ↓  stage 2: prioritization
prioritized_problems.json / .md
  ↓  stage 3: reproduce-plus-research (writable isolated workspace)
research.json / .md
  ↓  stage 4: solution optioning
solution_options.json / .md
  ↓  stage 5: solution selection + labeler + policy gating
solution_selection.json / .md
  ↓  stage 6: implementation planning
change_plans.json / .md
  ↓  ticket assembly
backlog.json / .md   (export-compatible)
```

At each stage a generic *relation-review* step ranks candidate neighborhoods by signal
family (semantic, evidence overlap, metadata, path anchor) and may merge, split, or
group records, but only with an explicit decision. Automatic logic is supplementary and
exclusion-oriented; it never decides grouping on its own.

## Dry-run behavior (offline fixtures)

`usertest-backlog reports backlog --dry-run` must not invoke agents. To keep the
pipeline observable on offline fixtures, stages 1–2 synthesize deterministic artifacts
from atoms and deterministic pre-score signals, while still writing the would-be stage
prompts under the stage artifact tree for inspection.

## Artifact tree

Inside a compiled target directory the full artifact tree looks like:

```
target_a.problem_records.json
target_a.problem_records.md
target_a.prioritized_problems.json
target_a.prioritized_problems.md
target_a.research.json
target_a.research.md
target_a.solution_options.json
target_a.solution_options.md
target_a.solution_selection.json
target_a.solution_selection.md
target_a.change_plans.json
target_a.change_plans.md
target_a.backlog.json           ← export-compatible, backed by staged evidence
target_a.backlog.md
target_a.backlog_artifacts/
  problem_mining/
  problem_prioritization/
  repro_research/
  solution_optioning/
  solution_selection/
  implementation_planning/
```

## Stage contracts

Each stage has a strict schema contract enforced by parsers in
`packages/backlog_core/src/backlog_core/stage_contracts.py`:

| Stage | Parser | Forbidden fields |
|-------|--------|-----------------|
| 1 problem mining | `parse_problem_record_list` | `proposed_fix`, `selected_solution`, any solution-family field |
| 2 prioritization | `parse_priority_decision_list` | solution fields |
| 3 research | `parse_research_dossier_list` | `implementation_performed=true` blocked |
| 4 optioning | `parse_solution_option_sets` | `selected_solution` |
| 5 selection | `parse_selection_decisions` | — |
| 6 planning | `parse_change_plan_list` | — |

`ready_for_ticket` is only allowed in policy once a selected solution and a change plan
both exist (milestone 6). Until then the effective stage is `triage` or
`research_required`.

## Relation review

`packages/backlog_core/src/backlog_core/relation_review.py` wraps `triage_engine` to
produce *candidate neighborhoods* at each stage. A neighborhood exposes signal families
separately:

```json
{
  "focus_id": "problem:readme-quickstart-missing",
  "stage": "problem_mining",
  "most_related_by_semantic": [ ... ],
  "most_related_by_evidence_overlap": [ ... ],
  "most_related_by_metadata": [ ... ],
  "most_related_by_path_anchor": [ ... ],
  "split_hints": [ ... ]
}
```

Automatic code only produces neighborhoods. A stage-specific reviewer prompt decides
merge, keep-separate, split, or same-cause-group based on the neighborhoods.

## Solution-family taxonomy

Solution families are defined as data in `configs/backlog_taxonomy.json`, not embedded
in prompt bodies. The initial three families are:
- `most_direct` – smallest targeted change that addresses the problem
- `most_robust` – adds defense-in-depth or broader correctness
- `most_comprehensive` – addresses the problem class, not just the instance

Prompt-guardrail terms that are banned unless injected from taxonomy text:
`fastest`, `quickest`, `easiest`, `simplest`, `lowest-effort`.

## Stage guidance

Per-stage guidance (what to favor, repo-specific rules, anti-patterns) lives in
`configs/backlog_stage_guidance/` and is injected by the prompt renderer. Editing policy
therefore means editing one config file, not multiple prompt templates.

## Automatic relation logic rule

Automatic relation logic is **supplementary, exclusion-oriented, and tuned against
fixtures**. It ranks candidate neighborhoods by signal family and stage. It never
auto-merges, auto-splits, or auto-groups. Tests must never assert that automatic logic
alone decides the final grouping.

## Implementation milestones

1. Shared foundation and stage 1 problem mining (this milestone)
2. Stage 2 problem prioritization
3. Stage 3 reproduce-plus-research in writable workspace
4. Stage 4 solution optioning
5. Stage 5 solution selection, labeler relocation, UX gating
6. Stage 6 implementation planning, final backlog assembly, cleanup

See `.agents/ops/backlog-six-stage-pipeline/backlog-six-stage-pipeline.execplan.md`
for the full plan of work, decision log, risks, and acceptance criteria.
