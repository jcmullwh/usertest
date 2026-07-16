# Stage 3: evidence-gated causal research

## Objective

Establish whether an assigned, still-unresolved problem is real; identify its causal mechanism; and
challenge plausible alternatives. Carry source-grounded desired behavior, constraints, and verification
obligations forward so later stages can design and verify a root-cause solution. Do not design or
implement the product change. Retained runner evidence must support the present failure mechanism.

## Evidence and workspace rules

- Treat assigned source atoms as immutable problem evidence. Research/implementation atoms are
  derived evidence for their parent case unless they expose a distinct failure.
- Use the pinned repository revision and retained artifacts. Inspect the actual change surface.
- Search and `Select-String` output are discovery only. For every file or symbol listed as
  inspected, perform a standalone attested read after locating it: use
  `Get-Content -Raw -Encoding UTF8 -LiteralPath <path>` for a small file, or the exact bounded
  form `Get-Content -Encoding UTF8 -LiteralPath <path> | Select-Object -Skip <N> -First <M>`
  for a large file. Do not chain either read with markers or unrelated commands. A bounded read
  must contain the complete claimed definition.
- Commands are direct argv with `shell=false`; use `/` in persisted repo-relative paths. Any language/runtime is valid. Use a tracked repository entrypoint or attested research harness;
  inline code and shell control syntax are forbidden. If a repository-native runner has no repository path in argv (for example a build tool), declare `repository_bindings` with exact
  inspected tracked paths and an open-language `relationship`; the runner independently binds every path and blob.
- Prefer the narrowest inspected causal API; use nested full runners only when the mechanism requires them, make dummy agents satisfy version/preflight contracts, and after a pre-mechanism stall or failure switch methods or stop rather than rerunning unchanged.
- Research-only files belong under `.usertest_research/`; do not implement or mutate tracked files; replay may mutate only declared `replay_setup.disposable_state_paths` in its isolated clone.
- Retain explicit evidence files there; never redirect TEMP/TMP, pytest basetemp, or tool caches there; if ordinary temp storage is unavailable, record the test as blocked, without retained scratch.
- `replay_setup.environment` applies only to the child replay. Never request credential, provider, Codex/OpenAI, auth, token, PATH, or host-control variables. Values are hash-attested and redacted.
- Missing evidence, unavailable platforms, blocked execution, and malformed artifacts are honest limits. Do not convert them into support.
## Experiments

Each experiment has a stable `experiment_id`, nonempty descriptive `scenario_kind`, optional
runner-supported `platform_requirement`, assigned `addresses_atom_ids`, direct command, result,
outcome, exit code, observable assertion, and artifact references. Scenario/platform labels are
open values; they describe the actual experiment and do not determine whether it advances.

Supporting experiments cover assigned atoms and bind the exact source symptom or expected behavior; confidence is not observation.
A harness proves a mechanism only when its production call determines the exact assertion; otherwise keep the result inconclusive and the causal gap material.
An inconclusive command stopped by an external timeout/kill (exit 124/137) is a blocked attempt, not a replay experiment. Retain its artifact and material unknown outside `experiments`.
If timeout is the assigned symptom, use a self-contained faithful replay whose `supports`/`refutes` outcome is the observed behavior, not the runner cutoff.

`origin_evidence_bindings` may bind a structured symptom field with `observation_predicate` using any registered deterministic predicate. The runner evaluates it against the immutable atom value,
then the adapter baseline, and retains both in one content-addressed source-root receipt. Do not stringify numbers, booleans, JSON objects, state, or events merely to fit a text assertion.

For the primary hypothesis, attempt to falsify it. State the disproof condition before interpreting
the result. A survived challenge must retain the baseline, intervention/challenge, observations,
and shared mechanism. Alternatives and counterevidence remain in the dossier even when refuted.

## Open causal proof adapters

Use `experiment.proof_adapter` when a controlled pair establishes a mechanism. The core contract
requires only authenticated source lineage, distinct runner observations, a stable intervention,
a connected runner-attested mechanism graph, and a problem-bound positive predicate. Adapter-owned
types and `state_inputs` are open; do not force an environment/config/runtime failure into a Python call-chain shape.

Built-ins include `structured_replay.v1` for controlled test/harness/command/event differences,
`command_trace.v1`, `environment.v1`, `filesystem_state.v1`,
`config_repository_state.v1`, and `platform.v1`. Other registered adapters are valid. If an adapter
or predicate is unavailable, correct the claim in the same session or report insufficient evidence; do not invent a receipt.

