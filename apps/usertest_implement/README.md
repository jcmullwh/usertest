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

---

## Usage

### Implement a specific ticket

From a ticket markdown file (for example in `.agents/plans/2 - ready/`):

```bash
usertest-implement run --ticket-path ".agents/plans/2 - ready/<ticket>.md"
```

Or from a tickets export JSON:

```bash
usertest-implement run --tickets-export runs/usertest/<target>/_compiled/<scope>.tickets_export.json --fingerprint <fp>
```

### Standard flow (refresh + implement next)

This is the recommended “just keep shipping” loop:

```bash
usertest-implement tickets run-next --backlog-target <target_slug>
```

It runs the backlog refresh steps via `usertest-backlog` (backlog → intent-snapshot → review-ux → export-tickets),
then selects the next local plan ticket (research-first) and runs it. Use `--no-refresh-backlog` for a fast path
that only selects from existing `.agents/plans/*` tickets.
