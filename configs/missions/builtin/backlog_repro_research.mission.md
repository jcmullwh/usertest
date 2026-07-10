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

## Goal

Reproduce and research a specific backlog problem **in an isolated writable workspace**.

The goal is **evidence and a bounded root cause**.

The goal is **NOT** to implement the fix, and **NOT** to judge success by a clean diff.

## Contract (repeat: do not implement)

Hard rules:

- Your primary output is a reproduction or a bounded investigation, not a fix.
- You may write files only under `.usertest_research/`, and only to support reproduction and
  investigation. Examples include isolated failing-test harnesses, temporary instrumentation,
  repro scripts, and copied fixture/setup material. Do not modify tracked source, existing tests,
  configs, scripts, tools, or add files elsewhere in the checkout.
- Do **not**:
  - implement the solution / fix the bug
  - change production behavior to make the symptom disappear
  - introduce new user-visible features or commands
  - perform broad refactors unrelated to the reproduced path
  - write documentation as if the change shipped

If you accidentally made implementation-like changes, you must **admit it** and treat it as suspicious.

## Required extension block (must be present)

In your final JSON report, include this required extension block:

```json
{
  "extensions": {
    "backlog_repro_research": {
      "research_schema_version": 3,
      "case_id": "case:... exactly as assigned",
      "problem_id": "problem:...",
      "repo_revision": "exact git commit inspected",
      "research_method": "reproduction|static_trace",
      "reproduction_status": "reproduced|reproduction_failed|partial|blocked",
      "research_status": "evidence_sufficient|insufficient_evidence|blocked",
      "writes_used": false,
      "writes_purpose": ["none"],
      "implementation_performed": false,
      "diff_classification": "no_changes",
      "artifact_refs": [
        {
          "artifact_id": "artifact:repro-output",
          "kind": "report|log|test|trace|fixture",
          "path": "...",
          "description": "..."
        },
        {
          "artifact_id": "artifact:mechanism-source",
          "kind": "source",
          "path": "exact/repo/path.py",
          "description": "Source file containing the diagnosed mechanism"
        }
      ],
      "experiments": [
        {
          "experiment_id": "exp-support",
          "scenario_kind": "faithful_replay",
          "platform_requirement": "any",
          "fidelity_mapping": {
            "original_condition": "required for faithful_replay/live_runtime",
            "retained_differences": "what differs from the originating scenario",
            "why_mechanism_equivalent": "why those differences do not change the mechanism"
          },
          "mechanism_link": {
            "kind": "entrypoint_dataflow",
            "entrypoint": "module.entrypoint",
            "code_path": [
              {
                "path": "exact/repo/entrypoint.py",
                "symbol": "module.entrypoint",
                "observation": "This inspected function directly calls the mechanism below."
              },
              {
                "path": "exact/repo/path.py",
                "symbol": "module.SymbolOrFunction",
                "observation": "This inspected mechanism produces the asserted value."
              }
            ]
          },
          "addresses_atom_ids": ["exact assigned atom ID"],
          "origin_evidence_bindings": [
            {
              "atom_id": "exact assigned atom ID",
              "role": "symptom|corroborating|context|command|expected_behavior",
              "field_path": "$.output_excerpt",
              "value": "exact immutable value at that field path",
              "value_sha256": "optional precomputed sha256 of canonical compact JSON"
            }
          ],
          "positive_outcome_contract": {
            "contract_kind": "origin_atom_exact_value",
            "atom_id": "only when this assigned atom has an explicit expected_* / desired_* / correct_* / intended_* / required_* field",
            "field_path": "$.expected_output",
            "postcondition": {
              "type": "command_stdout_equals|command_stdout_contains|command_stderr_equals|command_stderr_contains|command_combined_equals|command_combined_contains|artifact_json_value|config_state_equals",
              "value": "for command stream contracts: the exact immutable expected value"
            }
          },
          "command": "exact command",
          "result": "observable result",
          "outcome": "supports|refutes|inconclusive",
          "exit_code": 1,
          "observable_assertion": {
            "source": "exit_code|stdout|stderr|combined",
            "operator": "equals|contains|not_contains",
            "expected": "observable text, or an integer for exit_code"
          },
          "artifact_refs": ["artifact:repro-output", "artifact:mechanism-source"]
        },
        {
          "experiment_id": "exp-causal-challenge",
          "scenario_kind": "control",
          "control_relationship": {
            "supports_experiment_id": "exp-support",
            "mechanism_symbols": ["module.SymbolOrFunction"],
            "controlled_variable": "the strongest credible alternative cause changed while the selected mechanism is held fixed",
            "expected_difference": "the failure disappears only if that alternative, rather than the selected mechanism, is causal"
          },
          "addresses_atom_ids": ["exact assigned atom ID"],
          "command": "distinct causal challenge command",
          "result": "the original failure remains after changing the credible alternative",
          "outcome": "supports",
          "exit_code": 1,
          "observable_assertion": {
            "source": "exit_code",
            "operator": "equals",
            "expected": 1
          },
          "artifact_refs": ["artifact:repro-output", "artifact:mechanism-source"]
        },
        {
          "experiment_id": "exp-alternative-refute",
          "scenario_kind": "original_replay",
          "platform_requirement": "any",
          "addresses_atom_ids": ["exact assigned atom ID"],
          "command": "exact command that distinguishes the alternative",
          "result": "observable result inconsistent with the alternative",
          "outcome": "refutes",
          "exit_code": 0,
          "observable_assertion": {
            "source": "stdout",
            "operator": "contains",
            "expected": "retained distinguishing observation"
          },
          "artifact_refs": ["artifact:repro-output", "artifact:mechanism-source"]
        }
      ],
      "inspected_files": ["exact/repo/entrypoint.py", "exact/repo/path.py"],
      "inspected_symbols": [
        "module.entrypoint",
        "module.SymbolOrFunction",
        "module.AlternativeSymbol"
      ],
      "root_cause_hypotheses": [
        {
          "hypothesis_id": "h1",
          "statement": "...",
          "supporting_evidence": ["exp-support", "exp-causal-challenge"],
          "counterevidence": [],
          "falsification_attempts": [
            {
              "attempt_id": "falsify-h1-alternative-cause",
              "hypothesis_id": "h1",
              "claim": "copy the hypothesis statement exactly",
              "baseline_experiment_id": "exp-support",
              "challenge_experiment_id": "exp-causal-challenge",
              "disproof_condition": {
                "source": "exit_code",
                "operator": "equals",
                "expected": 0
              },
              "outcome": "survived"
            }
          ],
          "mechanism_symbols": ["module.SymbolOrFunction"],
          "disposition": "primary",
          "disposition_evidence": ["exp-support", "exp-causal-challenge"]
        },
        {
          "hypothesis_id": "h2",
          "statement": "the strongest plausible alternative cause",
          "supporting_evidence": ["artifact:repro-output"],
          "counterevidence": ["exp-alternative-refute"],
          "falsification_attempts": [
            {
              "attempt_id": "falsify-h2-retained-input",
              "hypothesis_id": "h2",
              "claim": "the strongest plausible alternative cause",
              "baseline_experiment_id": "exp-support",
              "challenge_experiment_id": "exp-alternative-refute",
              "disproof_condition": {
                "source": "stdout",
                "operator": "contains",
                "expected": "retained distinguishing observation"
              },
              "outcome": "disproved"
            }
          ],
          "mechanism_symbols": ["module.AlternativeSymbol"],
          "disposition": "refuted",
          "disposition_evidence": ["exp-alternative-refute"]
        }
      ],
      "root_cause_confidence": 0.0,
      "broader_class_assessment": "isolated_instance|repeated_variant|unknown",
      "material_unknowns": [
        {
          "hypothesis_id": "h2 when this unknown concerns an unresolved alternative",
          "unknown": "...",
          "affects": ["root_cause|interface|change_surface|scope|verification"],
          "evidence_needed": "..."
        }
      ],
      "blocking_reasons": [],
      "evidence_boundaries": ["..."]
    }
  }
}
```

