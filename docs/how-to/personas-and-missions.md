# How to write personas and missions

Personas and missions are the heart of this system:

- **Personas** define *who* is doing the evaluation.
- **Missions** define *what* they are trying to accomplish.

Keeping them separate makes results comparable:

- run the *same mission* with different personas (“novice vs expert”)
- run the *same persona* with different missions (“install vs troubleshoot”)

Both are plain Markdown files with YAML frontmatter.

---

## Where they live

### Built-ins (in this repo)

- Personas: `configs/personas/builtin/*.persona.md`
- Missions: `configs/missions/builtin/*.mission.md`

List them:

```bash
usertest personas list --repo-root .
usertest missions list --repo-root .
```

### Repo-specific (in a target repo)

For a particular target repo, put custom definitions under that repo’s `.usertest/` directory.

Recommended layout inside the *target* repo:

```text
.usertest/
  catalog.yaml
  personas/
    my_team.persona.md
  missions/
    first_run.mission.md
```

To scaffold this structure in a local target repo:

```bash
usertest init-usertest --repo-root . --repo "PATH/TO/TARGET"
```

---

## Catalog discovery (how the runner finds files)

The runner has a default catalog at `configs/catalog.yaml`.

If the target repo has `.usertest/catalog.yaml`, it is merged in. Common use:

- append additional `personas_dirs` and `missions_dirs`
- override default persona/mission IDs for that repo

Minimal example for a target repo:

```yaml
version: 1

personas_dirs:
  - .usertest/personas

missions_dirs:
  - .usertest/missions

defaults:
  persona_id: my_team
  mission_id: first_run
```

Relative paths in `personas_dirs` / `missions_dirs` are resolved relative to the *target repo root* (the directory passed to `init-usertest --repo`).

---

## Persona format

- Filename must end with `.persona.md`
- YAML frontmatter is required

Required frontmatter fields:

- `id` (string)
- `name` (string)

Optional but common:

- `extends` (string): ID of another persona to inherit from

### Persona template

```md
---
id: my_team
name: My Team Evaluator
extends: quickstart_sprinter
---

## Snapshot

Who is this evaluator? What context do they bring?

## Success looks like

- Concrete, testable outcomes.

## Constraints

- Time budget, risk tolerance, “don’t do” list.

## Reporting style

How should the output be written?
```

### Persona tips

- Keep personas stable so you can compare runs.
- Prefer “decision criteria” over step-by-step instructions.
- State constraints explicitly (time, risk, allowed changes).

---

## Mission format

- Filename must end with `.mission.md`
- YAML frontmatter is required

Required frontmatter fields:

- `id` (string)
- `name` (string)

Common optional fields:

- `extends` (string): inherit prompt/schema/settings from a base mission
- `tags` (list of strings)
- `requires_shell: true` if the mission genuinely needs shell commands
- `requires_edits: true` if the mission requires edits

> The `requires_*` flags allow the runner to fail fast with a clear error when a policy
> cannot support the mission.

### Mission template (recommended: extend a builtin)

```md
---
id: first_run
name: First run for our stack
extends: first_output_smoke
tags: [repo_specific]
requires_shell: true
requires_edits: false
---

## Goal

Produce one meaningful output using the repo’s default workflow.

## What counts as success

- A command that produces a real output (file or terminal output).
- Clear prerequisites and repeatable steps.

## Constraints

- Do not publish/deploy.
- If blocked, try 1–2 reasonable fixes, then stop and report.
```

---

## How inheritance works (`extends`)

Inheritance is intentionally simple:

### Personas

If `extends` is set:

1) base persona body
2) child persona body

### Missions

If `extends` is set, the runner inherits missing fields (prompt template, schema, execution mode)
from the base mission and concatenates the Markdown bodies.

---

## Validate discovery

After adding files to a target repo:

```bash
usertest personas list --repo "PATH/TO/TARGET"
usertest missions list --repo "PATH/TO/TARGET"
```

Then run with the IDs:

```bash
usertest run --repo-root . --repo "PATH/TO/TARGET" --persona-id my_team --mission-id first_run --policy inspect --agent codex
```
