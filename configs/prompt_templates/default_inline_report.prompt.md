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

## Preflight summary

${preflight_summary_md}

## Environment

```json
${environment_json}
```

## Execution notes

${execution_notes_md}

## Output requirements

- Return **ONLY** a JSON object.
- The JSON MUST validate against the schema below.
- Do not use `run_shell_command` to print this JSON (for example via `cat`); return it directly as your assistant response.

```json
${report_schema_json}
```