Notes:
- `implementation_performed` must be `false` even if you made writes. This stage is research-only.
- `writes_used` must match the observed research overlay. Keep it `false` with
  `writes_purpose=["none"]` when no `.usertest_research/` files were created. Set it
  `true` only when research-only overlay writes actually exist, and then replace `"none"`
  with the honest applicable purposes (`failing_test`, `temporary_instrumentation`,
  `repro_harness`, or `fixture_change`).
- Every hypothesis must name exact inspected `mechanism_symbols`. The first hypothesis uses
  `disposition="primary"`, cites a supporting original/faithful/static/live experiment, and
  uses one honest causal proof route. Prefer an explicit falsification attempt whenever a
  credible counterfactual exists. Each attempt copies the exact hypothesis ID and statement,
  names a distinct baseline and challenge, changes one runner-verifiable causal input, states
  a machine-checkable disproof condition, and reports only `survived`, `disproved`, or
  `inconclusive`. When a fully deterministic static/config path has no honest counterfactual,
  leave `falsification_attempts` empty. The runner may mint deterministic closure only for a
  complete exact symbol/path chain, after all evidence-backed alternatives are refuted and no
  material root-cause unknown remains. Never invent a challenge or alternative to satisfy the
  schema. The runner binds commands, results, assertions, exit codes, and stream hashes; prose
  alone cannot satisfy this contract. `counterevidence`
  remains a list of genuine observations against the hypothesis and may honestly be empty.
  Do not invent an alternative to satisfy the shape. Every
  alternative that is genuinely plausible from the evidence is explicitly `refuted`,
  `plausible`, or `unresolved`. A refuted alternative cites a runner-replayed `outcome="refutes"` experiment;
  plausible/unresolved alternatives become material unknowns keyed by `hypothesis_id`.
