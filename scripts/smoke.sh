#!/usr/bin/env bash
set -euo pipefail

# Stable smoke contract:
# 1) scaffold doctor
# 2) install path (editable by default, PYTHONPATH fallback optional)
# 3) CLI help
# 4) smoke tests
# Exit is non-zero on first failure.

SKIP_INSTALL=0
USE_PYTHONPATH=0
REQUIRE_DOCTOR=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-install)
      SKIP_INSTALL=1
      shift
      ;;
    --use-pythonpath)
      USE_PYTHONPATH=1
      shift
      ;;
    --require-doctor)
      REQUIRE_DOCTOR=1
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: scripts/smoke.sh [--skip-install] [--use-pythonpath] [--require-doctor]" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

PYTHON_BIN="python"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  else
    echo "Neither 'python' nor 'python3' is available on PATH." >&2
    exit 1
  fi
fi

print_setup_hint() {
  echo "==> Setup hint"
  echo "    Choose a setup mode:"
  echo "      - Default (recommended): installs deps + editable installs"
  echo "          bash ./scripts/smoke.sh"
  echo "      - From-source: installs deps + sets PYTHONPATH (no editables)"
  echo "          bash ./scripts/smoke.sh --use-pythonpath"
  echo "      - No-install: assumes deps + local packages are already importable"
  echo "          bash ./scripts/smoke.sh --skip-install  # (often combined with --use-pythonpath)"
  if [[ -z "${VIRTUAL_ENV:-}" && -z "${CI:-}" ]]; then
    echo "    Recommended venv:"
    echo "      ${PYTHON_BIN} -m venv .venv"
    echo "      source .venv/bin/activate  # or: source .venv/Scripts/activate (Git Bash)"
  fi
}

PIP_FLAGS=(--disable-pip-version-check --retries 10 --timeout 30)

print_skip_install_guidance() {
  echo "    You passed --skip-install, so this script will not run any installs." >&2
  echo "    That means it will NOT install requirements-dev.txt and it will NOT install local monorepo packages." >&2
  echo "" >&2
  echo "    Choose one setup mode:" >&2
  echo "      - Default (recommended for dev):" >&2
  echo "          bash ./scripts/smoke.sh" >&2
  echo "      - From-source (no editables, but installs deps):" >&2
  echo "          bash ./scripts/smoke.sh --use-pythonpath" >&2
  echo "      - No-install (deps already provisioned):" >&2
  echo "          bash ./scripts/smoke.sh --skip-install --use-pythonpath" >&2
  echo "" >&2
  echo "    Tip: prefer a virtualenv to avoid global/user-site installs:" >&2
  echo "      ${PYTHON_BIN} -m venv .venv && source .venv/bin/activate" >&2
}

if [[ "${REQUIRE_DOCTOR}" -eq 1 ]]; then
  if ! command -v pdm >/dev/null 2>&1; then
    echo "Scaffold doctor required but pdm was not found on PATH." >&2
    echo "Install pdm (recommended): ${PYTHON_BIN} -m pip install -U pdm" >&2
    echo "Or rerun without --require-doctor." >&2
    exit 1
  fi
  echo "==> Scaffold doctor"
  "${PYTHON_BIN}" tools/scaffold/scaffold.py doctor
else
  echo "==> Scaffold doctor (tool checks skipped; pdm optional)"
  echo "    Note: pdm is optional; continuing with the pip-based flow."
  echo "    To enable tool checks: ${PYTHON_BIN} -m pip install -U pdm"
  echo "    To require doctor: bash ./scripts/smoke.sh --require-doctor"
  "${PYTHON_BIN}" tools/scaffold/scaffold.py doctor --skip-tool-checks
fi

print_setup_hint

