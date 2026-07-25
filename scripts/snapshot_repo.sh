#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

source "${SCRIPT_DIR}/python_preflight.sh"
usertest_resolve_python "${REPO_ROOT}"
PYTHON_BIN="${USERTEST_PYTHON_BIN}"

eval "$("${PYTHON_BIN}" tools/python_toolchain.py resolve \
  --repo-root "${REPO_ROOT}" \
  --python-exe "${PYTHON_BIN}" \
  --workflow snapshot_repo \
  --emit shell)"
PYTHON_BIN="${USERTEST_TOOLCHAIN_PYTHON_EXE}"

echo "==> Using Python: ${USERTEST_PYTHON_SOURCE} -> ${PYTHON_BIN}"
if [[ -n "${USERTEST_PYTHON_VERSION:-}" ]]; then
  echo "==> Python version: ${USERTEST_PYTHON_VERSION}"
fi

if [[ $# -eq 0 ]]; then
  set -- --out repo_snapshot.zip
fi

echo "==> snapshot_repo"
"${PYTHON_BIN}" tools/snapshot_repo.py "$@"
