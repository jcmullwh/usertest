# Glossary

## Agent adapter

A thin wrapper around an external agent CLI (Codex / Claude / Gemini) that runs it headlessly and
captures its tool transcript.

Code: `packages/agent_adapters/`.

## Catalog

The configuration that tells the runner where to discover personas, missions, prompt templates, and
report schemas.

Runner default: `configs/catalog.yaml`.
Target override: `<target>/.usertest/catalog.yaml`.

## Mission

Defines **what task** the evaluator is trying to accomplish (install, first output, troubleshoot,
etc.).

Missions select the prompt template and the report schema.

## Persona

Defines **who the evaluator is** (constraints, priorities, what “success” means).

Personas should be stable so results remain comparable.

## Policy

An execution policy that controls what tools the agent is allowed to use.

Configured in `configs/policies.yaml`.

Common policies:

- `safe`: strict read-only
- `inspect`: read-only + allows shell
- `write`: allows edits

## Run directory

The folder written for each run under `runs/usertest/…`.

It contains the prompt, resolved persona/mission, tool logs, metrics, and report.

Contract: `docs/design/run-artifacts.md`.

## Workspace

The isolated checkout/installation directory where the agent runs.

Workspaces may be local or inside Docker depending on `--exec-backend`.

## Raw events

The unmodified tool transcript captured from the agent CLI.

Written as JSONL: `raw_events.jsonl`.

## Normalized events

A stable, agent-agnostic event stream derived from raw events.

Written as JSONL: `normalized_events.jsonl`.

Contract: `docs/design/event-model.md`.

## Report schema

A JSON Schema used to validate `report.json`.

Schemas live under `configs/report_schemas/`.

## Snapshot publishing

A mechanism to publish selected monorepo packages to a private registry as uniquely-versioned dev
releases.

Docs: `docs/monorepo-packages.md`.
Tooling: `tools/monorepo_publish/`.

## Scaffold

The repo tool that manages the monorepo manifest and runs tasks across projects.

Code: `tools/scaffold/scaffold.py`.
