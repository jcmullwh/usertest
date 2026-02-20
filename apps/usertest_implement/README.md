# `usertest-implement` CLI

`usertest-implement` runs a coding agent to implement **one exported backlog ticket** in a target repo while
preserving the standard `runner_core` run artifacts plus ticket linkage artifacts (`ticket_ref.json`,
`timing.json`, and optionally git/push/PR metadata).

---

## Requirements

- Python 3.11+
- `git` (required for `--commit/--push`)
- Optional: GitHub CLI (`gh`) (required for `--pr`)
  - `gh` runs on the **host** (even when `--exec-backend docker` is used).
  - Ensure `gh` is on `PATH` and authenticated (`gh auth login`).
- Optional: `docker` (required for `--exec-backend docker`)

Quick checks:

```bash
git --version
gh --version
gh auth status
```

Install `gh` (examples):

- Windows: `winget install --id GitHub.cli`
- macOS: `brew install gh`
- Debian/Ubuntu: `sudo apt-get install gh`

If `gh` is installed but not found, ensure its install directory is on `PATH` (Windows default:
`C:\\Program Files\\GitHub CLI`).

## Install

From the monorepo root (editable install):

```bash
python -m pip install -r requirements-dev.txt
python -m pip install -e apps/usertest_implement
```

Confirm:

```bash
usertest-implement --help
```
