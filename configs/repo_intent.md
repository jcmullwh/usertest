# Repo intent: agentic-usertest

This file is human-owned. It is the stable statement of what this repo is for, independent of any single mission, persona, or usertest run.

## What this repo is for

This repository exists to run repeatable, auditable “agent usertests” against target repositories using headless CLI agents (Codex / Claude / Gemini), producing structured reports and trace-derived metrics.

## Primary user journeys

- Run a single persona + mission against a target repo and inspect the resulting `report.json` / `report.md`.
- Run batches of targets for longitudinal analysis and compare outcomes across agents/missions.
- Generate structured backlogs from run artifacts, then route risky “new surface area” proposals into research/design review rather than immediately building them.

## Design constraints

- Runs must be reproducible and debuggable from artifacts (the transcript is canonical).
- Prefer loud failures to silent fallbacks.
- Avoid mission-specific “local optimum” behavior in shared logic.
- Tests must run offline (no network calls).

## Command surface philosophy

- Prefer a small number of composable commands and flags.
- When a proposal suggests adding new top-level commands/modes/config schemas, require evidence breadth across multiple contexts and route to research/UX review if evidence is narrow.

## Things we deliberately won’t do

- Treat a single mission’s preferences as the product definition.
- Add user-visible surface area by default as the first response to friction (prefer parameterization or docs/examples when possible).
