#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec 'I:\code\usertest\runs\usertest_implement\_workspaces\usertest_20260707T041511Z_codex_0\.venv\Scripts\python.exe' "$SCRIPT_DIR/verify_client.py"
