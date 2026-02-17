from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast


def _fallback_utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


def _fallback_make_event(
    event_type: str,
    data: dict[str, Any],
    *,
    ts: str | None = None,
) -> dict[str, Any]:
    return {"ts": ts or _fallback_utc_now_iso(), "type": event_type, "data": data}


def _fallback_write_events_jsonl(path: Path, events: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _fallback_iter_events_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            yield json.loads(raw)


_normalized_events_module: Any | None
try:
    _normalized_events_module = importlib.import_module("normalized_events")
except Exception:
    _normalized_events_module = None

if _normalized_events_module is not None:
    utc_now_iso = cast(Callable[[], str], _normalized_events_module.utc_now_iso)
    make_event = cast(
        Callable[[str, dict[str, Any]], dict[str, Any]],
        _normalized_events_module.make_event,
    )
    iter_events_jsonl = cast(
        Callable[[Path], Iterator[dict[str, Any]]],
        _normalized_events_module.iter_events_jsonl,
    )
    write_events_jsonl = cast(
        Callable[[Path, Iterable[dict[str, Any]]], None],
        _normalized_events_module.write_events_jsonl,
    )
else:
    utc_now_iso = _fallback_utc_now_iso
    make_event = _fallback_make_event
    iter_events_jsonl = _fallback_iter_events_jsonl
    write_events_jsonl = _fallback_write_events_jsonl

__all__ = [
    "iter_events_jsonl",
    "make_event",
    "utc_now_iso",
    "write_events_jsonl",
]
