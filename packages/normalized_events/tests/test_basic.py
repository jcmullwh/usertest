from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from normalized_events import iter_events_jsonl, make_event, write_events_jsonl


def test_make_event_envelope() -> None:
    event = make_event("test_type", {"ok": True}, ts="2026-01-01T00:00:00Z")
    assert event["ts"] == "2026-01-01T00:00:00Z"
    assert event["type"] == "test_type"
    assert event["data"] == {"ok": True}


def test_write_and_iter_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    events = [
        make_event("a", {"n": 1}, ts="2026-01-01T00:00:00Z"),
        make_event("b", {"n": 2}, ts="2026-01-01T00:00:01Z"),
    ]

    write_events_jsonl(path, events)

    raw_lines = path.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 2
    assert json.loads(raw_lines[0]) == events[0]
    assert json.loads(raw_lines[1]) == events[1]

    assert list(iter_events_jsonl(path)) == events


def test_normalized_events_does_not_import_repo_packages() -> None:
    code = "\n".join(
        [
            "import normalized_events, sys",
            "assert 'agent_adapters' not in sys.modules",
            "assert 'reporter' not in sys.modules",
            "assert 'runner_core' not in sys.modules",
            "assert 'sandbox_runner' not in sys.modules",
        ]
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
