---
id: backlog_repro_research
name: Backlog Repro + Research (Writable Workspace)
extends: null
tags: [repo_local, backlog, research, reproduction]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: troubleshoot_v1.schema.json
requires_shell: true
requires_edits: true
---

# Mission: reproduce and establish the causal mechanism

You are researching one automatically mined backlog case at a pinned repository revision.

## Goal

Determine whether the assigned source evidence describes a real, unactioned problem. Establish the
causal mechanism deeply enough that a later planner can choose a root-cause solution, challenge the
best alternative explanation, and define what would prove the problem solved.

Do not implement the product change. Research-only harnesses are allowed under
`.usertest_research/`; tracked product changes are not.

## Required behavior

1. Read every assigned source atom and retained attachment before forming the primary hypothesis.
2. Inspect the actual repository revision and relevant mechanism surface.
3. Run faithful experiments when possible. Commands are shell-free direct argv and must bind to a
   tracked repository entrypoint or an attested research harness. Any language/runtime is valid.
4. Compare a baseline with a controlled challenge. Use an open registered proof adapter when the
   mechanism is environment, filesystem/config state, platform, command/event behavior, a code
   path, or another registered domain type.
5. Attempt to falsify the primary hypothesis and retain counterevidence.
6. Bind the positive outcome to authenticated origin/repository meaning and a runner-evaluated
   registered predicate. Disappearance of an error alone is not proof of correctness.
7. Report material unknowns honestly. Confidence is telemetry, never a substitute for evidence.

For a non-code file/config/schema/template/asset/platform mechanism, `inspected_symbols` may be
empty only when a symbol-less `implementation_touchpoints` entry connects the causal locator to a
runner-observed repository file. Code-symbol mechanisms still require exact symbol inspection.

The stage guidance in the system prompt contains the current adapter, semantic-basis, predicate,
setup, and output contracts. Follow that contract; do not impose Python/pytest-only evidence shapes
or fixed scenario/platform vocabularies.

## Evidence sufficiency

Use `evidence_sufficient` only when retained evidence establishes:

- an assigned source symptom and its unactioned status;
- for a provisional same-cause research unit, every retained member facet and evidence that the
  same mechanism explains each member (the grouping is a hypothesis, not permission to merge);
- a connected causal path from source condition through the intervention target to the outcome;
- a survived adversarial challenge or authenticated deterministic closure;
- an executable positive outcome contract; and
- no material unknown that changes root cause, interface choice, or change surface.

Otherwise use `insufficient_evidence` or `blocked`, preserving the useful experiments and naming the
next evidence needed. Optional specialized diagnostics may be unavailable without invalidating an
independent causal proof.

## Report

Return one complete `troubleshoot_v1` report with a complete
`extensions.backlog_repro_research` object. Include the exact assigned `case_id` and `problem_id`,
method/status, writes declaration, artifact references, experiments, inspected files/locators,
hypotheses with falsification attempts, confidence telemetry, broader-class assessment, material
unknowns, blockers, and evidence boundaries. Set `implementation_performed` to `false`.

Do not emit runner-owned schema/revision/assignment/verification fields. Do not claim commands,
reads, artifacts, or outcomes that were not actually retained by this run.

## Correction

When the runner supplies feedback, correct the full dossier in this exact author session. If the
feedback requires evidence, continue in the same workspace with the available research tools and
actually run the missing experiment. Do not start over for the first correctable defect; restart
only when the author session/provenance is unavailable or repeated correction no longer makes
objective progress.
