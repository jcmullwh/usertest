---
id: backlog_repro_research
name: Backlog Repro + Research (Writable Workspace)
extends: null
tags: [repo_local, backlog, research, reproduction]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: troubleshoot_v1.schema.json
requires_shell: true
requires_edits: true
---

## Goal

Reproduce and research a specific backlog problem **in an isolated writable workspace**.

The goal is **evidence and a bounded root cause**.

The goal is **NOT** to implement the fix, and **NOT** to judge success by a clean diff.

## Contract (repeat: do not implement)

Hard rules:

- Your primary output is a reproduction or a bounded investigation, not a fix.
- You may write files only to support reproduction and investigation:
  - failing tests that demonstrate the problem
  - temporary instrumentation to observe the failure
  - repro harness scripts
  - minimal fixture/setup changes required to trigger the issue
- Do **not**:
  - implement the solution / fix the bug
  - change production behavior to make the symptom disappear
  - introduce new user-visible features or commands
  - perform broad refactors unrelated to the reproduced path
  - write documentation as if the change shipped

If you accidentally made implementation-like changes, you must **admit it** and treat it as suspicious.

## Required extension block (must be present)

In your final JSON report, include this required extension block:

```json
{
  "extensions": {
    "backlog_repro_research": {
      "problem_id": "problem:...",
      "reproduction_status": "reproduced|reproduction_failed|partial",
      "writes_used": true,
      "writes_purpose": [
        "failing_test",
        "temporary_instrumentation",
        "repro_harness",
        "fixture_change",
        "none"
      ],
      "implementation_performed": false,
      "root_cause_hypotheses": ["..."],
      "broader_class_assessment": "isolated_instance|repeated_variant|unknown",
      "unknowns": ["..."]
    }
  }
}
```

Notes:
- `implementation_performed` must be `false` even if you made writes. This stage is research-only.
- `writes_purpose` should be honest and specific. Use `"none"` if you made no writes.

## How to fill the troubleshoot report fields

Your report must validate against `troubleshoot_v1`:

- `goal`: restate the backlog problem you were assigned (include the problem_id).
- `failure_point`: describe where the failure manifests (command/test + error).
- `evidence.what_happened`: the reproduced behavior or the bounded observation.
- `attempted_fixes`: list what you tried to reproduce/bound the issue (not implementations).
- `recommended_fix_path`: list **next research actions** or a narrow fix direction, without implementing it.

