# `sandbox_cli_maintenance` Docker context

This context is the maintenance-specific companion to the generic `sandbox_cli` image. It is not
intended for arbitrary external target repos. It exists so same-repo `usertest-implement` Docker
runs can start from a preinstalled monorepo maintenance environment instead of reinstalling every
project from scratch.

The generated maintenance build context starts from the normal `sandbox_cli` manifests and scripts,
adds the union of all configured agent CLI installs from `configs/agents.yaml`, copies a curated
snapshot of this monorepo into `/workspace`, runs:

- `python -m pip install -r requirements-dev.txt`
- `python tools/scaffold/scaffold.py run install --all --skip-missing`

Then it fingerprints each project with `tools/scaffold/install_cache_fingerprint.py` and moves the
resulting `.venv` directories into `/opt/usertest_maint_seed/<project>/<fingerprint>/venv`.

The runtime maintenance profile can then:

- mount matching host cache hits directly into `/workspace/<project>/.venv`
- or copy a matching seeded `.venv` from `/opt/usertest_maint_seed` on cache miss

This context is generated per environment hash and is meant to be used through the maintenance
profile plumbing in `runner_core.execution_backend`, not built manually by hand.