- `evidence_sufficient` requires evidence for the exact mechanism and no material unknown
  affecting root cause, interface, or change surface. Do not use it for a partial or
  policy-blocked investigation.
- Every artifact needs a unique `artifact_id`, and experiment artifact references must use
  those exact IDs. Alternatives may use retained artifact evidence; prose is not evidence.
- Commands and exit codes must exactly match commands you actually ran. Every inspected file
  must be read during the run, and every inspected symbol must exist in a cited file.
  Python symbols may be exact function/class, import-binding, or assignment/constant names.
  JSON/TOML/YAML keys use only `config:/<RFC-6901-pointer>` (`~0` escapes `~`, `~1`
  escapes `/`, and numeric segments index arrays); never use a bare dotted config key.
- A practical repository CLI or script is valid evidence. Prefer replaying the originating
  command exactly. The runner permits it only when its shell-free argv exactly matches the
  immutable `$.command` of an addressed source atom, or when the argv resolves to a
  repository-owned entrypoint that you actually inspected. It still runs in a clean isolated,
  platform-routed checkout with a sanitized environment; shell syntax, `python -c`, absolute
  paths, traversal, URLs, and commands that resolve only through host `PATH` are rejected.
- The runner adds `evidence_verification` and a clean revision-pinned planning workspace after
  checking normalized events, file contents, artifact hashes, problem identity, and git HEAD.
  Do not invent or include that runner-owned receipt yourself.
- Each experiment must name every assigned **source** atom it addresses. Prior research,
  implementation, and verification atoms are supplied as history/counterevidence context;
  do not reproduce that derived commentary as if it were the original symptom. Together the
  supporting experiments cover the complete source assignment. A control must name its
  supporting experiment, copy the hypothesis mechanism symbols, address the same atoms, cite
  the same inspected mechanism-source artifact, and state the one controlled variable and
  predicted difference. A falsification challenge must use a different command, address the
  same source atoms, share inspected mechanism evidence with its baseline, and state in advance
  the observable condition that would disprove the selected claim. An unrelated green test or
  an unrelated `outcome="refutes"` experiment is neither counterevidence nor falsification. The controlled
  condition may be an input/fixture, config, environment, platform, filesystem state,
  completion marker, or explicit call argument. The runner requires complementary observable
  results. Exact baseline pytest/AST deltas are a strong optional proof, not the only proof.
- Use `origin_evidence_bindings` when atoms play different evidentiary roles or the relevant
  value is short (for example `"bad"`, `false`, or `3`). Each binding names the exact assigned
  atom, restricted `$.field[index]` path, and exact JSON value. You may supply `value_sha256`
  as a precommitment; if present it must be the SHA-256 of the value's canonical compact JSON.
  The runner always computes and retains the hash in its signed receipt, so omitting this
  optional convenience field does not weaken the proof. At least one binding must directly bind the observed symptom or
  exact originating command; other assigned atoms may be honestly marked `corroborating` or
  `context` rather than forced to contain the same long symptom string.
