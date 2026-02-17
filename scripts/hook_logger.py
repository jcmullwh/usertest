from __future__ import annotations

import json
import sys
from datetime import datetime, timezone


def main() -> int:
    """
    Minimal hook logger for future Claude Code instrumentation.

    This script intentionally accepts arbitrary JSON on stdin and writes a single JSONL line to
    stdout, so it can be used as a lightweight hook sink.
    """
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {"raw": ""}
    except json.JSONDecodeError:
        payload = {"raw": raw}

    event = {
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "hook": payload,
    }
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
