# Security and privacy for run artifacts (`runs/`)

Run directories under `runs/` are **sensitive by default**.

They can include:

- prompts and transcripts (`prompt.txt`, `raw_events.jsonl`, `normalized_events.jsonl`)
- tool output (`agent_stderr.txt`, verification logs, command/tool failure logs)
- rendered reports (`report.md`, `report.json`, `metrics.json`)
- workspace diffs (`patch.diff`, `diff_numstat.json`)
- sandbox diagnostics (for example under `sandbox/`)

This data can contain proprietary code and/or credentials that were printed by tools (including
content from `.env`-style files, which are not automatically excluded from target acquisition).

## Safe sharing checklist

Before you share a run directory (or any file from it), do a quick review/redaction pass:

1. Prefer sharing a minimal subset (often `report.*`, `metrics.json`, `error.json`,
   `agent_stderr.txt`, and `verification*`).
2. Scan text artifacts for credentials/tokens (API keys, OAuth tokens, private keys, connection
   strings), plus accidentally captured `.env` contents.
3. If you must share raw transcripts/events, assume they may contain proprietary code snippets and
   secrets printed by tools.
4. If you are sharing outside your org, consider re-running against a sanitized target or a minimal
   reproduction project instead.

## CI archiving guidance (use with caution)

Archiving run directories from CI can be valuable for debugging, but treat it as an explicit
opt-in. Recommended guardrails:

- Upload only on failure (or behind a manual flag).
- Restrict access (private repos, limited artifact visibility, protected branches).
- Keep retention short.
- Prefer uploading a minimal subset over `runs/usertest/**` when possible.

### GitHub Actions (example)

This uploads `runs/usertest/**` only when the workflow fails.

```yaml
- name: Upload usertest run artifacts (sensitive)
  if: ${{ failure() }}
  uses: actions/upload-artifact@v4
  with:
    name: usertest-runs
    path: runs/usertest/**
    if-no-files-found: ignore
    retention-days: 7
```

If you run on `pull_request` from forks, consider skipping artifact upload for forked PRs to avoid
unintended exposure.

### GitLab CI (example)

```yaml
artifacts:
  when: on_failure
  expire_in: 7 days
  paths:
    - runs/usertest/
```

