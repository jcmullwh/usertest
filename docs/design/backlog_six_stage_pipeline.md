# Evidence-driven automated backlog pipeline

## Overview

`usertest-backlog reports backlog` turns run evidence into implementation work only when the evidence chain is strong enough to support a concrete change. Stage files remain inspectable, but their presence is not success: canonical case identity, research readiness, plan specificity, and outcome proof are enforced in code.

The pipeline distinguishes four concepts:

- An **atom** is one observed fact extracted from a run.
- A **case** is the durable identity of an underlying problem across runs and wording changes.
- A **plan revision** is one selected implementation approach for a case.
- An **outcome** records what was implemented and which kinds of verification actually occurred.

Generated titles and summaries never determine case identity.

## Pipeline

```text
atoms
  -> lineage normalization and disposition
  -> stage 1: problem mining
  -> canonical case relation review
  -> stage 2: prioritization
  -> stage 3: reproduction and research proof
  -> stage 4: zero to three evidence-supported options
  -> stage 5: selection and falsification review
  -> stage 6: code-grounded implementation planning
  -> readiness policy and ticket assembly
  -> implementation, review, and outcome recording
```

The important control points are before research and before export. Same-cause records are canonicalized before separate dossiers can fan out. A partial or blocked dossier cannot reach optioning. A plan with unresolved discovery work cannot become `ready_for_ticket`.

## Atom lineage and dispositions

New atoms carry `origin_run_id`, `origin_stage`, `evidence_role`, `derived_from_atom_ids`, `parent_case_id`, `case_id`, and `disposition`. Supported dispositions are:

- `supports_case`
- `duplicate`
- `expected_noise`
- `deferred`
- `novel_case`
- `unresolved`

Research, implementation, and verification atoms are derived evidence. Their origin and
parent are established only from runner-owned metadata: `target_ref.json`, `ticket_ref.json`, a
validated top-level evidence assignment, or the persisted case registry. Fields under
model-authored `report.extensions.backlog_lineage` and
`report.extensions.backlog_repro_research` are retained as legacy diagnostic claims, but cannot
relabel evidence, select a parent, or make an atom mining-eligible. If no trusted parent exists,
the atom remains derived `unresolved` and fails closed outside problem mining.

When derived atoms name a trusted parent case they update that case and are not eligible to
originate another case. Promoting derived evidence to a new case requires an explicit
runner-owned classification that passes the lineage validator (for example, a durable atom
disposition decision); model prose and a self-declared `novel_case_rationale` are insufficient.
This promotion is reserved for a distinct failure in the research or implementation
infrastructure.

Updating a case is additive: prior observation evidence is retained, and newly attached
research, implementation, or verification atoms are recorded in the case's derived-evidence
set. A later cycle never replaces the old evidence set with only the newest observation.

Every eligible source observation receives an explicit disposition at every severity. Stage 1
partitions the eligible corpus into bounded chunk-aligned jobs, requires every assigned evidence
chunk to be read in full, and requires exactly one `supports_case`, `duplicate`, `expected_noise`,
`deferred`, or `unresolved` decision per assigned atom. A citation based only on the compact index
preview is rejected. Derived research/implementation evidence is tracked separately and can never
satisfy source-observation coverage. IDEA-originated records remain outside this automated pass.
The agent-readable chunks include the atom's structured output, artifact, impact, and error context,
not only its headline text. Every first pass uses the neutral problem-mining lens, every non-support
decision receives an independent adversarial review, and distinct evidenced problems are not
subject to a numeric record cap. A deferred decision names a concrete revisit condition.

`unresolved` is not, by itself, proof that anyone reviewed the atom. Newly extracted
unparented atoms carry `disposition_status: pending`. A decided disposition carries a
server-owned `disposition_receipt` that hash-binds the atom ID, disposition, case lineage,
decision source, and non-empty rationale. Canonical problem citation, trusted parent lineage,
the persistent case registry, and the durable atom-action ledger are valid decision sources.
The runner persists a `ProblemMiningEvidenceReceipt` that binds the exact eligible corpus, model
response, normalized read events, full chunk or per-atom file hashes, final case memberships, disposition
receipts, and non-empty rationales. Shadow validation re-reads those artifacts and rejects a
missing, partial, preview-only, dry-run, or tampered receipt. It also rejects every source atom at
any severity without a decided receipt. An explicit unresolved or deferred decision is allowed,
but never a default pending value.

