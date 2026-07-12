# Stage 3: evidence-gated causal research

## Objective

Establish whether an assigned, still-unresolved problem is real; identify its causal mechanism;
challenge plausible alternatives; and define an executable positive outcome. Do not implement the
product change. A detailed report is not sufficient unless retained runner evidence supports it.

## Evidence and workspace rules

- Treat assigned source atoms as immutable problem evidence. Research/implementation atoms are
  derived evidence for their parent case unless they expose a distinct failure.
- Use the pinned repository revision and retained artifacts. Inspect the actual change surface.
- Commands are direct argv with `shell=false`. Use a tracked repository entrypoint or an attested
  research harness. The language/runtime is not prescribed; inline interpreter commands and shell
  control syntax are forbidden. If a repository-native runner has no repository path in argv
  (for example a build tool), declare `repository_bindings` with exact inspected tracked paths and
  an open-language `relationship`; the runner independently binds every path and blob.
- Research-only files belong under `.usertest_research/`. Do not implement the fix or mutate tracked
  files. A replay may mutate only declared `replay_setup.disposable_state_paths` in its isolated
  clone. Undeclared/tracked mutation is rejected.
- `replay_setup.environment` applies only to the child replay. Never request credential, provider,
  Codex/OpenAI, auth, token, PATH, or host-control variables. Values are hash-attested and redacted.
- Missing evidence, unavailable platforms, blocked execution, and malformed artifacts are honest
  limits. Do not convert them into support.

## Experiments

Each experiment has a stable `experiment_id`, nonempty descriptive `scenario_kind`, optional
runner-supported `platform_requirement`, assigned `addresses_atom_ids`, direct command, result,
outcome, exit code, observable assertion, and artifact references. Scenario/platform labels are
open values; they describe the actual experiment and do not determine whether it advances.

Collectively, supporting experiments must cover the assigned source atoms. Bind an experiment to
the exact source symptom or expected-behavior field where possible. A high-confidence opinion does
not replace a controlled observation.

`origin_evidence_bindings` may bind a structured symptom field with `observation_predicate` using
any registered deterministic predicate. The runner first evaluates it against the immutable atom
value, then against the adapter's baseline observation, and retains both in one content-addressed
source-root receipt. Do not stringify numbers, booleans, JSON objects, state, or events merely to
fit a text assertion.

For the primary hypothesis, attempt to falsify it. State the disproof condition before interpreting
the result. A survived challenge must retain the baseline, intervention/challenge, observations,
and shared mechanism. Alternatives and counterevidence remain in the dossier even when refuted.

## Open causal proof adapters

Use `experiment.proof_adapter` when a controlled pair establishes a mechanism. The core contract
requires only authenticated source lineage, distinct runner observations, a stable intervention,
a connected runner-attested mechanism graph, and a problem-bound positive predicate. Adapter-owned
types and `state_inputs` are open; do not force an environment/config/runtime failure into a Python
call-chain shape.

Built-in adapters currently include:

- `structured_replay.v1` and `command_trace.v1` for command/output/event differences;
- `environment.v1` for runner-attested child-environment deltas;
- `filesystem_state.v1` and `config_repository_state.v1` for declared isolated state;
- `platform.v1` for platform-routed observations;
- `python_call_chain.v1` and `pytest_controlled_difference.v1` as optional specialized adapters.

Other registered adapters are valid. If an adapter or predicate is unavailable, correct the claim
in the same author session or report insufficient evidence; do not invent a receipt.

Keep the causal locator separate from the repository change surface. When research establishes the
connected production target, add `proof_adapter.implementation_touchpoints` entries with the exact
`causal_locator`, inspected repository `path`, zero or more inspected `symbols`, and an open-language
`relationship` explaining how that file consumes or governs the causal target. The runner retains a
touchpoint only when the locator equals the attested intervention target and the path/symbols match
its read receipts. Never use `env:...`, `fs:...`, platform labels, or a research harness as a fake
repository path. If no connected production touchpoint is established, report that material change-
surface unknown; optioning must return the case to research.

`inspected_symbols` may be empty when the mechanism is genuinely file/config/schema/template/asset
or platform-oriented and the runner can bind a symbol-less implementation touchpoint to an observed
repository file and the causal proof. Do not invent a symbol merely to satisfy a code-shaped format.
Conversely, a code-symbol route still requires exact inspected-symbol receipts; a file read alone
does not establish a code path.

Example adapter claim:

```json
{
  "adapter_id": "environment.v1",
  "hypothesis_id": "hypothesis:child-environment",
  "baseline_experiment_id": "experiment:mode-absent",
  "challenge_experiment_id": "experiment:mode-present",
  "intervention": {
    "kind": "child_environment_variable",
    "target": "env:APPLICATION_MODE",
    "predicted_polarity": "absent_to_ready",
    "before": null,
    "after": "ready"
  },
  "observations": {
    "baseline": {"source": "stdout_json", "json_pointer": "/mode"},
    "challenge": {"source": "stdout_json", "json_pointer": "/mode"}
  },
  "positive_outcome": {
    "predicate": {"kind": "equals", "expected": "ready"},
    "semantic_basis": {
      "kind": "origin_exact_value",
      "atom_id": "atom:assigned-source",
      "field_path": "$.expected_mode"
    }
  }
}
```

