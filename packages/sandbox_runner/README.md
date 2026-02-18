# `sandbox_runner`

`sandbox_runner` provides a small abstraction for running commands inside a **sandboxed execution
environment**.

In this repo, it is primarily used to run agents inside Docker so runs are:

- more isolated from the host
- more repeatable across machines
- easier to constrain (mounts, resources, env allowlists)

The implementation is not tied to any specific agent or the `usertest` framework — it can be used for other sandboxed execution use cases.

---

## Install

Distribution name: `sandbox_runner`
Import package: `sandbox_runner`

### Standalone package checkout (recommended first path)

Run from this package directory:

```bash
pdm install
pdm run smoke
pdm run test
pdm run lint
```

If you need only a runtime install (without dev tooling commands), use:

```bash
python -m pip install -e .
```

From a private GitLab PyPI registry (snapshot publishing):

```bash
pip install \
  --index-url "https://<gitlab-host>/api/v4/projects/<project_id>/packages/pypi/simple" \
  --extra-index-url "https://pypi.org/simple" \
  "sandbox_runner==<version>"
```

Snapshot publishing status: `incubator` (see `docs/monorepo-packages.md`).

---

## Quickstart

Start a Docker sandbox and use its `command_prefix` to run commands “inside” the container.

```python
from pathlib import Path

from sandbox_runner import DockerSandbox, SandboxSpec

workspace = Path("/tmp/workspace")
artifacts = Path("/tmp/artifacts")

spec = SandboxSpec(backend="docker")
instance = DockerSandbox(workspace, artifacts, spec).start()
try:
    # instance.command_prefix is a list like:
    #   ["docker", "exec", "-i", "<container>", ...]
    # You can pass it to subprocess calls.
    print("Docker exec prefix:", instance.command_prefix)
finally:
    instance.close()
```

---

## Key types

- `SandboxSpec`
  - describes backend (`docker`) and image/build settings
- `MountSpec`
  - host → container mounts, with `read_only` support
- `ResourceSpec`
  - CPU/memory/pids limits
- `DockerSandbox`
  - Docker implementation that builds/starts a container and returns a `SandboxInstance`

---

## How it fits in the system

When `usertest run` is invoked with:

- `--exec-backend docker`

…the runner will:

1) create a workspace directory
2) start a `DockerSandbox`
3) run the agent CLI using the sandbox `command_prefix`
4) write run artifacts to the host run directory

---

## Development

### Standalone package checkout (recommended first path)

Run from this package directory:

```bash
pdm install
pdm run smoke
pdm run smoke_extended
pdm run test
pdm run lint
```

`pdm run smoke_extended` exercises Docker-backed smoke and skips with an explicit reason when Docker is unavailable.

### Monorepo contributor workflow

Run from the monorepo root:

```bash
python tools/scaffold/scaffold.py run install --project sandbox_runner
python tools/scaffold/scaffold.py run test --project sandbox_runner
python tools/scaffold/scaffold.py run lint --project sandbox_runner
```
