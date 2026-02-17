# Architecture (MVP)

## Goal

Run a headless CLI agent against a target repo, capture raw tool logs, normalize them into a stable event model, derive metrics, and emit a schema-validated report.

## Packages

- `packages/runner_core`: target acquisition, workspace hygiene, run directory lifecycle, prompt assembly, adapter invocation.
- `packages/agent_adapters`: thin wrappers around agent CLIs (MVP: Codex), plus raw event parsing → normalized events. Extensible to other agents (Claude Code, Gemini, etc.). Generic to other agentic use cases.
- `packages/reporter`: metrics extraction and report assembly/validation helpers.
- `packages/triage_engine`: generic similarity and clustering primitives for triage workloads.
- `packages/backlog_core`: backlog-domain parsing, dedupe, coverage, policy, and markdown/document rendering.
- `packages/backlog_miner`: LLM backlog-mining orchestration and merge/labeler passes.
- `packages/backlog_repo`: repo-specific backlog inclusion helpers (`.agents/plans` sync, action ledgers, export fingerprints).
- `packages/sandbox_runner`: sandboxed execution environments (MVP: Docker-based). Focused on agentic use cases but generic to other sandboxed execution use cases as well.
- `apps/usertest`: end-user CLI (`usertest run`, `usertest batch`).
- `apps/usertest_backlog`: backlog-focused CLI (`usertest-backlog` / `python -m usertest_backlog.cli`).

## Data flow (single run)

1. Resolve target repo (local path or git URL) into an isolated workspace.
2. Optionally read `USERS.md` from the target. If present, it is included in prompt assembly and snapshotted into the run directory.
3. Resolve persona/mission/template/schema from the catalog, then build a prompt via template substitution.
4. Invoke agent adapter in headless mode; capture raw events and final message.
5. Normalize raw events into `normalized_events.jsonl`.
6. Compute metrics from normalized events.
7. Validate final report JSON against the mission-selected schema (snapshotted as `report.schema.json`), then write `report.json` and `report.md`.

## Run artifact contract

See:

- `docs/design/run-artifacts.md` for the concrete file-level run directory contract.
- `docs/design/event-model.md` for normalized event schema.
- `docs/design/report-schema.md` for report JSON schema expectations.

## Security boundaries (MVP)

- The system captures rich artifacts by design (`prompt.txt`, `raw_events.jsonl`, `normalized_events.jsonl`, `preflight.json`, `report.json`, `report.md`).
- Policy selection (`safe` / `inspect` / `write`) controls agent execution permissions, not post-hoc artifact redaction.
- Current sanitization is narrow and targeted (for example selected stderr noise suppression and sandbox diagnostic env-value scrubbing); there is no universal secret scrub of all artifacts.
- Target acquisition does not automatically exclude `.env`-style files. If those files exist in scope and are readable, agent prompts/tool output can include their contents.
- Treat run directories under `runs/usertest/` as sensitive operational data. For operator guidance, see `.agents/ops/security.md`.
