# `sandbox_cli` Docker context

This is a built-in Docker image context intended to be used with the runner's Docker execution
backend (`--exec-backend docker`) and the `usertest` CLI.

It installs a small set of base tools via simple manifests so you can customize the image by
editing text files rather than rewriting the Dockerfile.

## What this context includes

- Base image: `python:3.11-slim`
- APT packages listed in `manifests/apt.txt` (e.g., `git`, `gh`, `curl`, `ripgrep`, build tools)
- Optional `pip` packages from `manifests/pip.txt`
- Optional global `npm` packages from `manifests/npm-global.txt` (only runs if `npm` exists)

## What this context intentionally does not handle

- Secrets/auth: host agent login mounts are the default (`--exec-use-host-agent-login` semantics).
  To opt into API-key mode, pass `--exec-use-api-key-auth` and allowlist env vars with
  `--exec-env KEY` (e.g. `OPENAI_API_KEY`).
- Installing specific agent CLIs by default (Codex/Claude/Gemini): this image stays generic.

  For docker runs, the runner can optionally inject a per-run overlay manifest (under
  `overlays/manifests/`) based on `configs/agents.yaml` -> `sandbox_cli_install` for the selected
  `--agent`.

## Build

From the repo root:

```sh
docker build -t sandbox-cli-test -f packages/sandbox_runner/src/sandbox_runner/builtins/docker/contexts/sandbox_cli/Dockerfile packages/sandbox_runner/src/sandbox_runner/builtins/docker/contexts/sandbox_cli
```

## Use with usertest

```sh
PYTHONPATH=apps/usertest/src:packages/runner_core/src:packages/agent_adapters/src:packages/normalized_events/src:packages/reporter/src:packages/sandbox_runner/src \
  python -m usertest.cli run \
  --repo-root . \
  --repo "<PATH_OR_GIT_URL>" \
  --agent codex \
  --policy inspect \
  --exec-backend docker
```

If the agent CLI is already logged in on the host, Docker runs will reuse `~/.codex`,
`~/.claude`, or `~/.gemini` inside the container by default. To use API-key mode instead,
pass `--exec-use-api-key-auth` plus appropriate `--exec-env ...` values.

Run artifacts will include sandbox metadata and container logs under `<run_dir>/sandbox/`.
