# Documentation

This repository is a **monorepo** containing:

- **Apps**: end-user CLIs (`usertest`, `usertest-backlog`).
- **Packages**: reusable Python libraries that can be consumed outside this repo.
- **Tools**: repo-maintenance utilities (scaffolding, publishing, migrations, lint helpers).

The docs are organized in **Diátaxis style**:

- **Tutorials**: learn the system by doing.
- **How-to guides**: solve a specific problem.
- **Reference**: authoritative “what is it / what are the flags / what is the contract”.
- **Explanation**: architecture, rationale, design decisions.

If you are new, start with **Tutorials**.

> Note on “ops” docs
>
> Operational/runbook-style docs sometimes live under `.agents/ops/` for local, repo-specific notes.
> That folder is treated as local-only (git-ignored). Committed operator guidance lives under
> `docs/ops/`.

---

## Tutorials

- **Getting started (what this repo is + first run)**
  - `docs/tutorials/getting-started.md`
- **Working in this monorepo (scaffold vs pip)**
  - `docs/tutorials/monorepo-setup.md`

---

## How-to guides

- **Run a usertest against a repo** (single target, batch, docker, pip targets)
  - `docs/how-to/run-usertest.md`
- **Write personas and missions for a specific repo**
  - `docs/how-to/personas-and-missions.md`
- **Publish snapshot packages to the private registry**
  - `docs/how-to/publish-snapshots.md`
- **Use the scaffold tool to add projects and run tasks**
  - `docs/how-to/scaffold.md`

---

## Reference

- **CLI reference**
  - `docs/reference/cli.md`
- **Configuration reference** (catalog, policies, agents, templates, schemas)
  - `docs/reference/configuration.md`
- **Glossary**
  - `docs/reference/glossary.md`
- **Run directory artifact contract**
  - `docs/design/run-artifacts.md`
- **Normalized events contract**
  - `docs/design/event-model.md`
- **Report schemas**
  - `docs/design/report-schema.md`

---

## Explanation

- **Architecture overview**
  - `docs/design/architecture.md`
- **Backlog capture principles** (why extraction is strict)
  - `docs/design/backlog_capture_principles.md`
- **Monorepo packaging + snapshot publishing model**
  - `docs/monorepo-packages.md`

---

## Where to look depending on your role

- **I just want to run usertests (human or agent)** → `docs/tutorials/getting-started.md`
- **I need to add repo-specific personas/missions (human or agent)** → `docs/how-to/personas-and-missions.md`
- **I’m integrating a new agent adapter (developer/agent)** → `docs/agents/`
- **I’m reviewing the system contract** → `docs/design/run-artifacts.md`
- **I’m operating the repo (CI/publish/security)** → `docs/ops/`
