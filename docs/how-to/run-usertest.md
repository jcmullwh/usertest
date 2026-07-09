# How to run a usertest

This guide uses the module invocation form (`python -m usertest.cli ...`), which works even if you
haven’t installed the `usertest` console script yet.

If you did install the console script, you can replace `python -m usertest.cli` with `usertest` in
all examples.

If you don’t yet have a working environment, start with `docs/tutorials/getting-started.md`.

---

## Run a single target

### Representative validation (default built-in path)

```text
python -m usertest.cli run --repo-root . --repo "PATH/TO/TARGET" --agent codex --policy write
```

```text
python -m usertest.cli run --repo-root . --repo "https://github.com/org/repo.git" --agent codex --policy write
```

Defaults come from `configs/catalog.yaml`:

- persona: `representative_workflow_evaluator`
- mission: `verify_install_to_result`

Use this path when you want evidence that a real user can reach a representative result.

### Faster preflight probe

```text
python -m usertest.cli run --repo-root . --repo "PATH_OR_GIT_URL" --agent codex --policy inspect --persona-id quickstart_sprinter --mission-id first_output_smoke
```

Use this only to establish sign-of-life or isolate the first blocker.

## Choose a persona + mission


List built-ins:

```bash
python -m usertest.cli personas list --repo-root .
python -m usertest.cli missions list --repo-root .
```

Run with explicit IDs:

```text
python -m usertest.cli run --repo-root . --repo "PATH_OR_GIT_URL" --agent codex --policy write --persona-id representative_workflow_evaluator --mission-id produce_default_output
```

---

## Policies: safe vs inspect vs write

- Use `--policy safe` when you want the strictest mode.
- Use `--policy inspect` for preflight probes and read-only blocker isolation.
- Use `--policy write` for representative install-to-result validation and other workflows that may need setup/output creation.

Policies apply to **agent tool permissions during the run**.
They do not redact artifacts.

---

## Use a target-local `.usertest/` catalog

If the target repo contains `.usertest/catalog.yaml`, the runner will merge it with the default
catalog.

To initialize that folder in a local target repo:

```bash
python -m usertest.cli init-usertest --repo-root . --repo "PATH/TO/TARGET"
```

Then add repo-specific personas/missions under `.usertest/…` and reference them by ID.

Full guide: `docs/how-to/personas-and-missions.md`.

---

## Batch runs

Run multiple targets from a YAML file:

```text
python -m usertest.cli batch --repo-root . --targets examples/targets.yaml --agent codex --policy safe
```

Batch runs still produce per-target run directories; they’re just orchestrated from one command.

---

## Re-render a report without re-running

If you already have a run directory:

```bash
python -m usertest.cli report --repo-root . --run-dir "RUN_DIR"
```

To recompute metrics (and re-normalize `raw_events.jsonl`):

```bash
python -m usertest.cli report --repo-root . --run-dir "RUN_DIR" --recompute-metrics
```

Notes:

- `--recompute-metrics` **overwrites** `normalized_events.jsonl` as a side effect.
- For reproducibility, if `normalized_events.jsonl` already exists its timestamp stream is reused when
  possible; otherwise timestamps may be derived from `raw_events.ts.jsonl` (if present) or generated
  at recompute time.

---

## Compare delegation-disabled and delegation-enabled runs

Before making delegation policy more aggressive, compare paired maintenance runs:

```bash
python -m usertest.cli token-monitor delegation-ab \
  --disabled-run "RUN_DIR_WITHOUT_DELEGATION" \
  --enabled-run "RUN_DIR_WITH_DELEGATION" \
  --output-dir "experiments/idea-003-delegation-ab-validation"
```

The report is metadata-only and evaluates total input-token tradeoffs against
parent-context pressure, quality/review findings, resend signals, verification
behavior, and elapsed time. Raw source, prompts, and full logs are not copied.

---

## Use the Docker execution backend

The Docker backend is useful when you want:

- stronger isolation
- fewer host OS quirks (especially around shell commands)
- a more repeatable environment

