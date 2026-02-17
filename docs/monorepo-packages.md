# Monorepo Python packages: snapshot publishing and consumption

This repo can publish snapshot builds of selected Python packages under `packages/` to a GitLab
PyPI registry. Snapshots are for internal consumption while packages are still evolving.

Related guides:

- Monorepo mental model + scaffold: `docs/tutorials/monorepo-setup.md`
- Operator workflow: `docs/how-to/publish-snapshots.md`

## Maintainers: marking packages as publishable

By default, packages are treated as `internal` and are not published.

To opt a package into snapshot publishing, add this to `packages/<pkg>/pyproject.toml`:

```toml
[tool.monorepo]
status = "incubator" # or "supported" | "stable"
```

Allowed statuses:
- `internal`: never publish
- `incubator`: publish snapshots
- `supported`: publish snapshots
- `stable`: publish snapshots

Only `status != "internal"` is publishable.

## Snapshot behavior

Snapshot publishing:
- publishes uniquely versioned PEP 440 dev releases on every push to `main`
- rewrites internal monorepo dependencies to `==<same snapshot version>` instead of `file://` paths

Snapshot publishing does not:
- create a curated release contract
- publish packages not opted in via `[tool.monorepo].status`

## How snapshot versions are computed

The publisher computes a numeric `snapshot_id` and converts each package base version to:

`<base_release>.dev<snapshot_id>`

Example: `0.1.0 -> 0.1.0.dev735192004101`

Snapshot ID priority:
1. `MONOREPO_SNAPSHOT_ID` (if numeric)
2. GitHub Actions: `GITHUB_RUN_ID * 100 + GITHUB_RUN_ATTEMPT` (attempt defaults to `1`)
3. `CI_PIPELINE_ID` (if numeric)
4. `int(time.time())`

## Troubleshooting

Common failures:
- Invalid base version: `project.version` must be PEP 440 compatible.
- Local path dependency: published packages cannot depend on `file://...` requirements.
- Auth failures: verify `GITLAB_PYPI_USERNAME` and `GITLAB_PYPI_PASSWORD` with required scopes.
- Publish conflicts (`file already exists`): rerun workflow so snapshot id changes.

## Consumers: installing from GitLab

GitLab simple index URL:

`https://<gitlab-host>/api/v4/projects/<project_id>/packages/pypi/simple`

### pip

With forwarding enabled:

`pip install --index-url "https://<gitlab-host>/api/v4/projects/<project_id>/packages/pypi/simple" normalized-events==<version>`

With forwarding disabled (recommended explicit fallback):

`pip install --index-url "https://<gitlab-host>/api/v4/projects/<project_id>/packages/pypi/simple" --extra-index-url "https://pypi.org/simple" normalized-events==<version>`

Example import check:

`python -c "import normalized_events; print(normalized_events.__version__)"`

### PDM

In a consuming repo `pyproject.toml`:

```toml
[[tool.pdm.source]]
name = "gitlab"
url = "https://<gitlab-host>/api/v4/projects/<project_id>/packages/pypi/simple"
include_packages = [
  "normalized-events*",
  "runner-core*",
  "agent-adapters*",
  "reporter*",
  "sandbox-runner*",
]
```

Then install normally:

`pdm add normalized-events==<version>`

## usertest: evaluating installed artifacts

For a fresh-install usertest, use a package target:

`python -m usertest.cli run --repo-root . --repo "pip:agent-adapters==<version>" --agent codex --policy safe --persona-id quickstart_sprinter --mission-id first_output_smoke --exec-backend docker --exec-env GITLAB_PYPI_PROJECT_ID --exec-env GITLAB_PYPI_USERNAME --exec-env GITLAB_PYPI_PASSWORD`

PDM-based package target:

`python -m usertest.cli run --repo-root . --repo "pdm:agent-adapters==<version> normalized-events==<version>" --agent codex --policy safe --persona-id quickstart_sprinter --mission-id first_output_smoke --exec-backend docker --exec-env GITLAB_PYPI_PROJECT_ID --exec-env GITLAB_PYPI_USERNAME --exec-env GITLAB_PYPI_PASSWORD`

This creates a synthetic workspace, installs package requirements into an isolated virtualenv, and
runs the selected agent in that environment.

Runner resilience controls:
- `--agent-rate-limit-retries` (default `2`)
- `--agent-rate-limit-backoff-seconds` (default `1.0`)
- `--agent-rate-limit-backoff-multiplier` (default `2.0`)
- `--agent-followup-attempts` (default `2`)

Codex authentication in Docker is explicit:
- Host login mount mode is the default (`~/.codex` via `--exec-use-host-agent-login` semantics).
- API-key mode is opt-in: pass `--exec-use-api-key-auth --exec-env OPENAI_API_KEY` and set
  `OPENAI_API_KEY` on the host.
