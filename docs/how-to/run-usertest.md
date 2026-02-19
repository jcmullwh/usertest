# How to run a usertest

This guide uses the module invocation form (`python -m usertest.cli ...`), which works even if you
haven’t installed the `usertest` console script yet.

If you did install the console script, you can replace `python -m usertest.cli` with `usertest` in
all examples.

If you don’t yet have a working environment, start with `docs/tutorials/getting-started.md`.

---

## Run a single target

### Local directory

```bash
python -m usertest.cli run \
  --repo-root . \
  --repo "PATH/TO/TARGET" \
  --agent codex \
  --policy inspect
```

### Git URL

```bash
python -m usertest.cli run \
  --repo-root . \
  --repo "https://github.com/org/repo.git" \
  --agent codex \
  --policy inspect
```

---

## Choose a persona + mission

List built-ins:

```bash
python -m usertest.cli personas list --repo-root .
python -m usertest.cli missions list --repo-root .
```

Run with explicit IDs:

```bash
python -m usertest.cli run \
  --repo-root . \
  --repo "PATH_OR_GIT_URL" \
  --agent codex \
  --policy inspect \
  --persona-id burst_user \
  --mission-id produce_default_output
```

---

## Policies: safe vs inspect vs write

- Use `--policy safe` when you want the strictest mode.
- Use `--policy inspect` for most onboarding probes (read-only + shell).
- Use `--policy write` when you intentionally want edits.

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

```bash
python -m usertest.cli batch \
  --repo-root . \
  --targets examples/targets.yaml \
  --agent codex \
  --policy safe
```

Batch runs still produce per-target run directories; they’re just orchestrated from one command.

---

## Re-render a report without re-running

If you already have a run directory:

```bash
python -m usertest.cli report --repo-root . --run-dir "RUN_DIR"
```

To recompute metrics from the normalized events:

```bash
python -m usertest.cli report --repo-root . --run-dir "RUN_DIR" --recompute-metrics
```

---

## Use the Docker execution backend

The Docker backend is useful when you want:

- stronger isolation
- fewer host OS quirks (especially around shell commands)
- a more repeatable environment

```bash
python -m usertest.cli run \
  --repo-root . \
  --repo "PATH_OR_GIT_URL" \
  --agent codex \
  --policy inspect \
  --exec-backend docker
```

By default, Docker runs reuse host agent logins by mounting `~/.codex`, `~/.claude`, and/or
`~/.gemini`.

If you want API-key auth for Codex instead:

```bash
python -m usertest.cli run \
  --repo-root . \
  --repo "PATH_OR_GIT_URL" \
  --agent codex \
  --policy inspect \
  --exec-backend docker \
  --exec-use-api-key-auth \
  --exec-env OPENAI_API_KEY
```

---

## Usertesting a published package (fresh install)

To test the “fresh install” experience (instead of a repo checkout), use a `pip:` target.

Example (GitLab PyPI credentials are passed through as exec env vars):

```bash
python -m usertest.cli run \
  --repo-root . \
  --repo "pip:agent-adapters" \
  --agent codex \
  --policy safe \
  --exec-backend docker \
  --exec-env GITLAB_PYPI_PROJECT_ID \
  --exec-env GITLAB_PYPI_USERNAME \
  --exec-env GITLAB_PYPI_PASSWORD
```

See `docs/monorepo-packages.md` for details.

---

## Where outputs go

Run directories are written under:

`runs/usertest/<target>/<timestamp>/<agent>/<seed>/`

They contain rich evidence logs. Treat them as sensitive by default.
