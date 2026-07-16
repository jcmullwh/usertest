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
causal mechanism deeply enough that a later planner can choose a root-cause solution, challenge the best
alternative explanation, and carry desired behavior, constraints, and verification obligations forward.

Do not implement the product change. Research-only harnesses are allowed under
`.usertest_research/`; tracked product changes are not.

## Required behavior

1. Read the complete assigned-evidence index. Open every complete source atom needed to account for
   the assignment, and read each retained attachment chunk actually relied on by a claim. Do not
   claim unread optional attachment material was reviewed.
2. Inspect the actual repository revision and relevant mechanism surface.
3. Establish current actionability from the pinned revision, Git history, regression coverage, and
   retained outcomes. A historical failure predating a verified fix is not unactioned work.
4. Run faithful experiments when possible. Commands are shell-free direct argv and must bind to a
   tracked repository entrypoint or an attested research harness. Any language/runtime is valid.
5. Compare a baseline with a controlled challenge. Use an open registered proof adapter when the
   mechanism is environment, filesystem/config state, platform, command/event behavior, a code
   path, or another registered domain type.
6. Attempt to falsify the primary hypothesis and retain counterevidence.
7. Use a runner-evaluated registered predicate for each causal adapter and bind it to authenticated
   origin/repository meaning. A `positive_outcome` with role `causal_contrast` is mechanism evidence,
   not a future success contract. Carry desired behavior and constraints forward without inventing
   an algorithm, interface, safety matrix, or command.
8. Report material unknowns honestly. Confidence is telemetry, never a substitute for evidence.

For a non-code file/config/schema/template/asset/platform mechanism, `inspected_symbols` may be
empty only when a symbol-less `implementation_touchpoints` entry connects the causal locator to a
runner-observed repository file. Code-symbol mechanisms still require exact symbol inspection.

The stage guidance in the system prompt contains the current adapter, semantic-basis, predicate,
setup, and output contracts. Follow that contract; do not impose Python/pytest-only evidence shapes
or fixed scenario/platform vocabularies.

## Evidence sufficiency

Use `evidence_sufficient` only when retained evidence establishes:

- an assigned source symptom and its current actionability status;
- for a provisional same-cause research unit, every retained member facet and evidence that the
  same mechanism explains each member (the grouping is a hypothesis, not permission to merge);
- a connected causal path from source condition through the intervention target to the outcome;
- a survived adversarial challenge or authenticated deterministic closure;
- no unresolved empirical unknown that could change the root cause, current actionability, or
  connected production change surface.

Evidence sufficiency is separate from actionability. Emit `actionability_assessment` as `requires_change`, `already_addressed`, `non_actionable`, or `undetermined`, with retained evidence
references. A complete negative is valid Stage-3 throughput: keep it `evidence_sufficient`; do not
downgrade sound research merely to stop optioning.

Partial reproduction is not itself a blocker when independent retained evidence satisfies these requirements.
An unknown is material only when a plausible answer could change the established mechanism, connected
touchpoint, or current actionability. A future design parameter, supported-interface choice,
future safety proof, or outcome-family choice is Stage-4/5 work: carry evidence, constraints,
alternatives, desired behavior, and residual recurrence paths forward instead of requiring the
unimplemented solution to prove itself before optioning.
Unknown vendor internals or history may remain nonmaterial at a verified invariant boundary.
An outstanding live verification obligation belongs in the verification boundary unless it leaves the
present mechanism or connected change surface undecided. A future solution oracle is optional,
non-authoritative Stage-3 output. Use blockers only for a prevented causal proof element, not unavailable
future-design details or optional diagnostics.

Otherwise use `insufficient_evidence` or `blocked`, preserving the useful experiments and naming the
next evidence needed. Optional specialized diagnostics may be unavailable without invalidating an
independent causal proof.

## Report

Return one complete `troubleshoot_v1` report with `extensions.backlog_repro_research`, exact assigned
IDs, method/status, writes, artifacts, experiments, inspected locations, hypotheses/falsification,
confidence, breadth, unknowns, actionability, blockers, and boundaries. Do not implement.

Do not emit runner-owned schema/revision/assignment/verification fields. Do not claim commands,
reads, artifacts, or outcomes that were not actually retained by this run.

## Correction

When the runner supplies feedback, return it to the exact author session and correct the full dossier
there. If the feedback requires evidence, continue in the same workspace with the available research tools and
actually run the missing experiment. Do not start over for the first correctable defect; restart
only when the author session/provenance is unavailable or repeated correction no longer makes objective progress.
