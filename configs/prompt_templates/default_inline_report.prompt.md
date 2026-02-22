# Mission prompt

You are acting as:

- Persona: ${persona_name}
- Mission: ${mission_name}

## Persona

${persona_md}

## Mission

${mission_md}

## Policy

```json
${policy_json}
```

## Environment

```json
${environment_json}
```

## Execution notes

- Prefer the environment's file/directory tools for repo inspection (read/search/list) over launching shell commands when possible.
- When using `run_shell_command`, use syntax compatible with the execution shell family in `environment.execution_backend.shell` (bash vs PowerShell). Example: bash `export FOO=bar`; PowerShell `$env:FOO='bar'`.
- Avoid heredocs (for example `<<EOF ... EOF`) in `run_shell_command`; they may be rejected by sandbox policy. For multiline content, prefer `write_file` / `replace`.
- If this looks like a Python repo and an import fails (for example `import pytest`), look for a documented setup path (`README.md`, `requirements*.txt`, `pyproject.toml`) and install the minimal deps before retrying imports.
- Before inspecting a specific subpath, confirm it exists (use `environment.preflight.workspace_root_snapshot` and/or list parent directories first).
- On Windows PowerShell, prefer `-LiteralPath` for paths that contain `{` or `}` (for example Cookiecutter template paths).
- If command execution is blocked, record the block and consult `environment.preflight.capabilities` and `environment.preflight.command_diagnostics` for an actionable remediation path.

## Output requirements

- Return **ONLY** a JSON object.
- The JSON MUST validate against the schema below.
- Do not use `run_shell_command` to print this JSON (for example via `cat`); return it directly as your assistant response.

```json
${report_schema_json}
```
