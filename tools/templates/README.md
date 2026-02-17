# Templates

This folder contains project templates used by `tools/scaffold/`.

Templates are not “runtime code” — they are scaffolds for new projects.

Examples:

- `python_pdm_lib/` – template for a PDM-managed library under `packages/`
- `python_pdm_app/` – template for a PDM-managed app under `apps/`

The scaffold tool renders these templates when you run:

```bash
python tools/scaffold/scaffold.py add <kind> <id> --generator <generator>
```

Generator registry:

- `tools/scaffold/registry.toml`

Trust model notes and vendoring helpers:

- `tools/scaffold/README.md`
