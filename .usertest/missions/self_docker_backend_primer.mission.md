---
id: self_docker_backend_primer
name: Docker Execution Backend Readiness and UX Review
extends: null
tags: [selftest, ux, sandbox, docker, p2]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: task_run_v1.schema.json
---

## Goal

Evaluate the UX of using the **docker execution backend**:

- Can you discover that it exists?
- Can you figure out prerequisites and the minimal command?
- Are the flags and terminology clear (`--exec-backend`, contexts, caching, network)?

This mission is about clarity and operator confidence, not about benchmark performance.

## Tasks

- Find all documentation and CLI help text relevant to docker execution.
- Produce a “minimal docker run recipe” that includes:
  - required host prerequisites
  - how to pass or mount agent credentials (if applicable)
  - where artifacts will be written
- If docker is available in your environment and permitted by policy, do a lightweight smoke check (e.g., `docker --version` and/or a minimal `usertest` invocation that fails early but demonstrates the docker path is reachable).

## Evidence to include in your report (measurable)

- The minimal docker recipe command line you would recommend.
- A prerequisites checklist.
- File references (paths) to the docs/help text you used.
- If you ran any docker-related commands: include the command + a short output snippet.
- At least **3** UX issues or ambiguities you encountered (flag naming, missing docs, unclear defaults).

## UX focus prompts

- Would a cautious operator trust the defaults?
- Is it obvious how to disable network?
- Is cache behavior discoverable?
