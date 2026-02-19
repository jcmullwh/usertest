# Getting started

This repo provides a **repeatable “agent usertest” runner**.

You point it at a target repository, choose:

- a **persona** (who the evaluator is),
- a **mission** (what they are trying to do),
- a **policy** (how much the agent is allowed to do),

…and it produces a **run directory** containing a human report, a schema-validated JSON report, and
evidence logs that let you audit what happened.

The runner can drive multiple agent CLIs (Codex, Claude Code, Gemini) in headless mode.

---

## What you get

Each run writes a folder under `runs/usertest/…` with:

- `report.md` – human-friendly findings
- `report.json` – machine-friendly output (validated against a JSON schema)
- `metrics.json` – trace-derived metrics (commands run, errors, timing, etc.)
- `raw_events.jsonl` → `normalized_events.jsonl` – the tool transcript in a stable contract

If writes are allowed and edits occurred, a `patch.diff` is also written.

See `docs/design/run-artifacts.md` for the full contract.

---

## Core concepts

### Persona

**Who is the user?**

A persona should be stable so you can compare runs over time.
It captures things like goals, constraints, risk tolerance, and what counts as “success”.

Built-in personas live under `configs/personas/builtin/`.

### Mission

**What are they trying to accomplish in this run?**

A mission should be specific and judgeable (e.g., “get to the first meaningful output”).
Missions select a prompt template and a report schema so results remain structured.

Built-in missions live under `configs/missions/builtin/`.

### Policy

**What the agent is allowed to do.** Policies map to tool permissions.

- `safe`: read-only (strictest)
- `inspect`: read-only + allows non-destructive shell commands (recommended for most first runs)
- `write`: allows workspace edits

Policies are configured in `configs/policies.yaml`.

### Target-local `.usertest/`

If you want repo-specific personas/missions **versioned inside the target repo**, add a
`.usertest/` folder there.

This is the preferred way to encode: “How should we evaluate *this* repo?”

---

## Fastest output (no setup)

To understand what a run produces without installing anything, open the checked-in golden fixture:

- `examples/golden_runs/minimal_codex_run/report.md`
- `examples/golden_runs/minimal_codex_run/metrics.json`

You can also re-render that fixture from raw events:

```text
python -m usertest.cli report --repo-root . --run-dir examples/golden_runs/minimal_codex_run --recompute-metrics
```

---

## One-command smoke (recommended)

For a fast, deterministic end-to-end sanity check (doctor â†’ deps â†’ CLI help â†’ smoke tests), use the OS-specific smoke script:

Windows PowerShell:

```text
powershell -NoProfile -ExecutionPolicy Bypass -File .\\scripts\\smoke.ps1
```

macOS/Linux:

```text
bash ./scripts/smoke.sh
```

---

## Offline / air-gapped workflows

This repo supports some offline-safe workflows, but itâ€™s important to distinguish:

- **Offline-safe:** rendering reports from existing run artifacts (including the checked-in golden fixtures).
- **Not offline-safe:** running hosted agents (Codex/Claude/Gemini) requires outbound network access to the model provider APIs.

### Offline-safe: render reports and run smoke/tests

To run the **non-agent** parts offline, pre-download Python wheels while you still have network access, then install from a local wheelhouse.

1) While online (from repo root):

   ```bash
   python -m pip download -r requirements-dev.txt -d wheelhouse
   ```

2) Later, while offline (fresh virtualenv recommended):

   ```bash
   python -m pip install --no-index --find-links wheelhouse -r requirements-dev.txt
   ```

After that, you can run:

- `python -m usertest.cli report ...` (report re-rendering)
- `python -m pytest -q apps/usertest/tests/test_smoke.py` (smoke tests)

### Docker note

The Docker execution backend builds a sandbox image that installs tools (APT) and agent CLIs (often via npm). If you need reproducible behavior in restricted environments, build the image ahead of time and avoid rebuilds (do not pass `--exec-rebuild-image`).

---

## First real run

### 1) Install the CLI

From the repo root:

```bash
python -m pip install -r requirements-dev.txt
python -m pip install -e apps/usertest
```

Sanity check:

```bash
python -m usertest.cli --help
# If you installed the console script: usertest --help
```

> Monorepo note
>
> This repo contains multiple Python projects. For a more “monorepo-native” workflow (the same one
> CI uses), see `docs/tutorials/monorepo-setup.md` and `tools/scaffold/README.md`.

### 2) Install an agent CLI (one is enough)

The runner drives external CLIs. You need at least one installed and authenticated:

- `codex`
- `claude` (Claude Code)
- `gemini` (Gemini CLI)

Adapter notes: `docs/agents/`.

### 3) Pick a target repo

`--repo` can be:

- a local path
- a git URL

Optional but recommended: add a `USERS.md` file to the **target** repo describing who the users are
and what “success” means. If present, it is snapshotted into the run directory.

### 4) Run

For a first attempt, use `inspect` (read-only + allows shell commands):

```text
usertest run --repo-root . --repo "PATH_OR_GIT_URL" --agent codex --policy inspect --persona-id quickstart_sprinter --mission-id first_output_smoke
```

Strictest mode (no shell, no writes):

```bash
usertest run --repo-root . --repo "PATH_OR_GIT_URL" --agent codex --policy safe
```

### 5) Inspect the output

The command prints the run directory path. The most useful files:

- `report.md`
- `metrics.json`
- `report.json`
- `raw_events.jsonl` / `normalized_events.jsonl`

---

## Next: customize personas and missions for a specific repo

If you’re evaluating a particular repo repeatedly, put repo-specific definitions in that repo:

1) Scaffold the target repo:

```bash
usertest init-usertest --repo-root . --repo "PATH_TO_LOCAL_REPO"
```

2) Edit `PATH_TO_LOCAL_REPO/.usertest/catalog.yaml` to point to your definitions.

3) Add personas/missions under `PATH_TO_LOCAL_REPO/.usertest/…`.

Full guide: `docs/how-to/personas-and-missions.md`.

---

## If you run into issues

- If the agent is blocked by policy, switch to `--policy inspect` (read-only + shell).
- If you need isolation or want fewer OS-specific shell issues, use the Docker backend:

```bash
usertest run --repo-root . --repo "PATH_OR_GIT_URL" --agent codex --policy inspect --exec-backend docker
```

More workflows: `docs/how-to/run-usertest.md`.
