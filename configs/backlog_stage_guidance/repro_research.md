# Stage 3 guidance: evidence-gated reproduce and research

## Goal

Establish the failure mechanism and its boundaries at an exact repository revision. This
stage produces a research proof, not a proposed fix. A written report is not evidence by
itself.

## Workspace contract

The mission runs in an isolated writable workspace. Research-only files must stay under
`.usertest_research/`; modifying tracked source, existing tests/config/scripts/tools, or
adding files elsewhere makes the proof suspicious and non-advancing. Do not change
production behavior, implement a solution, add user-visible surface, or write documentation
as if a change shipped. `implementation_performed` must be false.

Do not invent sleeps, polling, retries, or timeout values. Use a value already configured
in the repository or document the limitation. Record the exact Git revision inspected.

## Evidence methods

- `reproduction`: execute the original or faithful scenario and capture command/result
  evidence. `evidence_sufficient` requires `reproduction_status="reproduced"`.
- `static_trace`: use when the original runtime cannot execute but a retained input or
  artifact can be evaluated through an exact deterministic code/config path. It may
  advance with a complete static-trace contract, no environment dependencies, and either
  a runner-derived inspected call chain, a data-linked retained harness, or an exact
  deterministic config-pointer trace.

Record every experiment with its exact command, result, outcome (`supports`, `refutes`,
or `inconclusive`), integer exit code, and artifact IDs. Give every artifact a unique
`artifact_id`. Record inspected files and symbols. The first root-cause hypothesis is the
implementation-driving mechanism and must cite an observed supporting experiment plus
one honest causal proof route. Prefer an explicit falsification attempt when a credible
counterfactual exists: it copies the exact hypothesis ID and claim, names a distinct
supporting baseline and challenge experiment, states a machine-checkable disproof condition,
and reports `survived`, `disproved`, or `inconclusive`. The runner binds its exact
command/result, one changed causal input, and observed hashes. When the mechanism is a fully
deterministic static/config path and no honest counterfactual exists, leave
`falsification_attempts` empty. The runner may mint a deterministic-closure receipt only when
the exact symbol/path chain is complete, every evidence-backed alternative is refuted, and no
material root-cause unknown remains. Never invent an alternative or challenge to satisfy a
count. Genuine
counterevidence remains valuable and may be empty; an unrelated `refutes` label is not a
falsification attempt. Investigate credible alternatives, but do not invent one merely to
fill a count. Every actual alternative needs an explicit disposition; a plausible unresolved
alternative is a material unknown. Free-form prose is not evidence.

Inspected Python symbols may name functions/classes, exact import bindings, or exact
assignment/constant bindings. Structured JSON, TOML, and YAML keys use only
`config:/<RFC-6901-pointer>` (for example `config:/tool/pytest/ini_options/addopts`),
with `~0` for `~`, `~1` for `/`, and numeric segments for array indexes. Bare dotted
config names are ambiguous and do not verify.

Each experiment also records `scenario_kind`, `addresses_atom_ids`, and an
`observable_assertion`. `scenario_kind` is exactly `original_replay`, `faithful_replay`,
`control`, `static_trace`, or `live_runtime`; `platform_requirement` is exactly lowercase
`any`, `windows`, `linux`, or `darwin`. Experiments collectively cover every assigned atom.
A supporting experiment uses `original_replay`, `faithful_replay`, deterministic
`static_trace`, or a platform-bound `live_runtime`; a refuting experiment may use a distinct
`control`. Every static trace includes
`{"deterministic":true,"environment_dependencies":[],"code_path":[{"path":"...","symbol":"...","observation":"..."}]}`
under its `static_trace` key. The runner
repeats allowlisted tests, safe retained harnesses, and evidence-bound practical repository
CLIs/scripts in independent clean clones,
captures stdout/stderr hashes, and verifies the assertion. Agent-workspace results alone are
not proof. Each hypothesis must name exact inspected mechanism symbols and link its supporting
experiment to the inspected source artifact. A causal challenge must address the same source
atoms as its baseline, share typed evidence for the selected mechanism, use a distinct command,
and record the observable condition that would disprove the claim before interpreting the
result. Use `disposition="primary"` on the first
hypothesis. Do not invent a second hypothesis merely to fill the schema. Mark every
alternative that the evidence actually makes plausible `refuted`, `plausible`, or
`unresolved`; refuted alternatives cite an actual `outcome="refutes"` experiment, while plausible/unresolved
alternatives are material unknowns keyed by `hypothesis_id`.

