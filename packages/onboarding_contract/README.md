# onboarding_contract

Internal shared contract for newcomer-first command guidance in this monorepo.

### Standalone package checkout (recommended first path)

Run from this package directory:

```bash
pdm install
pdm run smoke
pdm run test
pdm run lint
```

This package is intentionally small and dependency-free. It exists to keep newcomer-first command
guidance consistent between the repo root onboarding docs, the app CLIs, and scaffolded projects.

### Monorepo contributor workflow

Run from the monorepo root:

- Run tests: `python tools/scaffold/scaffold.py run test --project onboarding_contract`
- Run lint: `python tools/scaffold/scaffold.py run lint --project onboarding_contract`
