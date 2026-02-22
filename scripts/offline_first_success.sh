#!/usr/bin/env bash
set -euo pipefail

echo "WARNING: offline_first_success.sh is deprecated."
echo "NOTE: This path only rerenders a golden fixture run; it does NOT execute any agents or validate real performance."
echo "Use scripts/offline_fixture_rerender.sh (or run usertest/usertest-backlog normally) for real runs."

dirname "${BASH_SOURCE[0]}" >/dev/null 2>&1
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/offline_fixture_rerender.sh" "$@"
