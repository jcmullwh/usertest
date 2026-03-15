#!/usr/bin/env bash
set -euo pipefail

# One-command "from source" verification for agent-offline workflows.
#
# What it does:
# - Creates/uses a local `.venv`
# - Installs minimal deps from `requirements-dev.txt`
# - Sets `PYTHONPATH` for monorepo source execution
# - Copies a golden fixture run dir to a temp location
# - Re-renders `report.md` + recomputes metrics
#
# Usage (bash/zsh, from repo root):
#   bash ./scripts/offline_first_success.sh

FIXTURE_NAME="${1:-minimal_codex_run}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

source "${SCRIPT_DIR}/python_preflight.sh"
usertest_resolve_python "${REPO_ROOT}"
PYTHON_BIN="${USERTEST_PYTHON_BIN}"

eval "$("${PYTHON_BIN}" tools/python_toolchain.py resolve \
  --repo-root "${REPO_ROOT}" \
  --python-exe "${PYTHON_BIN}" \
  --workflow offline_first_success \
  --emit shell \
  --require-pip \
  --bootstrap-pip \
  --ensure-venv "${REPO_ROOT}/.venv" \
  --allow-temp-venv-fallback)"
PYTHON_BIN="${USERTEST_TOOLCHAIN_PYTHON_EXE}"

echo "==> Using Python: ${USERTEST_PYTHON_SOURCE} -> ${PYTHON_BIN}"
if [[ -n "${USERTEST_PYTHON_EXECUTABLE:-}" ]]; then
  echo "==> Python executable: ${USERTEST_PYTHON_EXECUTABLE}"
fi
if [[ -n "${USERTEST_PYTHON_VERSION:-}" ]]; then
  echo "==> Python version: ${USERTEST_PYTHON_VERSION}"
fi

VENV_DIR="${USERTEST_TOOLCHAIN_VENV_DIR}"
VENV_PY="${USERTEST_TOOLCHAIN_VENV_PY}"
if [[ "${USERTEST_TOOLCHAIN_VENV_FALLBACK_USED:-0}" == "1" ]]; then
  echo "==> WARNING: local .venv was unavailable; using temp venv at ${VENV_DIR}"
fi

PIP_FLAGS=(--disable-pip-version-check --retries 10 --timeout 30)

echo "==> Install minimal deps (requirements-dev.txt)"
"${VENV_PY}" -m pip install "${PIP_FLAGS[@]}" -r requirements-dev.txt

echo "==> Configure PYTHONPATH via scripts/set_pythonpath.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/set_pythonpath.sh"

echo "==> Copy fixture to temp dir"
RUN_DIR="$("${VENV_PY}" -c '
import pathlib
import shutil
import sys
import tempfile

fixture_name = sys.argv[1] if len(sys.argv) > 1 else "minimal_codex_run"
src = pathlib.Path("examples/golden_runs") / fixture_name
if not src.exists():
    raise SystemExit(f"Missing fixture dir: {src}")
dst_root = pathlib.Path(tempfile.mkdtemp(prefix="usertest_fixture_"))
dst = dst_root / fixture_name
shutil.copytree(src, dst)
print(dst)
' "${FIXTURE_NAME}")"

echo "==> Re-render report from fixture copy"
"${VENV_PY}" -m usertest.cli report --repo-root "${REPO_ROOT}" --run-dir "${RUN_DIR}" --recompute-metrics

echo "==> Success. Scratch run dir: ${RUN_DIR}"
