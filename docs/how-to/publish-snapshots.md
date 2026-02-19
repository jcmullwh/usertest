# How to publish snapshot packages

This repo can publish **snapshot builds** of selected packages under `packages/` to a
**private GitLab PyPI registry**.

Snapshots are meant for internal consumption while APIs are evolving.

This guide is written for maintainers/operators.

For the deeper model (version computation, dependency rewriting), see `docs/monorepo-packages.md`.

---

## 1) Mark a package as publishable

Packages default to **internal** (not published).

To opt a package into snapshot publishing, add to `packages/<pkg>/pyproject.toml`:

```toml
[tool.monorepo]
status = "incubator"  # or "supported" | "stable"
```

Any `status != "internal"` is publishable.

---

## 2) Run the publisher locally (dry-run)

Install publish dependencies:

```bash
python -m pip install -r tools/requirements-publish.txt
```

Self-test:

```bash
python tools/monorepo_publish/publish_snapshots.py --self-test
```

Preview what would be published:

```bash
python tools/monorepo_publish/publish_snapshots.py --dry-run
```

---

## 3) Publish via GitHub Actions

The workflow is:

- `.github/workflows/publish-snapshots.yml`

On every push to `main`, it runs a **safe validation-only** path (self-test + build + artifact scan)
and does **not** upload anything.

To actually publish snapshots, trigger the workflow manually via `workflow_dispatch`. The manual
path runs the same validation step and then performs a live publish (requires explicit confirmation
in the command via `--confirm-live-publish`).

The live publish job requires these secrets:

- `GITLAB_BASE_URL`
- `GITLAB_PYPI_PROJECT_ID`
- `GITLAB_PYPI_USERNAME`
- `GITLAB_PYPI_PASSWORD`

---

## 4) Install from the private registry

Consumers can install using the GitLab “simple index” URL, for example:

```bash
pip install \
  --index-url "https://<gitlab-host>/api/v4/projects/<project_id>/packages/pypi/simple" \
  --extra-index-url "https://pypi.org/simple" \
  normalized_events==<version>
```

See `docs/monorepo-packages.md` for pip and PDM examples.

---

## Notes on dependency rewriting

Published snapshot wheels **cannot** contain local-path dependencies like `file://…`.

The publisher rewrites monorepo-internal dependencies so packages published together reference the
same snapshot version.

If you see publish failures about local paths, the package probably still has a `file://…`
dependency that wasn’t eligible for rewrite.
