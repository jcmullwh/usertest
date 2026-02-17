# Report

## Target

```json
{
  "repo_input": "golden_fixture",
  "ref": null,
  "commit_sha": "0000000000000000000000000000000000000000",
  "acquire_mode": "copy",
  "agent": "gemini",
  "policy": "safe",
  "seed": 0,
  "prompt_version": 1
}
```

## Report

```json
{
  "schema_version": 1,
  "persona": {
    "name": "Evaluator",
    "description": "Golden fixture report."
  },
  "mission": "Assess fit quickly and safely.",
  "minimal_mental_model": {
    "summary": "This is a minimal, synthetic fixture used for regression tests.",
    "entry_points": [
      "README.md"
    ]
  },
  "confidence_signals": {
    "found": [
      "Runner can execute commands"
    ],
    "missing": [
      "Real target context"
    ]
  },
  "confusion_points": [],
  "adoption_decision": {
    "recommendation": "investigate",
    "rationale": "Fixture output only."
  },
  "suggested_changes": []
}
```

## Metrics

```json
{
  "event_counts": {
    "agent_message": 1,
    "read_file": 1,
    "run_command": 1
  },
  "distinct_files_read": [
    "README.md",
    "USERS.md"
  ],
  "distinct_docs_read": [
    "README.md",
    "USERS.md"
  ],
  "distinct_files_written": [],
  "commands_executed": 1,
  "commands_failed": 0,
  "lines_added_total": 0,
  "lines_removed_total": 0,
  "step_count": 2
}
```
