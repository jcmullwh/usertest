from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ATOM_STATUS_ORDER: dict[str, int] = {"new": 0, "ticketed": 1, "queued": 2, "actioned": 3}
CANONICAL_FAILURE_ATOM_SOURCE = "run_failure_event"
LEGACY_FAILURE_ATOM_SOURCES: set[str] = {
    "error_json",
    "report_validation_error",
}


def _coerce_string(value: Any) -> str | None:
    """Normalize a potential string value.

    Parameters
    ----------
    value:
        Candidate value to coerce.

    Returns
    -------
    str | None
        Trimmed non-empty string when coercion succeeds, otherwise ``None``.
    """

    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def sorted_unique_strings(values: list[str]) -> list[str]:
    """Return sorted unique non-empty strings.

    Parameters
    ----------
    values:
        Raw list that may contain duplicates and empty entries.

    Returns
    -------
    list[str]
        Alphabetically sorted unique strings.
    """

    return sorted({value for value in values if isinstance(value, str) and value.strip()})


def normalize_atom_status(value: str | None) -> str:
    """Normalize atom status to a supported value.

    Parameters
    ----------
    value:
        Input status label from persisted atom metadata.

    Returns
    -------
    str
        Normalized status value.

    Raises
    ------
    ValueError
        Raised when a non-empty unsupported status is supplied.
    """

    if value is None:
        return "new"
    cleaned = value.strip().lower()
    if cleaned in ATOM_STATUS_ORDER:
        return cleaned
    raise ValueError(f"Unsupported atom status: {value!r}")


def promote_atom_status(current: str | None, desired: str) -> str:
    """Promote status while preserving monotonic lifecycle ordering.

    Parameters
    ----------
    current:
        Existing status for an atom.
    desired:
        Newly requested status.

    Returns
    -------
    str
        Either ``desired`` (when it is same-or-later than ``current``) or ``current``.
    """

    current_n = normalize_atom_status(current)
    desired_n = normalize_atom_status(desired)
    if ATOM_STATUS_ORDER[desired_n] >= ATOM_STATUS_ORDER[current_n]:
        return desired_n
    return current_n


def canonicalize_failure_atom_id(atom_id: str) -> str | None:
    """Map legacy failure atom sources to the canonical failure source.

    Parameters
    ----------
    atom_id:
        Raw atom identifier in ``run:source:index`` shape.

    Returns
    -------
    str | None
        Canonicalized atom ID when applicable, otherwise ``None`` for non-failure
        or unparsable identifiers.
    """

    try:
        run_id, source, _index = atom_id.rsplit(":", 2)
    except ValueError:
        return None

    if source == CANONICAL_FAILURE_ATOM_SOURCE:
        return atom_id
    if source in LEGACY_FAILURE_ATOM_SOURCES:
        return f"{run_id}:{CANONICAL_FAILURE_ATOM_SOURCE}:1"
    return None


def load_backlog_actions_yaml(actions_path: Path) -> dict[str, dict[str, Any]]:
    """Load backlog action ledger keyed by ticket fingerprint.

    Parameters
    ----------
    actions_path:
        Path to ``configs/backlog_actions.yaml``.

    Returns
    -------
    dict[str, dict[str, Any]]
        Action entries keyed by fingerprint. Missing files are initialized with
        ``{"version": 1, "actions": []}``.

    Raises
    ------
    ValueError
        Raised when file content is missing required fields or schema version.
    """

    if not actions_path.exists():
        actions_path.parent.mkdir(parents=True, exist_ok=True)
        actions_path.write_text(
            yaml.safe_dump({"version": 1, "actions": []}, sort_keys=False), encoding="utf-8"
        )
        return {}

    raw = yaml.safe_load(actions_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid action ledger YAML (expected mapping): {actions_path}")
    version = raw.get("version")
    if version != 1:
        raise ValueError(f"Unsupported action ledger version (expected 1): {actions_path}")
    actions_raw = raw.get("actions")
    if actions_raw is None:
        return {}
    if not isinstance(actions_raw, list):
        raise ValueError(f"Invalid action ledger actions list: {actions_path}")

    actions: dict[str, dict[str, Any]] = {}
    for idx, entry in enumerate(actions_raw, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid action entry #{idx} (expected mapping): {actions_path}")
        fingerprint = _coerce_string(entry.get("fingerprint"))
        if fingerprint is None:
            raise ValueError(f"Invalid action entry #{idx} (missing fingerprint): {actions_path}")
        actions[fingerprint] = entry
    return actions


def load_atom_actions_yaml(path: Path) -> dict[str, dict[str, Any]]:
    """Load atom action ledger keyed by atom ID.

    Parameters
    ----------
    path:
        Path to ``configs/backlog_atom_actions.yaml``.

    Returns
    -------
    dict[str, dict[str, Any]]
        Atom action entries keyed by atom identifier. Missing files return an empty map.

    Raises
    ------
    ValueError
        Raised when YAML structure is invalid or contains unsupported status values.
    """

    if not path.exists():
        return {}

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid atom actions YAML (expected mapping): {path}")
    version = raw.get("version")
    if version != 1:
        raise ValueError(f"Unsupported atom actions version (expected 1): {path}")
    atoms_raw = raw.get("atoms")
    if atoms_raw is None:
        return {}
    if not isinstance(atoms_raw, list):
        raise ValueError(f"Invalid atom actions list: {path}")

    atoms: dict[str, dict[str, Any]] = {}
    for idx, entry in enumerate(atoms_raw, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid atom action entry #{idx} (expected mapping): {path}")
        atom_id = _coerce_string(entry.get("atom_id"))
        if atom_id is None:
            raise ValueError(f"Invalid atom action entry #{idx} (missing atom_id): {path}")
        item = dict(entry)
        item["atom_id"] = atom_id
        item["status"] = normalize_atom_status(_coerce_string(item.get("status")))
        atoms[atom_id] = item
    return atoms


def write_atom_actions_yaml(path: Path, atoms: dict[str, dict[str, Any]]) -> None:
    """Persist atom action ledger with normalized deterministic ordering.

    Parameters
    ----------
    path:
        Destination YAML path.
    atoms:
        Atom state payload keyed by atom ID.
    """

    payload_atoms: list[dict[str, Any]] = []
    for atom_id in sorted(atoms.keys()):
        item = dict(atoms[atom_id])
        item["atom_id"] = atom_id
        item["status"] = normalize_atom_status(_coerce_string(item.get("status")))

        for list_key in (
            "ticket_ids",
            "queue_paths",
            "queue_owner_roots",
            "fingerprints",
            "derived_from_atom_ids",
        ):
            values = item.get(list_key)
            if isinstance(values, list):
                item[list_key] = sorted_unique_strings(
                    [value for value in values if isinstance(value, str)]
                )
            elif values is None:
                item[list_key] = []
            else:
                item[list_key] = sorted_unique_strings([str(values)])

        payload_atoms.append(item)

    doc = {"version": 1, "atoms": payload_atoms}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
