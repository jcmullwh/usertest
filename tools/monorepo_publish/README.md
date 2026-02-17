# `monorepo_publish`

This tool publishes **snapshot builds** of eligible Python packages under `packages/` to a
**private GitLab PyPI registry**.

It exists because:

- packages in this monorepo often depend on each other via local paths during development
- published wheels cannot contain `file://…` path dependencies
- we want reproducible, uniquely-versioned snapshots for integration testing

User-facing docs:

- `docs/monorepo-packages.md` (model and consumption)
- `docs/how-to/publish-snapshots.md` (operator workflow)

---

## How eligibility works

Each package can opt into publishing by setting:

```toml
[tool.monorepo]
status = "incubator"  # or "supported" | "stable"
```

Packages default to `internal` (not published).

---

## CLI usage

From the repo root:

### Self-test

```bash
python tools/monorepo_publish/publish_snapshots.py --self-test
```

### Dry-run (compute versions + validate rewrites)

```bash
python tools/monorepo_publish/publish_snapshots.py --dry-run
```

### Live publish

Live publishing requires explicit confirmation:

```bash
python tools/monorepo_publish/publish_snapshots.py --confirm-live-publish
```

Environment variables required for live publish:

- `GITLAB_PYPI_PROJECT_ID`
- `GITLAB_PYPI_USERNAME`
- `GITLAB_PYPI_PASSWORD`
- optional: `GITLAB_BASE_URL` (defaults to `https://gitlab.com`)

---

## What it does

At a high level:

1) discovers eligible packages under `packages/`
2) computes a snapshot version
3) rewrites monorepo-internal dependencies to that snapshot version
4) builds wheels/sdists
5) uploads them to the registry (only when confirmed)

The publishing hook used in CI is:

- `.github/workflows/publish-snapshots.yml`
