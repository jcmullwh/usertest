from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

_LOCK_TIMEOUT_SECONDS = 60.0
_LOCK_POLL_SECONDS = 0.1
_LOCK_STALE_SECONDS = 300.0


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "updated_at": None, "actions": {}}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError:
        return {"schema_version": 1, "updated_at": None, "actions": {}}
    if not isinstance(raw, dict):
        return {"schema_version": 1, "updated_at": None, "actions": {}}
    actions = raw.get("actions")
    actions_dict = actions if isinstance(actions, dict) else {}
    updated_at_raw = raw.get("updated_at")
    updated_at = (
        updated_at_raw
        if isinstance(updated_at_raw, str) and updated_at_raw.strip()
        else None
    )
    return {
        "schema_version": 1,
        "updated_at": updated_at,
        "actions": actions_dict,
    }


def update_ledger_doc(
    doc: dict[str, Any],
    *,
    fingerprint: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    now = _utc_now_z()
    actions_raw = doc.get("actions")
    actions: dict[str, Any] = actions_raw if isinstance(actions_raw, dict) else {}

    entry_raw = actions.get(fingerprint)
    entry: dict[str, Any] = entry_raw if isinstance(entry_raw, dict) else {}
    changed = False

    if entry.get("fingerprint") != fingerprint:
        changed = True
    entry["fingerprint"] = fingerprint
    for key, value in updates.items():
        if value is None:
            continue
        if entry.get(key) != value:
            changed = True
            entry[key] = value

    if changed:
        entry["updated_at"] = now

    actions[fingerprint] = entry
    doc["schema_version"] = 1
    if changed:
        doc["updated_at"] = now
    doc["actions"] = actions
    return doc


def write_ledger(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(doc, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )


def _acquire_lock(path: Path) -> Path:
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as err:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = None
            if age is not None and age > _LOCK_STALE_SECONDS:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() - started > _LOCK_TIMEOUT_SECONDS:
                raise TimeoutError(
                    f"Timed out waiting for ledger lock: {lock_path}"
                ) from err
            time.sleep(_LOCK_POLL_SECONDS)
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"pid={os.getpid()}\n")
                handle.write(f"started_at={_utc_now_z()}\n")
        except Exception:
            try:
                lock_path.unlink()
            except OSError:
                pass
            raise
        return lock_path


def update_ledger_file(path: Path, *, fingerprint: str, updates: dict[str, Any]) -> dict[str, Any]:
    lock_path = _acquire_lock(path)
    try:
        doc = load_ledger(path)
        updated = update_ledger_doc(doc, fingerprint=fingerprint, updates=updates)
        write_ledger(path, updated)
        return updated
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass
