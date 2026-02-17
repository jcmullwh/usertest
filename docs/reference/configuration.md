# Configuration reference

This repo is configured primarily through files under `configs/`.

The runner also supports **target-local overrides** via `.usertest/` inside the repository being
tested.

---

## Runner configuration (`configs/`)

### `configs/agents.yaml`

Defines how to invoke each agent CLI adapter (binary path, default args, model config, etc.).

Used by:

- `apps/usertest` (`usertest run`, `usertest batch`)

### `configs/policies.yaml`

Defines execution policies (`safe`, `inspect`, `write`) and maps them to per-agent permissions.

Policies are enforced at runtime to control what tools the agent is allowed to use.

### `configs/catalog.yaml`

Defines where personas/missions/templates/schemas are discovered from and what defaults are.

This is the “root catalog”; target repos may extend/override it via `.usertest/catalog.yaml`.

### `configs/personas/` and `configs/missions/`

Built-in personas and missions.

- Personas: `configs/personas/builtin/*.persona.md`
- Missions: `configs/missions/builtin/*.mission.md`

### `configs/prompt_templates/`

Prompt templates used by missions.

Templates are Markdown with `${...}` placeholders that the runner fills with:

- resolved persona markdown
- resolved mission markdown
- `USERS.md` snapshot text (if present)
- policy + environment JSON
- report schema JSON

### `configs/report_schemas/`

JSON Schemas that validate `report.json`.

Missions select which schema to use.

### `configs/sandboxes/`

Sandbox execution configuration (Docker contexts, overlays, etc.).

See `configs/sandboxes/README.md`.

### Backlog-specific configuration

These are primarily used by `usertest-backlog` and backlog mining flows:

- `configs/backlog_prompts/`
- `configs/backlog_actions.yaml`, `configs/backlog_atom_actions.yaml`
- `configs/backlog_policy.yaml`

---

## Target-local configuration (`.usertest/` in the target repo)

### `.usertest/catalog.yaml`

Lets a target repo add/override persona and mission directories, plus defaults.

Typical use cases:

- keep repo-specific personas/missions versioned with the repo
- define “the default evaluation” for the repo

See `docs/how-to/personas-and-missions.md`.

### `.usertest/sandbox_cli_install.yaml`

Optional. Lets a target repo request extra packages or tools to be installed into the Docker
sandbox image before the run.

This is useful when:

- the repo’s “first run” requires nonstandard system deps
- you want consistent sandbox behavior across machines

The runner only uses this file when you pass:

- `--exec-use-target-sandbox-cli-install`

---

## Monorepo tooling configuration (`tools/scaffold/`)

The scaffold tool is configured by:

- `tools/scaffold/registry.toml` – generators
- `tools/scaffold/monorepo.toml` – project manifest (IDs, paths, tasks, CI flags)

See `tools/scaffold/README.md`.
