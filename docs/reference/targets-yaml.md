# `targets.yaml` reference (`usertest batch`)

`usertest batch` runs multiple evaluations from a single YAML file. This document describes the file format used by `--targets`.

If you are new and just want a working starting point, copy `examples/targets.yaml` and then run:

    python -m usertest.cli batch --repo-root . --targets examples/targets.yaml --agent codex --policy safe --validate-only --skip-command-probes

## File shape

The file must be a YAML mapping with a single top-level key:

- `targets`: a YAML list (`[]`) of per-target mappings.

Example:

    targets:
      - repo: "https://github.com/example/example.git"
        ref: "main"
        agent: "codex"
        policy: "safe"
        persona_id: "quickstart_sprinter"
        mission_id: "first_output_smoke"
        seed: 0

## Per-target fields

### Required

- `repo` (string, non-empty): what to run the agent against.
  - Can be a local path, a git URL, or a synthetic target like `pip:<package>` / `pdm:<spec>`.

### Common optional fields

If omitted, these fall back to the corresponding CLI flag values (or to catalog defaults where applicable).

- `ref` (string | null): git branch/tag/SHA to checkout when `repo` is a git URL.
- `agent` (string | null): which adapter to use. Valid values live in `configs/agents.yaml` (commonly `codex`, `claude`, `gemini`).
- `policy` (string | null): execution policy. Valid values live in `configs/policies.yaml` (commonly `safe`, `inspect`, `write`).
- `persona_id` (string | null): which persona to run. If omitted/null, the catalog default may be used.
- `mission_id` (string | null): which mission to run. If omitted/null, the catalog default may be used.
- `seed` (integer): a label used for comparability across runs.
- `model` (string | null): optional model override (if supported by the selected agent).

### Advanced fields

- `agent_config` (list[string]): repeatable agent configuration overrides applied in addition to any `--agent-config` flags.
  - Alias: `agent_config_overrides` (prefer `agent_config`).
- `preflight_commands` (list[string]): extra commands to probe during preflight in addition to `--preflight-command`.
- `preflight_required_commands` (list[string]): commands that must be available and permitted (fails fast) in addition to `--require-preflight-command`.
- `verification_commands` (list[string]): repeatable shell commands that must pass before handing off (in addition to any `--verify-command` flags).
- `verification_timeout_seconds` (number | null): optional per-command timeout for verification checks (non-positive disables).

Retry/backoff tuning (usually only needed when debugging provider capacity issues):

- `agent_rate_limit_retries` (integer)
- `agent_rate_limit_backoff_seconds` (number)
- `agent_rate_limit_backoff_multiplier` (number)
- `agent_followup_attempts` (integer)

## Validation behavior

- `python -m usertest.cli batch --validate-only ...` validates `targets.yaml` and exits without creating run directories or invoking any agent.
- On validation failure, the command prints a structured error summary and exits with code `2`.

For the most up-to-date flags, always prefer:

    python -m usertest.cli batch --help