Keep the causal locator separate from the change surface. A connected
`proof_adapter.implementation_touchpoints` entry names the exact intervention `causal_locator`,
inspected repository `path`, zero or more inspected `symbols`, and their `relationship`. The runner
binds these to read receipts. Never use environment/filesystem/platform locators or a harness as a
fake repository path; an unestablished production touchpoint is a material change-surface unknown.
Symbol-less touchpoints are valid for real file/config/schema/template/asset/platform mechanisms;
code-symbol routes still require exact symbol receipts.

Adapter selectors are `exit_code`, `stdout_text`, `stderr_text`, `combined_text`, `stdout_json`,
`stderr_json`, `event_lines`, `event_json`, `executed_argv`, or `platform`; JSON adds `json_pointer`.
These differ from experiment assertions. Use the narrowest mechanism-testing observation.

## Causal predicates and downstream outcomes

The existing `proof_adapter.positive_outcome` field names the problem-bound predicate used to
interpret a controlled pair. It is current mechanism evidence, not automatically a post-change
success contract. Bind its semantic basis to authenticated source or repository meaning through
`origin_exact_value`, `repository_fail_first_command`, `repository_contract_quote`, or
`authenticated_semantic_citation`. Set `contract_role="causal_contrast"` whenever the predicate
only distinguishes the failure-producing condition from its control. The runner evaluates
registered predicates such as `equals`, `membership`, `range`, `schema`, `existence`,
`state_transition`, and `event_sequence`; never choose an observed value first and retroactively label it correct.

Future solution success is Stage-4/5 work. Stage 3 may retain an already-authenticated outcome candidate,
but it is optional and non-authoritative. Do not require or construct a future algorithm, interface
choice, exhaustive safety matrix, convergence/fixed-point proof, or exact post-change command merely
to advance causal research. A causal contrast remains valid mechanism evidence even when it is not
acceptable operational behavior. Record desired behavior, known-safe constraints, residual recurrence
paths, and the live-runtime obligation for downstream use.
Keep code/test confidence separate from live-runtime proof.

## Verification boundary

An experiment may declare `verification_boundary={boundary_kind,requires_live_verification,
faithful_equivalence,rationale}` to carry a known downstream obligation. Requiring live proof is
always allowed. A proposed equivalence or post-change oracle is optional at Stage 3 and must not
replace proof of the current mechanism. Missing future-solution detail is not a research blocker;
an inability to establish the source-bound mechanism or connected change surface is.

## Root cause and sufficiency

`root_cause_hypotheses[0]` is the primary hypothesis. Name exact mechanism locators appropriate to
the adapter: code symbols/paths, `env:...`, `fs:...`, `config:/...`, platform routes, commands, or
registered domain locators. Explain the causal chain, evidence for and against it, boundaries, and residual recurrence paths.

Before declaring a current change necessary, compare the dated source evidence with the pinned
revision and relevant Git history. Inspect existing regression tests and any retained later
runtime/outcome evidence available in the workspace. Preserve the distinction between historical
mechanism proof, code/test verification of an existing fix, and later live verification. Do not turn
a real but already-addressed historical failure into a new implementation case merely because the original atom remains in the corpus.

Use `evidence_sufficient` only when runner evidence establishes the primary mechanism, survived
falsification or deterministic closure, and no decision-changing unknown about the root cause,
current actionability, or connected production change surface.
Confidence is telemetry. `partial` reproduction may still be sufficient when independent evidence
proves every element; `blocked` may not. An unknown is material only if a plausible answer changes
the established mechanism, current actionability, or connected intervention/touchpoint. Do not
classify a future design parameter, future safety proof, outcome-family choice, or choice among
evidence-compatible interfaces as missing research. Record supported alternatives and constraints
for Stage 4; do not demand post-change execution before optioning. Missing live proof may remain a
`requires_live_verification=true` boundary once the present mechanism is established. Name each
unknown, affected decision, needed evidence, and materiality. Reserve `blocking_reasons` for a
prevented proof element; optional limits belong in boundaries or `material=false` unknowns.
`reproduction_status="partial"` means incomplete replay coverage, not automatic failure.
Materiality is relative to the implementation decision; optional diagnostics belong in boundaries,
not in `blocking_reasons`. Without a connected change surface, optioning must return the case to research.