A refuting control is causal evidence only when `control_relationship` names the supporting
experiment, copies the hypothesis mechanism symbols, addresses the same origin atoms, and
cites the same inspected mechanism-source artifact. Record the controlled variable and the
observable difference predicted by the hypothesis. An unrelated passing test is not a control.
Controls change one declared causal condition: an input/fixture, configuration, environment,
platform, filesystem state, completion marker, or explicit call argument. The runner requires
a complementary observable result, but does not require every real failure to fit one Python
AST argument delta. Exact pytest/AST controls remain a strong optional evidence mode.

Use explicit origin bindings when an experiment addresses corroborating/context atoms or a
short exact symptom. Bind each atom to its immutable `$.field[index]` path and exact JSON value;
the runner records the source-atom and value hashes. At least one binding must directly carry the
observed symptom or exact originating command. A causal control can distinguish mechanisms, but
its output is not automatically the correct output for the original input. Do not transpose a
control value into the success contract without a separately verified equivalence invariant.

Research must also establish what post-change success means. There are three usable routes:

1. An exact repository pytest node already fails at a semantic assertion whose value is
   data-dependent on the inspected mechanism. The runner binds that assertion and its relevant
   helper/import closure; an unrelated green test is not positive proof.
2. An assigned source-observation atom has an exact structured expected-behavior field
   (`expected_*`, `desired_*`, `correct_*`, `intended_*`, `required_*`,
   `expected_behavior`, or `success_criteria`). Declare `contract_kind="origin_atom_exact_value"`
   and add the matching `origin_evidence_bindings` entry with `role="expected_behavior"`.
3. For the normal novel-bug case, write a fail-first Python harness under
   `.usertest_research/` whose `assert` compares a value or typed property produced by the
   inspected mechanism with an explicit JSON scalar. The clean baseline must fail at that exact
   assertion. Declare this experiment contract:

   ```json
   {
     "contract_kind": "retained_harness_semantic_assertion",
     "expected_value": true,
     "semantic_relation": "required_operational_property",
     "semantic_rationale": "The source failure says the materialized path is unreadable; this assertion exercises that same path and requires it to be readable.",
     "semantic_basis": {
       "kind": "source_atom_quote",
       "atom_id": "exact assigned source atom ID",
       "field_path": "$.text",
       "exact_quote": "exact source passage that establishes the failure or required behavior"
     },
     "adversarial_review_reference": "exact falsification attempt_id when a bound attempt targets this experiment"
   }
   ```

   `semantic_relation` is one of `exact_expected_value`,
   `logical_correction_of_source_failure`, `required_operational_property`, or
   `repository_contract_requirement`. Instead of `source_atom_quote`, a researched repository
   contract may use:

   ```json
   {
     "kind": "repository_contract_quote",
     "contract_type": "api_contract",
     "path": "exact/inspected/repository/path.py",
     "symbol": "exact inspected mechanism symbol for api_contract",
     "json_pointer": "/exact/schema/value for schema",
     "contract_subject": "exact mechanism subject named in a documentation quote",
     "exact_quote": "exact inspected API, documentation, or schema passage"
   }
   ```

   `contract_type` is `api_contract`, `documentation`, or `schema`, and the file must appear in
   `inspected_files` at the researched revision. Supply only the locator appropriate to the
   type: `symbol` for `api_contract`, `json_pointer` for `schema`, or `contract_subject` for
   `documentation`. The symbol must be inspected and mechanism-bound; the pointer value and
   documentation subject are content-addressed. The quote proves provenance, not semantic
   sufficiency. Stage 5 independently reviews whether the assertion is the logical correction,
   covers the full source problem, and proves intended operation. It must reject a swallowed
   exception, renamed marker, classifier-only mitigation, or invented success value.
   `adversarial_review_reference` is required when a runner-bound falsification attempt targets
   this experiment and must equal that attempt's exact `attempt_id`; it may be omitted only when
   no relevant intervention exists, such as a deterministic-closure proof route.

