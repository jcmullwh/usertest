---
id: self_end_to_end_run_single_target
name: "End-to-End: Run Usertest on a Small Target Repo"
extends: null
tags: [selftest, ux, e2e, p0, requires_write]
execution_mode: single_pass_inline_report
prompt_template: inline_report_evidence_v1.prompt.md
report_schema: task_run_evidence_v1.schema.json
---

## Goal

Exercise the repo’s **primary user journey** end-to-end:

> “I have a target repo. I want to run a usertest persona+mission and get a run directory with a rendered report and metrics.”

## Success requires all of:

- a real `usertest run` invocation was executed
- the run directory was created during this run
- `report.md` and `metrics.json` exist and were inspected
- one explicit check confirms the generated artifacts match the attempted run

## Non-success cases

These do not count as success by themselves:

- printing the command without running it
- creating a scratch target but never running `usertest`
- stopping after CLI help, prompt generation, or preflight output
- inspecting checked-in golden runs instead of generating a fresh run

## UX focus

- Can you figure out the *minimum required inputs* (agent, policy, repo, persona/mission)?
- Is the CLI feedback clear about what it’s doing and where outputs are?
- If something is missing (agent binary, credentials, docker), are the error messages actionable?

## Scenario

You are a first-time operator. Pick a **small, low-risk** target repo to run against.

Good targets:

- a tiny local scratch repo you create (e.g., a README + one script)
- a small public repo with no secrets and minimal setup

Avoid targets that require credentials, large downloads, or long-running services.

## Suggested approach (not a script)

- Use the repo’s docs to find the recommended `usertest run` invocation pattern.
- Prefer a conservative but write-capable policy for the first real attempt.
- If you have multiple agent CLIs available, prefer using one that won’t create confusing recursion with your current environment.
- After the run completes (or fails), inspect the produced run directory and explain its contents.

## Evidence to include in your report (measurable)

If successful:

- The exact command used to run the test.
- The printed run directory path.
- A short explanation of why the target repo was sufficient to exercise the product promise.
- A list of **at least 6** run artifacts present in that directory (by filename), including `report.md` and `metrics.json`.
- A short excerpt (a few lines) from `report.md` and one key metric from `metrics.json`.
- One explicit check that ties the generated report back to the run you executed.

If blocked:

- The exact command you ran.
- The most important error output (verbatim snippet).
- A concrete “operator fix” checklist (what you would install/configure/change).
- A “product fix” suggestion (what the tool could do to make this failure less confusing).
- The next representative step that was blocked.

## Stop conditions

If you hit blockers, attempt at most **two** targeted fixes. Then stop and report.

## Notes

This mission typically requires a write-capable policy because it needs to create a run directory.
