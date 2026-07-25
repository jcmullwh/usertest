# `backlog_repo`

`backlog_repo` contains the repository-facing persistence contracts for the automated backlog. It connects run-derived evidence to `.agents/plans`, action ledgers, stable case-aware ticket identities, and durable outcomes.

The package provides:

- loading and writing `backlog_actions.yaml` and `backlog_atom_actions.yaml`;
- indexing `.agents/plans` without treating a folder as proof of resolution;
- stable fingerprints based on canonical case and plan revision;
- validated outcome records that separate implementation, tests, original-scenario proof, and live proof;
- non-destructive duplicate and supersession archival.

## Install

Distribution name: `backlog_repo`

### Standalone package checkout (recommended first path)

From this package directory:

```powershell
pdm install
pdm run smoke
pdm run test
pdm run lint
```

### Monorepo contributor workflow

Run from the monorepo root:

```powershell
python tools/scaffold/scaffold.py run install --project backlog_repo
python tools/scaffold/scaffold.py run test --project backlog_repo
python tools/scaffold/scaffold.py run lint --project backlog_repo
```

## Public API

Action-ledger helpers:

- `load_backlog_actions_yaml(...)`
- `load_atom_actions_yaml(...)`
- `write_atom_actions_yaml(...)`
- `normalize_atom_status(...)`
- `promote_atom_status(...)`

Plan indexing and archival:

- `scan_plan_ticket_index(...)`
- `sync_atom_actions_from_plan_folders(...)`
- `archive_plan_ticket_file(...)`

Case-aware export identity:

- `ticket_export_case_id(...)`
- `ticket_export_plan_revision_id(...)`
- `ticket_export_fingerprint(...)`
- `ticket_export_anchors(...)`

Ticket and verification provenance:

- `canonical_plan_sha256(...)`
- `canonical_ticket_body_sha256(...)`
- `render_verification_contract_markdown(...)`
- `parse_verification_contract_markdown(...)`
- `verification_commands_sha256(...)`

Outcome contracts:

- `validate_outcome_record(...)`
- `upsert_outcome_markdown(...)`
- `extract_outcome_markdown(...)`
- `outcome_suppresses_new_case_discovery(...)`
- `transition_outcome_record(...)`
- `verify_outcome_record_provenance(...)`

Stage-6 target intent is content-addressed with the canonical case, selected option,
researched repository revision, and planned paths/symbols/interventions. Implementation
scope deliberately has a narrow machine gate: every planned production path must be
touched and the reviewed head must be the exact head covered by runner verification.
Extra support/test paths and wider changes are retained as review advisories rather than
rejected by brittle line-, import-, config-, or new-file heuristics.
- `write_case_relation_receipt(...)`
- `validate_case_relation_receipt(...)`

Legacy tickets without a case ID continue to use the historical text/anchor fingerprint for read compatibility. Newly staged tickets use `case_id` plus `plan_revision_id`, so generated wording and owner changes do not create a new identity.

## Outcome semantics

An outcome is not inferred from a plan folder. Structural `OutcomeRecord` validation makes an artifact safe to retain and render; it is not completion proof. Staged case sync and shadow validation reopen the referenced plan, review, implementation, verification, and Git artifacts beneath configured trusted roots before an implementation or resolution state can advance a case. A failed provenance check leaves the case nonterminal as `unverified`.

`tests_verified` requires a schema-v2 receipt that binds exact runner command coverage to the canonical ticket body, local plan content, case/plan revision, and hashed verification contract. It does not mean the original failure was replayed or that a runtime problem was observed working live. The durable role contract records every retained research experiment and the exact stage-5-selected positive contract for each outcome oracle. Consolidated cases use a signed `multi_scenario` oracle and cannot advance original-scenario proof unless every child replay passes. Runtime cases may be `resolved` only with passing original-scenario and live evidence from the dedicated workflows that establish those evidence roles.

Duplicate and superseded plan copies are archived with an embedded machine-readable outcome that points to the related plan; those archival records never close the case. A case-scoped duplicate or supersession additionally requires a hashed runner-owned relation receipt, exact source-to-target direction in the case registry, and an acyclic canonical target. Merely naming another existing case is not evidence. If no canonical relationship is known, cleanup preserves the ticket rather than guessing.