A malformed mining job no longer discards successful disjoint jobs. Its assigned atoms receive a
`failed_unresolved` receipt and remain eligible for the next cycle, while successful cases may
continue into research. The cycle receipt is `partial_failed_jobs` and cannot pass shadow/export
until a later verified job replaces every failed assignment.

Dry-run preserves its offline fixture behavior: it records deterministic synthesized decisions
without claiming agent reads and marks the receipt `dry_run_not_exportable`. Shadow mode cannot be
combined with dry-run, so these fixture receipts can never unlock export.

## Stable cases and relation review

Case IDs are persisted in `<target>.case_registry.json` and are minted from evidence identifiers, never from generated prose. The registry maps historical problem IDs, atom IDs, and ticket fingerprints to the canonical case.

Every nonterminal registry case is hydrated into the next cycle even when none of its original
atoms are eligible for mining again. This keeps blocked, mitigated, unverified, and test-only
cases visible without manufacturing a new identity. Terminal cases remain available as
historical comparison targets; genuinely recurring evidence can explicitly reopen the same case.

Relation review runs immediately after problem mining. The reviewer receives the actual problem
statement, canonical symptoms, evidence summary, atom IDs, and a compact index of active and
historical cases. Similarity scores only route attention; they are not sufficient evidence for a
decision. The decisions are operational:

- `merge` combines evidence into the focus case.
- `alias` records that one identity resolves to another.
- `same_cause_group` produces one dossier-generating case while retaining the symptom facets and group ID.
- `split` creates stable child cases only from reviewer-supplied, complete, disjoint evidence
  partitions. Run timestamps are never used as an implicit causal split.
- `keep_separate` preserves both cases with an audit rationale.

The resulting canonical records, not the pre-review candidates, are sent to prioritization and every later stage. Downstream records must carry the expected `case_id`; mismatches fail loudly.

Prioritization ranks canonical cases but does not suppress them. Stage 1 has already separated
expected noise, proposals, duplicates, and unresolved atom evidence, so every canonical problem
receives `selected_for_research: true`; `p0` through `watch` determine research order only. This
prevents a legitimate single-run or lower-impact problem from remaining permanently unresearched.

## Research proof contract

Stage 3 emits research schema version 3. A proof includes:

- the exact repository revision;
- original artifact references;
- experiments with commands and observed results;
- inspected files and symbols;
- structured hypotheses with supporting evidence and counterevidence;
- root-cause confidence;
- material unknowns and the decisions they affect;
- blocking reasons and investigation boundaries;
- the research method and status.

Runner, report, extension, and dossier failures are isolated per case as explicit blocked proofs;
they do not erase or stop unrelated case research. Global repository/configuration failures still
stop the stage because no case can be researched correctly under a broken shared configuration.

`packages/backlog_core/src/backlog_core/stage_contracts.py` validates the structure. `assess_research_readiness()` additionally requires confidence of at least `0.75`, no material unknown affecting root cause, interface, or change surface, and runner-bound mechanism and counterevidence. A dossier may use the `static_trace` research method when the exact deterministic path and symbols are verified; that can advance root-cause optioning, but a ready implementation plan still needs an original, faithful, or correctly platformed live replay for post-change behavioral outcome proof.

Faithful replay has a three-hour safety allowance by default. The allowance is intentionally long;
a replay that reaches it remains `timed_out`/blocked and never advances as partial success. Test and
operator wrappers must allow the replay to finish naturally rather than imposing a shorter timeout.

The allowed research statuses are `evidence_sufficient`, `insufficient_evidence`, and `blocked`. Partial runs, unavailable artifacts, policy blocks, suspicious diffs, runner failures, and malformed reports are blocked or insufficient; they are not converted to a success-like default.

Counterevidence must be a causally linked control, not merely a different passing test. The
runner AST-verifies both exact pytest nodes, requires exactly one explicit argument-slot
difference across their verified mechanism calls, and proves a complementary replay result
(nonzero-to-zero exit or removal of the same failure marker). Model-authored controlled-variable
prose is retained for explanation but is not causal proof. Each content-addressed control mints
one runner-owned failure-path receipt.

