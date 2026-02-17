---
id: self_policy_security_review
name: Understand Policies and Security Boundaries (Operator UX)
extends: null
tags: [selftest, ux, security, policy, p1]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: task_run_v1.schema.json
---

## Goal

Evaluate whether a security-conscious operator can confidently answer:

- “What can the agent do under each policy?”
- “What data might end up in run artifacts?”
- “How do I run this safely on a sensitive repo?”

This is **not** a security audit. It’s a UX mission about whether the repo communicates safety boundaries clearly.

## Tasks

- Find where policies are defined and how they map to each agent.
- Build a small policy comparison matrix (safe vs inspect vs write).
- Find the repo’s guidance on handling secrets and run artifacts.
- Identify inconsistencies or unclear statements across README/docs/config.

## Evidence to include in your report (measurable)

- A policy matrix with at least:
  - whether workspace edits are allowed
  - whether shell commands are allowed
  - whether network access is implied/blocked
  - how agent “approvals” are handled
- Cite the exact file(s) where this is defined (paths).
- At least **5** concrete “safe operation” recommendations for operators.
- At least **3** actionable doc/UX improvements that would reduce accidental unsafe usage.

## UX focus prompts

- Can a new operator choose the right policy without reading source?
- Are there warnings in the right places (README vs deep docs)?
- Is it obvious which artifacts might contain sensitive content?