```text
python -m usertest.cli run --repo-root . --repo "PATH_OR_GIT_URL" --agent codex --policy write --exec-backend docker
```

By default, Docker runs reuse host agent logins by mounting `~/.codex`, `~/.claude`, and/or
`~/.gemini`.

If you want API-key auth for Codex instead:

```text
python -m usertest.cli run --repo-root . --repo "PATH_OR_GIT_URL" --agent codex --policy write --exec-backend docker --exec-use-api-key-auth --exec-env OPENAI_API_KEY
```

### Docker cache (`--exec-cache`)

Docker runs can optionally mount a persistent host cache directory at `/cache` inside the container:

- `--exec-cache cold` (default): no persistent host cache mount; `/cache` is per-container and discarded.
- `--exec-cache warm`: mounts a host directory at `/cache` so caches survive across runs.

In the built-in `sandbox_cli` image, heavyweight tool caches are wired into `/cache` (notably `pip` and `pdm`),
so `warm` mainly speeds up repeated installs across runs.

`usertest-implement` adds an implement-only maintenance venv cache layer (enabled by default) on top of warm cache.
That layer stores per-project `.venv` snapshots under `/cache/usertest_maint_venvs` and can make repeated
`scaffold.py run install --all` steps near-noop when lockfiles are unchanged.

For same-repo `usertest-implement` Docker runs, the tool now also selects a dedicated maintenance
image profile by default. That profile:

- resolves a maintenance image from `local -> pull -> build`
- seeds project `.venv` directories inside the image under `/opt/usertest_maint_seed`
- bind-mounts matching cached `.venv` directories directly into `/workspace/<project>/.venv`

This profile is intentionally maintenance-only. `usertest` still uses the generic `sandbox_cli`
path for normal usertest runs against arbitrary targets.

For same-repo `usertest-implement` handoff runs, verification now also defaults to reuse mode:

- the prompt tells the agent to return its final JSON report when the work is ready instead of
  waiting on a long verifier command in the model transcript
- the runner then requests verification once through the broker, blocks outside the model loop,
  and finalizes automatically if it passes
- if the agent explicitly requested verification through the broker before returning, the run can
  still select that proof when the workspace is unchanged afterward
- the selected verification result is recorded in `verification.json`, and the reuse/fallback decision is
  recorded in `verification_reuse.json`

Defaults:

- Host cache directory: `<repo_root>/runs/_cache/usertest` (when `--exec-cache warm` and `--exec-cache-dir` is not set)
- Container mount point: `/cache`

Example (override the host cache directory):

```text
python -m usertest.cli run --repo-root . --repo "PATH_OR_GIT_URL" --agent codex --policy write --exec-backend docker --exec-cache warm --exec-cache-dir runs/_cache/usertest-ci
```

---

## Usertesting a published package (fresh install)

To test the “fresh install” experience (instead of a repo checkout), use a `pip:` target.

Example (GitLab PyPI credentials are passed through as exec env vars):

```text
python -m usertest.cli run --repo-root . --repo "pip:agent-adapters" --agent codex --policy write --persona-id representative_workflow_evaluator --mission-id verify_install_to_result --exec-backend docker --exec-env GITLAB_PYPI_PROJECT_ID --exec-env GITLAB_PYPI_USERNAME --exec-env GITLAB_PYPI_PASSWORD
```

See `docs/monorepo-packages.md` for details.

---

## Where outputs go

Run directories are written under:

`runs/usertest/<target>/<timestamp>/<agent>/<seed>/`

They contain rich evidence logs. Treat them as sensitive by default.

Before sharing a run directory (or uploading it from CI), do a quick review/redaction pass:

- scan for credentials and tokens in `prompt.txt`, `raw_events.jsonl`, `normalized_events.jsonl`,
  `agent_stderr.txt`, and verification logs
- watch for accidental capture of target `.env` contents (target acquisition does not exclude them)
- prefer sharing the smallest subset that still supports debugging (often `report.*`, `metrics.json`,
  `error.json`, `agent_stderr.txt`, and `verification*`)

CI archiving guidance (including GitHub Actions examples for `runs/usertest/**`): `docs/ops/security.md`.
