#!/usr/bin/env bash
set -euo pipefail

# This script is a thin wrapper around offline_first_success.sh for backwards compatibility.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/offline_first_success.sh" "$@"
