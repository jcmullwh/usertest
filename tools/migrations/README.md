# Migrations

This folder contains **one-off migration scripts** for on-disk formats.

These scripts are intentionally:

- stdlib-only
- dry-run by default
- explicit about what they will move/change

Treat migrations as operational tooling: run them only when you understand what will change.

---

## `migrate_runs_layout.py`

Migrates legacy run directories into the canonical layout:

`runs/usertest/…`

It plans moves from:

- `usertest/runs/*` → `runs/usertest/*`
- legacy `runs/*` (excluding `runs/usertest` and `runs/_cache`) → `runs/usertest/*`

### Dry-run

```bash
python tools/migrations/migrate_runs_layout.py
```

### Apply

```bash
python tools/migrations/migrate_runs_layout.py --apply
```

### Conflict handling

- `--rename-on-conflict`: rename incoming dirs by appending `__migrated_<N>`
- `--skip-existing`: keep existing destination dirs and skip conflicting sources

These are mutually exclusive.

---

## Safety notes

- Migrations operate within a single repo root.
- Moves are executed via `shutil.move` (rename when possible, copy+delete otherwise).
- Always run dry-run first and inspect the planned moves.