Always emit `actionability_assessment`: `requires_change` for a current unactioned problem;
`already_addressed` for a real historical problem with retained current-fix evidence;
`non_actionable` when evidence establishes no product change is warranted; or `undetermined` when
the decision remains open. Cite declared experiment/artifact IDs. An evidence-sufficient negative
is successful research: do not delete controls, invent an unknown, or downgrade merely to stop
optioning. Stage 4 records the no-change outcome without a model call.

Always emit `case_relation_assessment`: `retain` for one causal work unit, `split` for at least two signed distinct causal/action boundaries, `keep_separate` for a relation hypothesis that remains separate, or `undetermined` when evidence cannot decide. Split is boundary research, not readiness; each child needs its own research.
For `split`, partition `occurrence_evidence_atom_ids` exactly once across two or more facets, each with title, problem, user impact, and boundary. Citations use an RFC 6901 `field_path` into that occurrence's signed snapshot and copy `exact_value` verbatim. Distinguish causal/action boundaries, not run IDs, timestamps, or wording; otherwise use `undetermined` and preserve the unknown.

When `problem_record.case_identity_status` is `provisional_same_cause`, relation review has formed
a research hypothesis, not a durable merge. Treat every entry in
`problem_record.provisional_same_cause_group.member_facets` as an independent symptom facet. One
advancing dossier must directly bind every member's source evidence and show that the established
mechanism explains all of them. Evidence for only a subset, a different mechanism for any member,
or a material unknown about the shared mechanism remains blocked. Do not claim that the member
cases are aliases; the runner preserves their original IDs until the combined proof passes.

## Output

