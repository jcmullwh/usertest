# Sandbox overlays (repo-owned)

This folder is for **repo-/project-specific** sandbox configuration overlays.

Examples of things that belong here:
- Default overlay manifests for `sandbox_cli` (APT/pip/npm) that vary by environment.
- Templates or docs for how to structure overlays used by `runner_core.execution_backend`.

Generated, per-run overlay files should live under `runs/` (not here).