- A resolved plan needs a runner-minted positive outcome contract. An existing repository
  pytest can supply it only when the clean baseline fails at an exact semantic assertion whose
  value is data-dependent on the inspected mechanism; an unrelated green mechanism-touching
  test is not proof. An assigned source-observation atom can supply
  `contract_kind="origin_atom_exact_value"` only from an exact structured expected-behavior
  field, with a matching `origin_evidence_bindings` entry using `role="expected_behavior"`.
  Derived research/implementation/verification atoms and suggested-change/impact prose cannot
  author success semantics.
- For a novel bug without either route, create a fail-first Python harness under
  `.usertest_research/`. Its semantic `assert` must compare a mechanism-dependent result or
  typed operational property with an explicit JSON scalar, and the clean baseline must fail at
  that exact assertion. Declare:

  ```json
  {
    "contract_kind": "retained_harness_semantic_assertion",
    "expected_value": true,
    "semantic_relation": "required_operational_property",
    "semantic_rationale": "Explain why this exact assertion corrects the source problem rather than merely hiding its marker.",
    "semantic_basis": {
      "kind": "source_atom_quote",
      "atom_id": "exact assigned source atom ID",
      "field_path": "$.text",
      "exact_quote": "exact source passage that establishes the failure or required behavior"
    },
    "adversarial_review_reference": "exact falsification attempt_id when a bound attempt targets this experiment"
  }
  ```

  `semantic_relation` is `exact_expected_value`, `logical_correction_of_source_failure`,
  `required_operational_property`, or `repository_contract_requirement`. A researched API,
  documentation, or schema contract may replace the source basis with
  `{"kind":"repository_contract_quote","contract_type":"api_contract|documentation|schema",`
  `"path":"exact inspected path","symbol":"api_contract only",`
  `"json_pointer":"/schema/pointer only","contract_subject":"documentation only",`
  `"exact_quote":"exact inspected contract passage"}`. Supply only the locator for the chosen
  contract type. The API symbol must be an inspected mechanism symbol; the schema pointer value
  and documentation subject are content-addressed. That path must appear in `inspected_files`
  at the researched revision. `adversarial_review_reference` is required when a runner-bound
  falsification attempt targets the experiment and must equal its exact `attempt_id`; omit it
  only when there is no relevant intervention, such as deterministic closure. The runner proves the
  quote, file, assertion, baseline failure, mechanism dependency, and retained-harness hashes;
  it does not pretend to prove natural-language meaning. Stage 5 must independently decide
  whether the selected assertion covers the full source problem and proves intended operation.
  A swallowed exception, renamed marker, classifier-only mitigation, or invented scalar is not
  sufficient.
- A retained Python harness may advance only when its asserted output, exception, exit, or
  artifact is data-dependent on the inspected production call; calling and discarding the
  result before printing a hard-coded symptom cannot advance. A causal control can distinguish
  mechanisms, but its different input's value is not automatically the correct value for the
  original input and cannot independently mint a positive contract.
  Non-throwing wrong output is valid without a traceback when a runner-derived inspected
  entrypoint-to-mechanism call chain and a real causal challenge bind the output to the cause.
  Removing a marker is weaker and must not substitute for proving the corrected behavior.
  Deterministic static trace requires an empty `environment_dependencies` list and an exact
  code/config path; config pointers can advance without a Python harness. `live_runtime` must
  name its platform; the configured router uses Linux Docker by default and an explicitly
  approved host route for a matching platform such as Windows.
- Research writes belong only under `.usertest_research/`. Modifying tracked source, existing
  tests/config/scripts/tools, or adding files elsewhere makes the proof suspicious. A clean
  baseline replay, not a temporary instrumented workspace state, decides readiness.

## How to fill the troubleshoot report fields

Your report must validate against `troubleshoot_v1`:

- `goal`: restate the backlog problem you were assigned (include the problem_id).
- `failure_point`: describe where the failure manifests (command/test + error).
- `evidence.what_happened`: the reproduced behavior or the bounded observation.
- `attempted_fixes`: list what you tried to reproduce/bound the issue (not implementations).
- `recommended_fix_path`: list **next research actions** or a narrow fix direction, without implementing it.
