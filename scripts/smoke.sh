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
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: scripts/smoke.sh [--skip-install] [--use-pythonpath]" >&2
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

if command -v pdm >/dev/null 2>&1; then
  echo "==> Scaffold doctor"
  "${PYTHON_BIN}" tools/scaffold/scaffold.py doctor
else
  echo "==> Scaffold doctor skipped (pdm not found on PATH)"
fi

if [[ "${SKIP_INSTALL}" -eq 0 ]]; then
  echo "==> Install base Python deps"
  "${PYTHON_BIN}" -m pip install -r requirements-dev.txt

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
      -e apps/usertest_backlog
  fi
elif [[ "${USE_PYTHONPATH}" -eq 1 ]]; then
  echo "==> Configure PYTHONPATH via scripts/set_pythonpath.sh"
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/set_pythonpath.sh"
fi

echo "==> CLI help smoke"
"${PYTHON_BIN}" -m usertest.cli --help

echo "==> Backlog CLI help smoke"
"${PYTHON_BIN}" -m usertest_backlog.cli --help

echo "==> Pytest smoke suite"
"${PYTHON_BIN}" -m pytest -q apps/usertest/tests/test_smoke.py apps/usertest/tests/test_golden_fixture.py apps/usertest_backlog/tests/test_smoke.py