Inspected Python symbols include exact imports and assignment/constant bindings. Structured
JSON/TOML/YAML keys use `config:/<RFC-6901-pointer>` (`~0` for `~`, `~1` for `/`, numeric
array indexes); bare dotted config keys are rejected as ambiguous.

The target revision is explicit. `--research-ref` overrides
`backlog_research.source_ref` in `configs/backlog_research.yaml`; a live run without either value
fails closed. For local repositories the ref is resolved to a commit before acquisition, and the
requested ref, resolved commit, runner workspace HEAD, clean planning workspace HEAD, and replay
workspace HEADs are retained in the proof. Each experiment is rerun from an independent clean
checkout and its observable assertion and stdout/stderr hashes are receipted.

Clean replay is an explicit execution boundary, not an implicit host subprocess. The repo default
uses `platform_router`: platform-neutral/Linux evidence runs in the configured Docker image with
networking disabled and no inherited host environment, while an explicitly Windows-only
experiment may use `trusted_host` for a local source repository inside an explicit configured
root. The receipt binds the executed argv to the selected boundary and, for Docker, to actual
sandbox metadata, image identity, network mode, and cleanup confirmation. Absent, invalid, or
platform-mismatched routing blocks research rather than falling back to the host.

A model-authored `.usertest_research/` probe cannot establish a production-code causal link
from its own stdout or stderr. Overlay probes remain useful for exploration and controls, but
causal support must come from a runner-verifiable non-overlay execution path.

After every independently researched case has passed that gate, the pipeline performs a second
same-mechanism review. Consolidation is permitted only when the repository revision and a
runner-verified causal signature agree. That signature binds the verified production mechanism,
its provenance, and the controlled input or deterministic closure that distinguishes the causal
branch; sharing a file or symbol is not sufficient. The canonical dossier embeds every original
member dossier, evidence receipt, and outcome oracle. Each member is revalidated from retained
artifacts before optioning, so consolidation reduces duplicate planning without discarding a
symptom, control, or original-scenario obligation.

Legacy dossiers remain readable only through the explicit legacy parser mode and are never implementation-ready.

## Options, selection, and planning

Stage 4 emits zero to three genuinely distinct mechanisms. `insufficient_evidence` and `no_safe_option` are valid results. Direct, robust, and comprehensive are no longer mandatory slots. Family labels are non-unique: distinct mechanisms may share one family, while a legacy bare-array response is rejected by the live stage contract.

An accepted falsification review may cite only a content-addressed
`control_verification_id` for the selected hypothesis/mechanism. The server derives its
scope-limiting effect, binds the review to the exact option and research receipt, and rejects
arbitrary model-labeled evidence. Unsupported risks must be control-evidence-mitigated or
block selection.

A shared contract or canonical abstraction requires at least two runner-owned failure paths
with distinct independence keys and disjoint originating atoms. Artifact IDs, experiment IDs,
files, symbols, relabeled paths, and repeated wording from one run are not class-level evidence.
Each option records a causal-coverage assessment: mechanism addressed, symptoms covered,
assumptions, residual recurrence paths, compatibility risks, and testability.

Each option is also bound to one verified research hypothesis by exact hypothesis text,
mechanism symbols, supporting evidence, and counterevidence. Its intervention points must
cover those mechanism symbols using exact inspected symbol/path receipts; free-standing
solution prose cannot substitute an unrelated mechanism.

Stage 5 selects only an existing supported option. A separate falsification prompt challenges the diagnosis and selection. Its structured evidence references must bind to verified research IDs, paths, or symbols, and every material assumption, recurrence path, and compatibility risk receives an explicit disposition. It must select exactly one evidence-supported positive outcome contract for every retained research oracle. Contracts the review rejects as surface-only, contradicted, or insufficient are not smuggled into the plan as success criteria. A missing oracle selection, rejected review, blocked risk, or unresolved challenge returns the case to research.

Stages 4 through 6 load prompts, taxonomy, guidance, and repo intent from the orchestrator repository, but inspect code only in the clean retained target revision-view attested by stage 3. Before each stage consumes a dossier, the server re-hashes its claims, origin assignment and atom files, runner and replay artifacts, inspected baseline blobs, and retained clean workspace; a missing or changed receipt blocks progression. The stages never substitute the orchestrator checkout or a mutable source checkout for a missing target workspace.

Stage 6 runs with read-only access to that recorded target revision. A plan must contain:

- `case_id`; the pipeline assigns `plan_revision_id` as a server-owned content hash after parsing;
- exact files, modules, or symbols;
- data-flow and interface changes;
- the selected option's exact scope evidence, with every verified intervention point
  mapped to a plan target; unresearched production-scope expansion is rejected;
- compatibility and failure-mode behavior;
- executable verification commands, each represented as one invocation with an
  unmasked exit code (no chaining, pipes, redirection, command substitution, inline
  interpreter payload, or forced-success shell wrapper);
- role-specific post-merge proof contracts for the original scenario and recurrence, plus a
  live role when required and a mitigation-effect role before `mitigated` can be claimed. These
  roles use runner-evaluated exit/output/artifact predicates; generic test commands cannot be
  relabeled as live, mitigation, or recurrence evidence;
- a before/after mapping bound to every retained verified original/faithful research experiment,
  with the same replay commands and explicit before/after exit expectations. A consolidated case
  receives a signed `multi_scenario` oracle whose child scenarios must all pass at the exact clean
  implementation commit;
- an explicit `requires_live_verification` boolean.

Live proof is inferred from verified evidence of an actual runtime boundary (for example, a
provider invocation, process launch, external delivery, or platform shell interaction), not from
generic words such as "integration", "service", or "network" in otherwise static code prose.

If before/after proof is impossible, the limitation must cite an exact material unknown or evidence boundary from research and provide an executable alternate verification command.

Implementation steps beginning with discovery work such as “locate,” “identify,” or “determine” are rejected. Discovery belongs in research, not in an implementation ticket.

## Readiness

One shared evidence-readiness evaluator is used by assembly, policy, and export. Assembly and
export apply required UX review as a separate routing gate; UX review does not redefine whether the
underlying research, selection, and plan evidence is complete.
`ready_for_ticket` requires all of the following:

1. A canonical case and complete lineage, with no problem or priority parse warning and an explicit priority selection for research.
2. A strict, evidence-sufficient research proof.
3. No decision-critical material unknown.
4. A supported selected option that survives falsification.
5. A decision-complete, code-grounded plan.
6. Before implementation export, any separately required UX review.

Blocked and triage records do not promote their cited atoms to `ticketed`. Only exportable `research_required` and `ready_for_ticket` records do so.

## Outcomes and completion

Queue folder state is not resolution. `backlog_repo.outcomes` validates these distinct outcome states:

- `planned`
- `implemented`
- `tests_verified`
- `original_scenario_verified`
- `live_verified`
- `resolved`
- `mitigated`
- `duplicate`
- `superseded`
- `unverified`
- `integrity_unknown`

An implementation merge records the case, plan revision, target branch, merged commit, PR, and CI
evidence. It becomes `tests_verified` only when the retained implementation-run verifier proves that
configured commands passed; otherwise the merge records `implemented`. Neither state claims the
original failure or live runtime was verified. Runtime cases can be `resolved` only with passing
runner-owned original-scenario and live roles. Non-runtime resolution still requires the original
scenario role. `mitigated` requires bound tests plus a dedicated machine-checked
mitigation-effect role. Every role binds the case, plan revision, merged commit, verification
contract, target-scope contract, and the tested implementation head; timeouts remain blocked.
Recurrence is not a relabeled planner command: the central refresh receipt binds two later stable
shadow-cycle receipts and their canonical-case snapshots to the plan-time case revision and
evidence set, and it must contain an actual source-observation run after the prior outcome. Any
added same-class evidence or reopen marker fails the proof. Stable cycles with no new source window
are recorded only as pipeline stability. A resolved case may honestly record recurrence as
`not_observed/no_new_source_window` with an explicit remaining risk; that is never presented as a
passing recurrence check.

Only `resolved`, `duplicate`, and `superseded` outcomes suppress creation of a new case. Other states
allow recurrence evidence to update or reopen the existing case. When new same-cause evidence is
attached after resolution, the registry persists which resolving plan was invalidated and keeps the
canonical case open until a different plan revision earns a new terminal outcome.

Pending outcome proof is case-local, not a global backlog stop. Refresh retries merged nonterminal
outcomes, preserves their artifacts, and continues mining unrelated source evidence. Export withholds
only a case whose implementation is still awaiting an executable proof. If the original-scenario
predicates actually fail, that failure is durable evidence that the solution did not work and the case
may re-enter research for a revised mechanism instead of being suppressed as merely pending.

