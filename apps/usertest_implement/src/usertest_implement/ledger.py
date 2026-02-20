from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


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
    updated_at = updated_at_raw if isinstance(updated_at_raw, str) and updated_at_raw.strip() else None
    return {
        "schema_version": 1,
        "updated_at": updated_at,
        "actions": actions_dict,
    }


def update_ledger_doc(doc: dict[str, Any], *, fingerprint: str, updates: dict[str, Any]) -> dict[str, Any]:
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


def update_ledger_file(path: Path, *, fingerprint: str, updates: dict[str, Any]) -> dict[str, Any]:
    doc = load_ledger(path)
    updated = update_ledger_doc(doc, fingerprint=fingerprint, updates=updates)
    write_ledger(path, updated)
    return updated
