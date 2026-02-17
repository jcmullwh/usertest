from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

Event = dict[str, Any]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


def make_event(event_type: str, data: dict[str, Any], *, ts: str | None = None) -> Event:
    return {"ts": ts or utc_now_iso(), "type": event_type, "data": data}


def write_events_jsonl(path: Path, events: Iterable[Event]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


def iter_events_jsonl(path: Path) -> Iterator[Event]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


__all__ = [
    "Event",
    "iter_events_jsonl",
    "make_event",
    "utc_now_iso",
    "write_events_jsonl",
]
