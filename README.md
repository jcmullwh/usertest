# Agentic usertest monorepo

[![CI](https://github.com/jcmullwh/usertest/actions/workflows/ci.yml/badge.svg)](https://github.com/jcmullwh/usertest/actions/workflows/ci.yml)

This repo contains **usertest**, a runner for repeatable “agentic usertests” and **usertest-backlog**, a backlog miner. We aim to solve two core problems:

1. The only way to actually test software is to have users use it. Developers (agent and human) have great ideas about how
well their software works, but those ideas are just guesses until they are put into contact with users.
2. User feedback/suggestions/input is often noisy, overlapping, and sometimes contradictory, making it difficult to aggregate and act upon.

**usertest**

You point it at a target repo (local path or git URL), choose a **persona** and **mission**.
A headless CLI agent then acts as the persona and attempts the mission. The run produces:

- a schema-validated (`report.json`) and human-readable (`report.md`) agent report covering challenges, confusion points, and improvement suggestions.
- a tool transcript (`normalized_events.jsonl`) and trace-derived metrics (`metrics.json`) on agent behavior and performance.

**usertest-backlog**

The backlog CLI provides tools to mine and analyze data produced by usertest, generates synthetic backlog items from that data, and helps export those items into target issue trackers. It:

- translates usertest run histories (report + evidence) into individual component "atoms".
- mines the atoms to propose backlog items.
- merges and deduplicates those items into a structured backlog.
- optionally exports those items into a target issue tracker.

**usertest-implement**

Once we have all of those target issues, we need to implement them.

- Use the same mechanisms as usertest and usertest-backlog to generate a PR.
- Track metrics on the implementers to identify rising complexity and tech debt.

## Start here

- **Docs hub:** `docs/README.md`
- **Tutorial:** `docs/tutorials/getting-started.md`
- **Monorepo setup + scaffold workflow:** `docs/tutorials/monorepo-setup.md`
- **One-command smoke (per OS):** `scripts/smoke.ps1` (Windows) / `scripts/smoke.sh` (macOS/Linux)

## Fastest output (no setup)

Open the checked-in golden fixture artifacts directly (no Python deps required):

- `examples/golden_runs/minimal_codex_run/report.md`
- `examples/golden_runs/minimal_codex_run/metrics.json`

### One-command "from source" verification

If you haven't set up a Python environment for this repo yet, use the one-command scripts to verify everything is working. They create a local `.venv`, install minimal dependencies, configure `PYTHONPATH`, and render a report from a golden fixture. These scripts do **not** execute any agents or make network calls:

- **Windows PowerShell:**
  ```powershell
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline_first_success.ps1
  ```
- **macOS / Linux:**
  ```bash
  bash ./scripts/offline_first_success.sh
  ```

### Running from source (manual)

If you prefer to manage your environment manually, you must install dependencies and configure `PYTHONPATH` for this monorepo.

1. **Install minimal dependencies:**
   `python -m pip install -r requirements-dev.txt`
2. **Set PYTHONPATH:**
   - **Windows PowerShell:** `. .\scripts\set_pythonpath.ps1`
   - **macOS / Linux:** `source scripts/set_pythonpath.sh`
3. **Verify:**
   `python -m usertest.cli --help`

Success signal: the command prints help output.

## Repo structure

This is a **monorepo** managed by `tools/scaffold/scaffold.py` (manifest-driven task runner and project generator). It is intentionally structured to facilitate iteration, experimentation, and evolution of the overarching project, particularly by agentic contributors.

There are three main “kinds” in this specific project:

- `apps/` – **end-user** deliverables (CLIs)
  - `apps/usertest` → `usertest`
  - `apps/usertest_backlog` → `usertest-backlog`
- `packages/` – reusable libraries (can be consumed outside this repo)
  - some packages are snapshot-published to a **private** registry (see below)
- `tools/` – internal repo tooling (scaffold, publishing, migrations, lint helpers)

The monorepo is managed by `tools/scaffold/scaffold.py` (manifest-driven task runner and project generator). CI uses the scaffold manifest to generate its job matrix.

## Quickstart

### Requirements

- Python 3.11+ (CI currently runs 3.11; newer versions are best-effort)
- `git`
- Optional: GitHub CLI (`gh`) (needed for `usertest-implement run --pr`)
- At least one of: agent CLIs on PATH + credentials
  - `codex` CLI (logged in via `codex login` / subscription)
  - `claude` CLI (Claude Code)
  - `gemini` CLI (Gemini CLI)
- Optional: `pdm` (for the scaffold-based install flow)
- Optional: `docker` (for the Docker execution backend)

### Step 0: doctor

Run this first in any setup path:

`python tools/scaffold/scaffold.py doctor`

Convenience wrappers:

- Windows PowerShell: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\doctor.ps1`
- macOS/Linux: `bash ./scripts/doctor.sh`

### One copy-paste smoke command per OS

These commands are self-contained (no implicit prior shell state) and enforce non-zero exit on
failure.

Windows PowerShell:

`powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1`

macOS/Linux:

`bash ./scripts/smoke.sh`

Note: on some Windows sandboxes, `bash.exe` may be on `PATH` (for example via Git for Windows) but
execution is blocked ("Access is denied"). In that case, use the PowerShell smoke command above
and avoid bash-based validation steps.

The smoke scripts run:

1. `python tools/scaffold/scaffold.py doctor`
2. dependency install
3. `python -m usertest.cli --help`
4. `python -m usertest_backlog.cli --help`
5. `python -m pytest -q apps/usertest/tests/test_smoke.py apps/usertest/tests/test_golden_fixture.py apps/usertest_backlog/tests/test_smoke.py`

If `pdm` is not installed, the scripts run doctor with tool checks skipped (`scaffold.py doctor --skip-tool-checks`)
and continue with the pip-based flow. For CI or strict preflight runs, require doctor explicitly:

- PowerShell: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -RequireDoctor`
- macOS/Linux: `bash ./scripts/smoke.sh --require-doctor`

Fallback mode if you want PYTHONPATH-based execution instead of editable installs:

- PowerShell: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -UsePythonPath`
- macOS/Linux: `bash ./scripts/smoke.sh --use-pythonpath`

Restricted environments (no editable installs / pre-provisioned deps):

- No editable installs (still installs `requirements-dev.txt`): use the PYTHONPATH modes above.
- No installs at runtime (deps already provisioned, e.g., offline wheelhouse): run smoke with both flags:
  - PowerShell: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -SkipInstall -UsePythonPath`
  - macOS/Linux: `bash ./scripts/smoke.sh --skip-install --use-pythonpath`
- If you pass only `--skip-install` (without `--use-pythonpath`), smoke assumes your environment already has the
  monorepo packages installed and importable. Note: `--skip-install` skips *all* installs (including
  `requirements-dev.txt`); smoke will run an import preflight and fail fast with actionable setup guidance if imports
  are not available.
- Manual repro (preflight UX): in a fresh environment without installs, run `bash ./scripts/smoke.sh --skip-install`
  (or PowerShell `.\scripts\smoke.ps1 -SkipInstall`) and confirm the first failure signal is the preflight guidance
  (not a Python stack trace from later CLI/pytest imports).

### Manual editable install (no PYTHONPATH)

Run these in an activated virtual environment (otherwise pip may default to user-site install):

`python -m pip install -r requirements-dev.txt`

Primary path (plain editable app install):

`python -m pip install -e apps/usertest`

Fallback path (explicit local editable bootstrap):

`python -m pip install --no-deps -e packages/normalized_events -e packages/agent_adapters -e packages/run_artifacts -e packages/reporter -e packages/sandbox_runner -e packages/runner_core -e packages/triage_engine -e packages/backlog_core -e packages/backlog_miner -e packages/backlog_repo -e apps/usertest -e apps/usertest_backlog`

Why both paths exist:

- plain `-e apps/usertest` is the default local-dev path
- explicit `--no-deps -e ...` gives deterministic, explicit control over each local editable source

### Source-run fallback (PYTHONPATH)

Use this path if you intentionally do not install editables:

Windows PowerShell:

`. .\scripts\set_pythonpath.ps1`

macOS/Linux:

`source scripts/set_pythonpath.sh`

## Run a single target

After install (editable or PYTHONPATH), run:

`python -m usertest.cli run --repo-root . --repo "PATH_OR_GIT_URL_OR_DIR" --agent codex --policy write --persona-id quickstart_sprinter --mission-id first_output_smoke`

Local directory example (initializes `.usertest/` scaffold):

`python -m usertest.cli init-usertest --repo-root . --repo "PATH_TO_LOCAL_DIR"`

Then run against that directory (requires an agent CLI + credentials):

`python -m usertest.cli run --repo-root . --repo "PATH_TO_LOCAL_DIR" --agent codex --policy write --persona-id quickstart_sprinter --mission-id first_output_smoke`

List built-in personas/missions:

`python -m usertest.cli personas list --repo-root .`

`python -m usertest.cli missions list --repo-root .`

## Backlog CLI

Backlog mining/inclusion commands are provided by the separate backlog CLI app:

`python -m usertest_backlog.cli --help`

Defaults are configured in `configs/catalog.yaml`.

Example: quick output with defaults-first mission:

`python -m usertest.cli run --repo-root . --repo "PATH_OR_GIT_URL" --agent codex --policy write --persona-id burst_user --mission-id produce_default_output`

Claude Code variant:

`python -m usertest.cli run --repo-root . --repo "PATH_OR_GIT_URL" --agent claude --policy write --persona-id quickstart_sprinter --mission-id first_output_smoke`

Gemini variant:

`python -m usertest.cli run --repo-root . --repo "PATH_OR_GIT_URL" --agent gemini --policy write --persona-id quickstart_sprinter --mission-id first_output_smoke`

### Evaluate a published Python package (fresh install)

To usertest a deployed Python package (fresh install into an isolated virtualenv before the agent
runs), pass a pip target:

`python -m usertest.cli run --repo-root . --repo "pip:agent-adapters" --agent codex --policy write --persona-id quickstart_sprinter --mission-id first_output_smoke --exec-backend docker`

This repo provides support for private registries(GitLab PyPI in particular); in that case also set the additionaly flags below with environment variables and optionally `GITLAB_BASE_URL`. For details, see `docs/monorepo-packages.md`.`

`python -m usertest.cli run --repo-root . --repo "pip:agent-adapters" --agent codex --policy write --persona-id quickstart_sprinter --mission-id first_output_smoke --exec-backend docker --exec-env GITLAB_PYPI_PROJECT_ID --exec-env GITLAB_PYPI_USERNAME --exec-env GITLAB_PYPI_PASSWORD`

Notes:

- The runner writes install artifacts to `bootstrap_pip.log`, `bootstrap_pip.json`, and
  `bootstrap_pip_list.json` in the run dir.
- For self-hosted GitLab, also set `GITLAB_BASE_URL`.
- For GitLab PyPI consumption details, see `docs/monorepo-packages.md`.

Execution-policy notes:

- Execution policies apply to agent tool permissions during `run`/`batch`; host-side CLI commands
  such as `python -m usertest.cli --help` are unaffected.
- `--policy safe` is strictest (no writes; and for Claude/Gemini, no shell commands).
- `--policy inspect` is read-only but allows shell commands (recommended for first-success probing
  workflows on Claude/Gemini).
- Built-in `first_output_smoke` / `produce_default_output` missions require edits; use `--policy write` for those runs.
- Which policy should I use?
  - Read-only + shell (no edits): `--policy inspect`
  - Any workflow that requires edits: `--policy write`
  - Claude/Gemini with *no shell commands at all*: `--policy safe`
  - Common missions:
    - `privacy_locked_run`: `--policy inspect`
    - `first_output_smoke`: `--policy write`
    - `produce_default_output`: `--policy write`
- If you need repo-specific tool probes, add `--preflight-command <CMD>` (repeatable) and optional
  `--require-preflight-command <CMD>`.
- If you want a required “pre-handoff” CI/test gate, add `--verify-command "<SHELL_CMD>"` (repeatable) and optional
  `--verify-timeout-seconds <SECONDS>`. The runner can schedule follow-up attempts to fix failures before handing off.
- `preflight.json` includes per-command diagnostics with status values: `present`, `missing`, and
  `blocked_by_policy`.
- `USERS.md` is optional context; built-in prompt templates no longer require it.

Artifacts land under `runs/usertest/<target>/<timestamp>/<agent>/<seed>/`:

- `target_ref.json`, `prompt.txt`
- `effective_run_spec.json`, `prompt.template.md`, `report.schema.json`
- `persona.source.md`, `persona.resolved.md`, `mission.source.md`, `mission.resolved.md`
- `raw_events.jsonl`, `normalized_events.jsonl`
- `metrics.json`
- `report.json`, `report.md`
- `verification.json` (when `--verify-command` is used)
- `patch.diff` (only if writes were allowed and edits occurred)

For the full file-level contract (required vs optional files, semantics, and a redacted sample
layout), see `docs/design/run-artifacts.md`. For offline fixtures, see `examples/golden_runs/`.

### Docker execution backend (optional)

`python -m usertest.cli run --repo-root . --repo "PATH_OR_GIT_URL" --agent codex --policy write --exec-backend docker`

Docker runs default to:

- outbound network enabled (`--exec-network open`)
- host agent login reuse enabled (`~/.codex`, `~/.claude`, `~/.gemini` mounts via `--exec-use-host-agent-login`)

If you want API-key auth instead, opt in explicitly with:

`--exec-use-api-key-auth --exec-env OPENAI_API_KEY`

Note: the agent CLI itself runs *inside* the Docker container in this repo. Setting
`--exec-network none` will prevent Codex/Claude/Gemini from reaching their hosted APIs, so it is
not a “privacy-locked agent run” mode. For a no-network / no-credentials first success signal, use
the golden fixtures in “Fastest output (no setup)” above.

If you need to override the Docker build context, pass `--exec-docker-context` explicitly (default:
the built-in sandbox_cli context shipped with `sandbox_runner`).

Optional flags:

- `--exec-docker-timeout-seconds <SECONDS>` to bound Docker CLI operations.
- `--exec-docker-python context|auto|<VERSION>` to control sandbox Python base image.
- `--exec-use-target-sandbox-cli-install` to merge a target repo's
  `.usertest/sandbox_cli_install.yaml` into Docker image overlays.

Sandbox metadata/log artifacts are written under `<run_dir>/sandbox/`.

Docker smoke test behavior when Docker is unavailable:

- `packages/sandbox_runner/tests/test_docker_smoke.py` skips with an explicit reason (for example
  `docker not on PATH` or `docker version timed out`).
- Show skip reasons with: `python -m pytest -q -rs packages/sandbox_runner/tests/test_docker_smoke.py`

### Re-render a report for an existing run

`python -m usertest.cli report --repo-root . --run-dir "RUN_DIR"`

Golden fixture verification command:

`python -m pytest -q apps/usertest/tests/test_golden_fixture.py`

### Batch

`python -m usertest.cli batch --repo-root . --targets examples/targets.yaml --agent codex --policy safe`

## Configuration

- `configs/agents.yaml`: how to invoke each agent CLI (`codex`, `claude`, `gemini`)
- `configs/policies.yaml`: policy mappings (`safe`, `inspect`, `write`)
- `configs/catalog.yaml`: persona/mission/template/schema discovery + defaults
- `configs/report_schemas/*.schema.json`: JSON schemas selected per mission

## Troubleshooting

- If `usertest` is "command not found" / not on PATH, either:
  - run via module invocation (after installing deps): `python -m usertest.cli --help`, or
  - install the console script: `python -m pip install -e apps/usertest`
- If Codex fails with `model_reasoning_effort` enum errors, use one of
  `minimal|low|medium|high` (example: `--agent-config model_reasoning_effort=high`).
- If preflight reports `blocked_by_policy`, switch to `--policy inspect` (read-only + shell) or
  update `configs/policies.yaml`.
- If you're on Windows and `python`/`python3` resolves to a WindowsApps alias (for example
  `...\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe`) and spawning Python fails (often
  `Access is denied`), install/select a full CPython interpreter and ensure it takes precedence
  over WindowsApps on PATH (or disable the "App execution aliases" for Python in Windows
  Settings). When `--verify-command` uses pytest, the runner fails fast with actionable details in
  `preflight.json` (`command_diagnostics`, `python_runtime`, `pytest_probe`) and `error.json`
  (`python_unavailable` / `pytest_unavailable`).
- If you use a Windows-host checkout inside WSL or a Linux container and `git status` shows
  widespread unrelated modifications, it is usually a CRLF/LF line ending mismatch. Mitigations:
  - For new clones: `git config --global core.autocrlf input`
  - For existing clones (after stashing real edits): `git add --renormalize .`
- Windows workaround for bash assumptions:
  - Prefer Docker backend for shell-capable runs:
    `python -m usertest.cli run --repo-root . --repo "PATH_OR_GIT_URL" --agent gemini --policy inspect --exec-backend docker`
  - If shell commands are not required, run `--policy safe` to avoid bash/tool-call noise.
  - Cosmetic vs blocking: cosmetic means run exits `0` with report artifacts; blocking means
    preflight failure (`error.json` subtype like `policy_block` / `mission_requires_shell`) or
    non-zero agent exit.
- If Gemini fails with `GEMINI_SANDBOX is true but failed to determine command for sandbox` while
  using `--exec-backend docker`, update to a version that disables Gemini's nested sandbox.
- If a target repo ships `agents.md`/`AGENTS.md` and you want a more neutral evaluation, pass
  `--obfuscate-agent-docs`.
- Runner outputs default to `runs/usertest/` (legacy paths are not auto-deleted). To migrate:
  `python tools/migrations/migrate_runs_layout.py` then
  `python tools/migrations/migrate_runs_layout.py --apply`.
- To create a shareable snapshot ZIP of this repo:
  `python tools/snapshot_repo.py --out repo_snapshot.zip`
  - `.gitignore` files are excluded by default; pass `--include-gitignore-files` to include them.
  - If the output already exists, pass `--overwrite`.
- If a run fails mid-acquisition or disk usage grows, delete `runs/usertest/_workspaces/`.
- If you change `packages/runner_core`, `packages/agent_adapters`, or `packages/reporter` and the
  CLI still behaves like old code, refresh the CLI env:
  `python tools/scaffold/scaffold.py run install --project cli`.

Windows coverage is enforced in CI by `.github/workflows/ci.yml` jobs
`windows_scaffold_smoke` and `windows_script_smoke`.

## Security

Operational security/runbook notes live under `.agents/ops/`.

Key points:

- Run policies constrain agent tool permissions during `run`/`batch`; they do not sanitize run
  artifacts globally.
- Run artifacts (`prompt.txt`, `raw_events.jsonl`, `normalized_events.jsonl`, `report.*`) may
  contain sensitive data.
- `.env`-style files are not automatically excluded from target workspace acquisition; treat targets
  and artifacts as sensitive by default.

For details, see `.agents/ops/security.md`.

## Snapshot publishing (private registry)

Packages under `packages/` can be snapshot-published to a private GitLab PyPI registry.

- How it works: `docs/monorepo-packages.md`
- Operator workflow: `docs/how-to/publish-snapshots.md`

## License

MIT (see `LICENSE`).