Harnesses under `.usertest_research/` may establish faithful or static evidence when the
runner retains and hashes them, executes safe argv at clean HEAD, and verifies their asserted
output/exception/artifact is data-dependent on every callable production mechanism symbol.
A call-and-discard harness followed by a hard-coded print cannot advance. For a positive
contract, the exact mechanism-dependent scalar assertion must fail before the change and the
retained, content-addressed harness must replay unchanged after it. State all differences from
the original scenario in `fidelity_mapping`. It is required for `faithful_replay` and
`live_runtime`, optional but encouraged for `static_trace`, and omitted for
`original_replay` and `control`. Runtime evidence must name its required platform enum;
Docker/Linux evidence cannot prove a Windows-only failure.

The runner emits typed `mechanism_evidence`: `exception_trace`, `observed_output`,
`controlled_scenario`, `temporary_harness`, `static_trace`, or `live_runtime`. A wrong value,
missing artifact, or bad classification is valid observed evidence even when production code
does not throw and therefore has no traceback, but `observed_output` still needs a
runner-derived production call chain plus a complementary causal control. Reading a nearby
symbol and replaying the symptom is insufficient.

The assigned source atoms are the immutable problem evidence. Derived research,
implementation, and verification atoms are context for hypotheses and recurrence checks;
they are not mandatory symptoms to reproduce and cannot bootstrap a new problem case.

## Status gate

- `evidence_sufficient`: confidence is at least `0.75`,
  artifact and experiment evidence is present, exact files and symbols were inspected,
  and no material unknown affects root cause, interface, or change surface.
- `insufficient_evidence`: investigation ran but evidence cannot support an implementation
  decision. State material unknowns and the evidence needed.
- `blocked`: required artifacts, workspace capability, policy, or execution access was
  unavailable. State blocking reasons.

Partial reproduction, suspicious implementation diffs, runner failures, missing artifacts,
or malformed reports cannot be `evidence_sufficient`.

The outer troubleshoot report `status` is not the research conclusion. `partial` and `failure`
preserve an honest `insufficient_evidence` or `blocked` extension; only a contradictory
`evidence_sufficient` claim is rejected when the outer report is not `success`. Runner exit,
schema, and evidence receipts remain authoritative for execution integrity.

A missing/malformed report, missing extension, or genuine model JSON-schema error receives at
most one complete retry in a distinct workspace at the same pinned revision. The retry rereads
the full assignment and reruns claimed experiments; it is not a JSON-repair pass. Evidence
verification failures, suspicious implementation diffs, nonzero execution, and implementation
violations do not consume that retry. Never strengthen an evidence status merely to pass shape.

## Strict output

The model-authored `extensions.backlog_repro_research` object must include: `case_id`,
`problem_id`, `research_method`, `reproduction_status`,
`research_status`, `writes_used`, `writes_purpose`, `implementation_performed`,
`root_cause_hypotheses`, `root_cause_confidence`, `broader_class_assessment`,
`material_unknowns`, `artifact_refs`, `experiments`, `inspected_files`,
`inspected_symbols`, `blocking_reasons`, and `evidence_boundaries`. Copy the assigned
`case_id` and `problem_id` exactly. The runner supplies schema version 3, the acquired repository
revision, the final diff classification, a clean revision-pinned planning workspace, and an
`evidence_verification` receipt. Readiness requires that receipt to bind commands to
normalized events, files and symbols to the acquired revision, and artifacts to hashes.
