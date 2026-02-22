#!/usr/bin/env bash
set -euo pipefail

# Sets PYTHONPATH for running this monorepo from source.
#
# Usage (bash/zsh, from repo root):
#   source scripts/set_pythonpath.sh
#
# Notes:
# - Source the script so it updates PYTHONPATH in your current shell.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

export PYTHONPATH="${REPO_ROOT}/apps/usertest/src:${REPO_ROOT}/apps/usertest_backlog/src:${REPO_ROOT}/apps/usertest_implement/src:${REPO_ROOT}/packages/runner_core/src:${REPO_ROOT}/packages/agent_adapters/src:${REPO_ROOT}/packages/normalized_events/src:${REPO_ROOT}/packages/reporter/src:${REPO_ROOT}/packages/sandbox_runner/src:${REPO_ROOT}/packages/triage_engine/src:${REPO_ROOT}/packages/backlog_core/src:${REPO_ROOT}/packages/backlog_miner/src:${REPO_ROOT}/packages/backlog_repo/src:${REPO_ROOT}/packages/run_artifacts/src"
echo "PYTHONPATH set."
echo "${PYTHONPATH}"
