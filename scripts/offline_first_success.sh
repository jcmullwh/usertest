#!/usr/bin/env bash
set -euo pipefail

FIXTURE_NAME="${1:-minimal_codex_run}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

source "${SCRIPT_DIR}/python_preflight.sh"
usertest_resolve_python "${REPO_ROOT}"
PYTHON_BIN="${USERTEST_PYTHON_BIN}"
export USERTEST_PYTHON="${PYTHON_BIN}"

LAUNCHER_ARGS=(
  tools/first_run_launcher.py
  offline-first-success
  --repo-root "${REPO_ROOT}"
  --python "${PYTHON_BIN}"
  --python-source "${USERTEST_PYTHON_SOURCE}"
  --shell posix
  --fixture-name "${FIXTURE_NAME}"
)

if [[ -n "${USERTEST_PYTHON_EXECUTABLE:-}" ]]; then
  LAUNCHER_ARGS+=(--python-executable "${USERTEST_PYTHON_EXECUTABLE}")
fi
if [[ -n "${USERTEST_PYTHON_VERSION:-}" ]]; then
  LAUNCHER_ARGS+=(--python-version "${USERTEST_PYTHON_VERSION}")
fi

exec "${PYTHON_BIN}" "${LAUNCHER_ARGS[@]}"
