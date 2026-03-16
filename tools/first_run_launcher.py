from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
USERTEST_SRC = REPO_ROOT / "apps" / "usertest" / "src"
if str(USERTEST_SRC) not in sys.path:
    sys.path.insert(0, str(USERTEST_SRC))

from usertest.first_run_launcher import main


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
