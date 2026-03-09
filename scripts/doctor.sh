#!/usr/bin/env bash
set -euo pipefail

SKIP_TOOL_CHECKS=0
REQUIRE_PIP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-tool-checks)
      SKIP_TOOL_CHECKS=1
      shift
      ;;
    --require-pip)
      REQUIRE_PIP=1
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: scripts/doctor.sh [--skip-tool-checks] [--require-pip]" >&2
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

echo "==> Using Python: ${USERTEST_PYTHON_SOURCE} -> ${PYTHON_BIN}"
if [[ -n "${USERTEST_PYTHON_EXECUTABLE:-}" ]]; then
  echo "==> Python executable: ${USERTEST_PYTHON_EXECUTABLE}"
fi
if [[ -n "${USERTEST_PYTHON_VERSION:-}" ]]; then
  echo "==> Python version: ${USERTEST_PYTHON_VERSION}"
fi

if [[ "${SKIP_TOOL_CHECKS}" -eq 1 ]]; then
  echo "==> Scaffold doctor (tool checks skipped)"
  EXTRA_ARGS=("--skip-tool-checks")
else
  echo "==> Scaffold doctor"
  EXTRA_ARGS=()
fi

if [[ "${REQUIRE_PIP}" -eq 1 ]]; then
  EXTRA_ARGS+=("--require-pip")
fi

"${PYTHON_BIN}" tools/scaffold/scaffold.py doctor "${EXTRA_ARGS[@]}"
