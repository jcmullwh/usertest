#!/usr/bin/env bash
set -euo pipefail

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

source "${SCRIPT_DIR}/python_preflight.sh"
usertest_resolve_python "${REPO_ROOT}"
PYTHON_BIN="${USERTEST_PYTHON_BIN}"
export USERTEST_PYTHON="${PYTHON_BIN}"

LAUNCHER_ARGS=(
  tools/first_run_launcher.py
  smoke
  --repo-root "${REPO_ROOT}"
  --python "${PYTHON_BIN}"
  --python-source "${USERTEST_PYTHON_SOURCE}"
  --shell posix
)

if [[ -n "${USERTEST_PYTHON_EXECUTABLE:-}" ]]; then
  LAUNCHER_ARGS+=(--python-executable "${USERTEST_PYTHON_EXECUTABLE}")
fi
if [[ -n "${USERTEST_PYTHON_VERSION:-}" ]]; then
  LAUNCHER_ARGS+=(--python-version "${USERTEST_PYTHON_VERSION}")
fi
if [[ "${SKIP_INSTALL}" -eq 1 ]]; then
  LAUNCHER_ARGS+=(--skip-install)
fi
if [[ "${USE_PYTHONPATH}" -eq 1 ]]; then
  LAUNCHER_ARGS+=(--use-pythonpath)
fi
if [[ "${REQUIRE_DOCTOR}" -eq 1 ]]; then
  LAUNCHER_ARGS+=(--require-doctor)
fi

exec "${PYTHON_BIN}" "${LAUNCHER_ARGS[@]}"
