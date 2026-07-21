# Report

## Target

```json
{
  "repo_input": "C:\\Users\\jason\\AppData\\Local\\Temp\\ut_checkout_enospc_probe_pi3nkx19\\source",
  "ref": "probe-ref",
  "commit_sha": "10a9ac3c987ab029994f2345bc8be183e44fb8f8",
  "acquire_mode": "git",
  "agent": "codex",
  "policy": "safe",
  "seed": 7001,
  "obfuscate_agent_docs": false,
  "requested_persona_id": "p",
  "requested_mission_id": "m",
  "requested_codex_resume_session_id": null,
  "users_md_present": false,
  "persona_id": "p",
  "mission_id": "m",
  "prompt_template_path": "C:\\Users\\jason\\AppData\\Local\\Temp\\ut_checkout_enospc_probe_pi3nkx19\\runner_root\\configs\\prompt_templates\\t.prompt.md",
  "report_schema_path": "C:\\Users\\jason\\AppData\\Local\\Temp\\ut_checkout_enospc_probe_pi3nkx19\\runner_root\\configs\\report_schemas\\s.schema.json"
}
```

## Raw report.json

```json
{
  "ok": "yes",
  "extensions": {
    "verification": {
      "status": "disabled",
      "terminal_reason": null,
      "failure_reason": null,
      "source": "disabled",
      "reused": false,
      "timed_out": false,
      "cancelled": false
    },
    "python_toolchain_capability": {
      "toolchain_status": "not_required",
      "python_required": false,
      "pdm_required": false,
      "interpreter_usable": true,
      "context_probe_passed": null,
      "reason_code": null,
      "reason_type": null,
      "reason": null,
      "validated_executable": "U:\\ubq\\implementation\\_workspaces\\bc70b15b_20260721T052958Z_codex_0\\.venv\\Scripts\\python.exe"
    },
    "shell_capability": {
      "state": "unprobed",
      "agent": "codex",
      "operating_system": "Windows",
      "backend": "local",
      "sandbox_mode": "read-only",
      "probe_status": "passed",
      "reason_code": "codex_windows_shell_unprobed",
      "reason_type": "runtime",
      "reason": "A generic local shell payload probe passed, but local Windows Codex shell backend launchability was not proven under the Codex sandbox policy.",
      "policy_status": "allowed",
      "policy_reason": "Codex sandbox policy is explicitly configured as read-only.",
      "allowed_tools": null
    }
  }
}
```

## Metrics

```json
{
  "event_counts": {
    "agent_message": 1,
    "preflight_shell_capability": 1
  },
  "distinct_files_read": [],
  "distinct_docs_read": [],
  "distinct_files_written": [],
  "commands_executed": 0,
  "commands_failed": 0,
  "commands_no_matches": 0,
  "commands_blocked_by_policy": 0,
  "lines_added_total": 0,
  "lines_removed_total": 0,
  "step_count": 0
}
```
