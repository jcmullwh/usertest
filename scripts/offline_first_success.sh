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

PYTHON_BIN="python"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  else
    echo "Neither 'python' nor 'python3' is available on PATH." >&2
    exit 1
  fi
fi

VENV_DIR="${REPO_ROOT}/.venv"
VENV_PY="${VENV_DIR}/bin/python"
if [[ ! -x "${VENV_PY}" ]]; then
  echo "==> Create venv (.venv)"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi
if [[ ! -x "${VENV_PY}" ]]; then
  echo "Failed to create venv at ${VENV_DIR}" >&2
  exit 1
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
