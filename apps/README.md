# Apps

Apps are **end-user facing** deliverables such as CLIs or web applications.

In this repo they are primarily CLIs:

- `apps/usertest` → `usertest` (run usertests)
- `apps/usertest_backlog` → `usertest-backlog` (compile/analyze/export backlog)
- `apps/usertest_implement` → `usertest-implement` (implement one exported ticket)

Each app is an independent Python project with its own `pyproject.toml`.

For setup options in this monorepo, see:

- `docs/tutorials/monorepo-setup.md`