Observation sources include runner-retained exit code, stdout/stderr text or JSON, event lines/JSON,
executed argv, platform, and adapter-specific isolated state. Use the narrowest observation that
tests the claimed mechanism.

## Positive outcome

A failure disappearing is not automatically success. Bind the desired result to one runner-minted
semantic basis:

- `origin_exact_value`: an exact structured expected/desired/required field from a source atom;
- `repository_fail_first_command`: a pre-existing, hash-bound repository command that fails before
  and passes under the controlled challenge;
- `authenticated_semantic_citation`: an exact authenticated source field plus rationale/relation.
  This proves authenticity only and sets `semantic_review_required`; Stage 5 judges its meaning.

Use a registered deterministic predicate. Built-ins include `equals`, `membership`, `range`,
`schema`, `existence`, `state_transition`, and `event_sequence`; registered domain predicates are
also valid. The runner evaluates the predicate against retained observations. Do not choose an
observed value first and retroactively label it correct.

The resulting positive contract must remain executable after implementation using the original
baseline command/setup, the registered adapter observation, and the same predicate. Keep code/test
proof separate from live-runtime proof.

## Verification boundary

When an experiment will be the original-scenario oracle, add a structured
`verification_boundary` with an open nonempty `boundary_kind`, boolean
`requires_live_verification`, boolean `faithful_equivalence`, and a concrete `rationale`. Requiring
live proof is always allowed when the replay is attested. Waiving it requires
`faithful_equivalence=true`; the runner will retain that waiver only when the experiment is bound
to both the selected mechanism evidence and an executable outcome oracle. The runner mints
provenance references and the boundary hash. Provider names, scenario-label vocabulary, and prose
keyword matching do not establish or waive live verification.

## Root cause and sufficiency

`root_cause_hypotheses[0]` is the primary hypothesis. Name exact mechanism locators appropriate to
the adapter: code symbols/paths, `env:...`, `fs:...`, `config:/...`, platform routes, commands, or
registered domain locators. Explain the causal chain, evidence for and against it, boundaries, and
residual recurrence paths.

Use `research_status="evidence_sufficient"` only when runner evidence establishes the primary
mechanism, a meaningful falsification survives (or an authenticated deterministic closure exists),
the positive outcome is bound, and no material unknown affects the root cause, interface choice,
or change surface. `root_cause_confidence` is telemetry, not a threshold. Otherwise use
`insufficient_evidence` or `blocked` and state exactly what evidence is needed.

Every `material_unknowns` item says what is unknown, what it affects, evidence needed, and whether
it is material. Material unknowns block implementation planning; explicit nonmaterial observations
may remain without blocking.

When `problem_record.case_identity_status` is `provisional_same_cause`, relation review has formed
a research hypothesis, not a durable merge. Treat every entry in
`problem_record.provisional_same_cause_group.member_facets` as an independent symptom facet. One
advancing dossier must directly bind every member's source evidence and show that the established
mechanism explains all of them. Evidence for only a subset, a different mechanism for any member,
or a material unknown about the shared mechanism remains blocked. Do not claim that the member
cases are aliases; the runner preserves their original IDs until the combined proof passes.

## Output

Return one complete `troubleshoot_v1` report. Its `extensions.backlog_repro_research` object contains:

```json
{
  "case_id": "case:exact-assigned-id",
  "problem_id": "problem:exact-assigned-id",
  "research_method": "short accurate method label",
  "reproduction_status": "reproduced | reproduction_failed | partial | blocked",
  "research_status": "evidence_sufficient | insufficient_evidence | blocked",
  "writes_used": false,
  "writes_purpose": ["none"],
  "implementation_performed": false,
  "artifact_refs": [],
  "experiments": [],
  "inspected_files": [],
  "inspected_symbols": [],
  "root_cause_hypotheses": [],
  "root_cause_confidence": 0.0,
  "broader_class_assessment": "isolated_instance | repeated_variant | unknown",
  "material_unknowns": [],
  "blocking_reasons": [],
  "evidence_boundaries": []
}
```

The runner adds schema version, repository revision, evidence assignment, diff classification, and
verification receipts. Do not emit those runner-owned fields.

## Correction behavior

If output structure/reference interpretation is wrong, correct the complete dossier in the exact
author session. If the runner verifier discovers an evidence gap, continue research in that same
session and workspace with research capabilities, run the needed experiment, and return the full
dossier. Do not restart at the first defect. Restart only after repeated nonprogress, lost immutable
provenance/session/workspace, or correction cost equivalent to a fresh investigation.
