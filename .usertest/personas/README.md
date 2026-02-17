# Repo-local personas

These personas are designed to create **distinct evaluation styles** for running usertest missions against this repo.

They are **not** deterministic test fixtures. They intentionally emphasize different user needs:

- first-time onboarding and adoption decisions
- CLI discoverability and error-message quality
- operations/CI repeatability
- security/compliance boundaries
- maintainer and contributor ergonomics
- containerization and isolation
- report readability for stakeholders

## Personas included

- `repo_adoption_gatekeeper` — decide quickly whether to adopt/pilot the repo.
- `repo_cli_wayfinder` — judge the CLI as the primary UI; discoverability and errors.
- `repo_ci_pipeline_operator` — automation/CI usage; repeatable non-interactive runs.
- `repo_qa_mission_designer` — evaluate mission design quality and comparability.
- `repo_security_compliance_reviewer` — boundaries, auditability, safe defaults.
- `repo_doc_first_diagnostician` — docs correctness and "copy/paste" paths.
- `repo_release_surgeon` — maintainer workflow; small low-risk changes.
- `repo_external_contributor` — first PR experience; setup and conventions.
- `repo_container_platform_engineer` — docker backend and container ergonomics.
- `repo_report_consumer` — report readability; minimal setup expectations.

## Suggested usage

Pair personas with missions to cover different risk areas:

- Onboarding: `repo_adoption_gatekeeper`, `repo_doc_first_diagnostician`
- CLI IA: `repo_cli_wayfinder`
- CI/ops: `repo_ci_pipeline_operator`
- Security: `repo_security_compliance_reviewer`
- Docker: `repo_container_platform_engineer`
- Maintainer/DX: `repo_release_surgeon`, `repo_external_contributor`
- Reporting: `repo_report_consumer`