if [[ "${SKIP_INSTALL}" -eq 0 ]]; then
  if command -v id >/dev/null 2>&1; then
    if [[ "$(id -u)" -eq 0 ]]; then
      if [[ -z "${VIRTUAL_ENV:-}" ]]; then
        echo "==> Note: running as root without an active virtualenv; pip installs may land in system site-packages"
        echo "    Recommended:"
        echo "      python -m venv .venv"
        echo "      source .venv/bin/activate"
      fi
    fi
  fi

  echo "==> Install base Python deps"
  "${PYTHON_BIN}" -m pip install "${PIP_FLAGS[@]}" -r requirements-dev.txt

  if [[ "${USE_PYTHONPATH}" -eq 1 ]]; then
    echo "==> Configure PYTHONPATH via scripts/set_pythonpath.sh"
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/set_pythonpath.sh"
  else
    echo "==> Install monorepo packages (editable, no deps)"
    # --no-deps avoids duplicate direct-reference resolver conflicts between local packages.
    "${PYTHON_BIN}" -m pip install --no-deps \
      -e packages/normalized_events \
      -e packages/agent_adapters \
      -e packages/run_artifacts \
      -e packages/reporter \
      -e packages/sandbox_runner \
      -e packages/runner_core \
      -e packages/triage_engine \
      -e packages/backlog_core \
      -e packages/backlog_miner \
      -e packages/backlog_repo \
      -e apps/usertest \
      -e apps/usertest_backlog \
      -e apps/usertest_implement
  fi
elif [[ "${USE_PYTHONPATH}" -eq 1 ]]; then
  echo "==> Configure PYTHONPATH via scripts/set_pythonpath.sh"
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/set_pythonpath.sh"
fi

guard_import_origin() {
  local guard_rc=0
  local guard_out=""
  guard_out="$("${PYTHON_BIN}" tools/smoke_import_guard.py --repo-root "${REPO_ROOT}" 2>&1)" || guard_rc=$?
  printf '%s\n' "${guard_out}"
  return "${guard_rc}"
}

echo "==> Import-origin guard smoke"
if ! guard_import_origin; then
  guard_rc=$?
  if [[ "${USE_PYTHONPATH}" -eq 0 ]]; then
    echo "==> WARNING: 'usertest' did not import from this workspace; switching to PYTHONPATH mode."
    echo "    (This commonly happens when another checkout is installed editable in the same interpreter.)"
    echo "==> Configure PYTHONPATH via scripts/set_pythonpath.sh"
    USE_PYTHONPATH=1
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/set_pythonpath.sh"
    if ! guard_import_origin; then
      exit $?
    fi
  else
    exit "${guard_rc}"
  fi
fi

if [[ "${SKIP_INSTALL}" -eq 1 ]]; then
  PREFLIGHT_CODE=$'import importlib\n\nmods = [\n    "usertest",\n    "usertest.cli",\n    "usertest_backlog",\n    "usertest_backlog.cli",\n    "usertest_implement",\n    "usertest_implement.cli",\n    "agent_adapters",\n    "backlog_core",\n    "backlog_miner",\n    "backlog_repo",\n    "normalized_events",\n    "reporter",\n    "run_artifacts",\n    "runner_core",\n    "sandbox_runner",\n    "triage_engine",\n]\n\nerrors = []\nfor mod in mods:\n    try:\n        importlib.import_module(mod)\n    except Exception as e:\n        errors.append((mod, f"{type(e).__name__}: {e}"))\n\nif errors:\n    for mod, msg in errors:\n        print(f"{mod}: {msg}")\n    raise SystemExit(1)\n'

  preflight_rc=0
  preflight_out="$("${PYTHON_BIN}" -c "${PREFLIGHT_CODE}" 2>&1)" || preflight_rc=$?
  if [[ "${preflight_rc}" -ne 0 ]]; then
    echo "==> Smoke preflight failed: required imports are not available in this Python environment." >&2
    if [[ -n "${preflight_out}" ]]; then
      while IFS= read -r line; do
        [[ -n "${line}" ]] || continue
        echo "    - ${line}" >&2
      done <<<"${preflight_out}"
    fi
    echo "" >&2
    print_skip_install_guidance
    exit 1
  fi
fi

echo "==> CLI help smoke"
"${PYTHON_BIN}" -m usertest.cli --help

echo "==> Backlog CLI help smoke"
"${PYTHON_BIN}" -m usertest_backlog.cli --help

echo "==> Implement CLI help smoke"
"${PYTHON_BIN}" -m usertest_implement.cli --help

echo "==> Pytest smoke suite"
"${PYTHON_BIN}" -m pytest -q apps/usertest/tests/test_smoke.py apps/usertest/tests/test_golden_fixture.py apps/usertest_backlog/tests/test_smoke.py apps/usertest_implement/tests/test_smoke.py

echo "==> Smoke complete: all checks passed."