Generated-plan cleanup is non-destructive. Duplicate and superseded files move to the archive with an embedded relationship outcome. If a stale legacy plan has no canonical identity, cleanup leaves it in place and reports it as unresolved rather than guessing a replacement. Corrupt historical files are copied byte-for-byte, marked `integrity_unknown`, and never outrank a readable canonical plan.

## Dry-run behavior

`--dry-run` does not invoke agents. Stages 1 and 2 may emit deterministic fixture artifacts. Stage 3 emits a blocked research proof because no reproduction or code inspection occurred. Stages 4 through 6 therefore emit no options, selections, or plans. Dry-run proves orchestration and gating; it does not synthesize implementation readiness.

## Shadow rollout and export gate

`--shadow` runs the complete live pipeline and evaluates the depth invariants without exporting
tickets or updating the atom-action ledger. It cannot be combined with `--dry-run`. The cycle and
its content hashes are appended beside the backlog in `<target>.shadow_state.json`.

Implementation entry points use one centralized refresh transaction under an OS-backed scope lock:
a preliminary shadow, a fresh intent snapshot and UX review, then two fresh stable qualifying
shadows followed immediately by export. The transaction uses one repo/ref/profile/agent/model and
one ledger pair throughout, probes live PR state before every boundary, and fails closed if any PR
is open or the probe cannot establish that none are open.

`configs/backlog_export_gate.yaml` keeps `reports export-tickets` locked until the configured number
of consecutive shadow cycles pass with the same source-observation atom corpus, canonical case
graph, ticket set, complete pipeline source/configuration manifest, and runner-owned research proof
basis. The proof-basis hash covers each implementation-ready stage-3 case's origin evidence
assignment and artifact hashes, repository revision, replay state/output receipts, inspected source,
causal and control links, and actual Docker `image_id`. Mutable image tags, run-local paths,
container names, and generation timestamps are excluded; changing the immutable image ID or any
decision-bearing receipt resets the streak. A Docker receipt without an observed
`sha256:<digest>` image ID cannot qualify even when its configured tag is present. Source atom hashes
include evidence content, severity, and lineage; derived research/implementation/verification atoms
remain auditable but do not reset stability merely because a shadow run created another receipted
investigation with the same verified basis. `require_exact_export_projection` remains a compatible
configuration field, but cross-cycle equality is evaluated over canonical case and plan intent:
source evidence, mechanism/hypothesis binding, exact target paths and symbols, and executable
before/after oracles. Generated prose, fingerprints, and content-addressed plan revision IDs are not
stability signals. In both modes separate byte hashes bind export to the latest backlog and complete
rendered export projection; any later edit locks the gate. A failed shadow cycle resets the
consecutive count.

Shadow state schema 7 and cycle schema 5 are strict. Earlier state is rejected rather than upgraded
or counted toward a qualifying streak; archive the old state and complete the configured number of
new live shadow cycles. This is deliberately a rollout gate,
not a test-only assertion; operators must run the configured number of real agent-backed shadow
cycles before automated export is enabled for a generated backlog.

## Artifact tree

```text
target.problem_records.json
target.prioritized_problems.json
target.research.json
target.solution_options.json
target.solution_selection.json
target.change_plans.json
target.case_registry.json
target.backlog.json
target.backlog_artifacts/
  problem_mining/
  problem_prioritization/
  repro_research/
  solution_optioning/
  solution_selection/
  implementation_planning/
```

Each JSON artifact has a Markdown rendering. Raw prompts, responses, repository context, and relation/falsification reviews remain under the stage artifact tree.

## Offline validation

Run tests from the individual PDM projects:

```powershell
Set-Location packages\backlog_core
pdm run test

Set-Location ..\backlog_miner
pdm run test

Set-Location ..\..\apps\usertest_backlog
pdm run pytest

Set-Location ..\usertest_implement
pdm run pytest
```

The suites are offline. The sanitized benchmark at
`apps/usertest_backlog/tests/fixtures/historical_depth_benchmark.json` records exact retained
problem IDs and evidence IDs for the lifecycle, verification-path, shell, apply-patch, storage,
Claude-stderr, and Python-toolchain cases. Tests replay those contracts without mutating the
retained operational runs. A two-cycle shadow regression additionally verifies that active cases
and their evidence remain stable while exports are disabled.
