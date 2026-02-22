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

PIP_FLAGS=(--disable-pip-version-check --retries 10 --timeout 30)

if command -v pdm >/dev/null 2>&1; then
  echo "==> Scaffold doctor"
  "${PYTHON_BIN}" tools/scaffold/scaffold.py doctor
else
  if [[ "${REQUIRE_DOCTOR}" -eq 1 ]]; then
    echo "Scaffold doctor required but pdm was not found on PATH." >&2
    echo "Install pdm (recommended): ${PYTHON_BIN} -m pip install -U pdm" >&2
    echo "Or rerun without --require-doctor." >&2
    exit 1
  fi
  echo "==> Scaffold doctor (tool checks skipped; pdm not found on PATH)"
  echo "    Note: pdm is optional; continuing with the pip-based flow."
  echo "    To enable tool checks: ${PYTHON_BIN} -m pip install -U pdm"
  echo "    To require doctor: bash ./scripts/smoke.sh --require-doctor"
  "${PYTHON_BIN}" tools/scaffold/scaffold.py doctor --skip-tool-checks
fi

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

if [[ "${SKIP_INSTALL}" -eq 1 && "${USE_PYTHONPATH}" -eq 0 ]]; then
  if ! "${PYTHON_BIN}" -c "import usertest" >/dev/null 2>&1; then
    echo "==> Smoke preflight failed: 'usertest' is not importable in this Python environment." >&2
    echo "    Fix options:" >&2
    echo "      - Rerun without --skip-install (installs editables into the active env)." >&2
    echo "      - Or use PYTHONPATH mode:" >&2
    echo "          ${PYTHON_BIN} -m pip install -r requirements-dev.txt" >&2
    echo "          bash ./scripts/smoke.sh --skip-install --use-pythonpath" >&2
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