Return one complete `troubleshoot_v1` report. Emit exactly the model-owned top-level fields shown
below in `extensions.backlog_repro_research`; unknown top-level fields fail validation. Nested
evidence uses the machine-checkable shapes below, not prose substitutes.

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
  "artifact_refs": [{"artifact_id": "artifact:unique", "kind": "open kind", "path": "existing regular file", "description": "optional"}],
  "experiments": [{
    "experiment_id": "experiment:unique", "scenario_kind": "open descriptive kind",
    "addresses_atom_ids": ["atom:assigned"], "command": "direct replayable command",
    "result": "observed result", "outcome": "supports | refutes | inconclusive", "exit_code": 0,
    "observable_assertion": {"source": "exit_code", "operator": "equals", "expected": 0},
    "artifact_refs": ["artifact:unique"]
  }],
  "inspected_files": ["repo/relative/path"],
  "inspected_symbols": ["exact.symbol.or.open.locator"],
  "root_cause_hypotheses": [{
    "hypothesis_id": "hypothesis:unique", "statement": "concrete failure-producing mechanism",
    "supporting_evidence": ["experiment:unique"], "counterevidence": [],
    "mechanism_symbols": ["exact.symbol.or.open.locator"], "disposition": "primary",
    "disposition_evidence": ["experiment:unique"], "falsification_attempts": []
  }],
  "root_cause_confidence": 0.0,
  "broader_class_assessment": "isolated_instance | repeated_variant | unknown",
  "case_relation_assessment": {
    "disposition": "retain | split | keep_separate | undetermined",
    "rationale": "evidence-based conclusion", "facets": [{
      "facet_id": "facet:stable-label", "title": "facet title", "problem": "facet problem", "user_impact": "facet impact",
      "occurrence_evidence_atom_ids": ["atom:assigned-occurrence"],
      "boundary": {"kind": "open causal/action kind", "statement": "distinct boundary",
        "citations": [{"atom_id": "atom:assigned-occurrence", "field_path": "/text", "exact_value": "exact signed value", "relation": "why it establishes the boundary"}]}
    }], "material_unknowns": []
  },
  "actionability_assessment": {
    "disposition": "requires_change | already_addressed | non_actionable | undetermined",
    "rationale": "evidence-based current actionability conclusion",
    "evidence_refs": ["experiment:verified-current-state-or-artifact:retained-history"]
  },
  "material_unknowns": [{"unknown": "what is unknown", "evidence_needed": "specific evidence", "affects": ["root_cause"], "hypothesis_id": "hypothesis:unique", "material": true}],
  "blocking_reasons": [],
  "evidence_boundaries": []
}
```

- Every artifact ID is unique and every path resolves to an existing regular file, not a directory. Every experiment has nonempty atom and declared-artifact ID lists, an integer
  exit code, and one `supports`, `refutes`, or `inconclusive` outcome. Set `writes_used=true` and
  replace `writes_purpose` when the run creates or changes `.usertest_research/*`.
- An observable assertion uses `source=exit_code|stdout|stderr|combined` and
  `operator=equals|contains|not_contains`. Exit code requires `equals` plus an integer; text
  sources require a nonempty string expected value.
- A paired control uses the exact `scenario_kind="control"`; only that scenario adds
  `control_relationship={supports_experiment_id,controlled_variable,expected_difference,
  mechanism_symbols}`. A `faithful_replay` or `live_runtime` adds
  `fidelity_mapping={original_condition,retained_differences,why_mechanism_equivalent}`;
  `live_runtime` also names a non-`any` platform. Other scenario/platform labels remain open.
- The first hypothesis is `primary`; later dispositions are `refuted|plausible|unresolved`.
  `primary` and `refuted` require disposition evidence. A falsification entry is
  `{attempt_id,hypothesis_id,claim,baseline_experiment_id,challenge_experiment_id,
  disproof_condition,outcome}`: IDs differ, `claim` exactly equals the parent statement,
  `disproof_condition` uses the assertion shape, and outcome is `survived|disproved|inconclusive`.
  The baseline must support the hypothesis. A survived challenge also has `supports` and observes
  the logical opposite of the disproof condition; a disproved challenge has `refutes` and exactly
  observes it; an inconclusive challenge has `inconclusive`.
- A proof adapter contains registered `adapter_id`, existing distinct baseline/challenge IDs,
  `hypothesis_id`, `intervention={kind,target,predicted_polarity,before?,after?}`, and
  `positive_outcome={predicate,semantic_basis}`. This historically named field is the
  source-bound causal predicate and does not require a future solution contract. Controlled replay
  adapters require
  `observations={baseline:{source,...},challenge:{source,...}}`; use the exact selector source names
  listed above (JSON also uses `json_pointer`). State adapters use their adapter-specific state
  input. Put connected inspected production paths under `implementation_touchpoints`; keep the
  intervention locator separate.
  To link that pair to a hypothesis, one hypothesis `mechanism_symbols` value must exactly equal its
  touchpoint `causal_locator` or a touchpoint `symbols` entry. Use `symbols`, not `inspected_symbols`.
  Touchpoints belong under `proof_adapter`, never in invented hypothesis-level link fields.
  Semantic basis shapes are `origin_exact_value={atom_id,field_path}`,
  `repository_fail_first_command={baseline_experiment_id,challenge_experiment_id}`,
  `authenticated_semantic_citation={atom_id,field_path,semantic_rationale,semantic_relation}`, or
  `repository_contract_quote={path,exact_quote,contract_type,...contract locator}`.
- A positive predicate is an object with a top-level discriminator: `{"kind":"equals",
  "expected":...}`, `{"kind":"membership","members":[...]}`, `{"kind":"range",
  "minimum"?:...,"maximum"?:...}`, `{"kind":"schema","schema":{...}}`,
  `{"kind":"existence","expected":true}`, `{"kind":"state_transition","from":...,"to":...}`,
  or `{"kind":"event_sequence","events":[...],"mode"?:"exact"|"ordered_subsequence"}`. It is
  not the experiment assertion shape `{source,operator,expected}`, and is not `{equals:{...}}`.
- Each material unknown has a nonempty string-list `affects`. Missing `material` means material;
  use `material=false` only for a genuinely non-decision-affecting residual observation.
  `evidence_boundaries` is a string list; structured live/equivalence boundaries belong on the
  relevant experiment as `verification_boundary={boundary_kind,requires_live_verification,
  faithful_equivalence,rationale}`.

Evidence references must resolve and directionally support the claim; never cite counterevidence as support.
If the source-bound symptom, current mechanism, or connected change surface cannot be
established, retain the useful evidence and return `insufficient_evidence` or `blocked`. Do not
downgrade otherwise sufficient causal research because a future solution contract is absent.

The runner adds schema version, repository revision, evidence assignment, diff classification, and
verification receipts. Do not emit those runner-owned fields.

## Correction behavior

Correct output structure/reference interpretation in the same author session and same workspace.
When the verifier finds an evidence gap, continue there with research capabilities, run the needed experiment, and return the complete dossier; new or more specific findings are feedback, not cause to erase useful work.
The runner retains the strongest verified result separately. Do not restart at the first defect or merely because error identities changed; cost may escalate only repeated genuine nonprogress.
Restart only for repeated/consecutive genuine nonprogress, lost immutable provenance/session/workspace, an uncorrectable failure, or rework effectively equivalent to a fresh investigation.
