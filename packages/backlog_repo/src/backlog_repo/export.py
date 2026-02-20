from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import Any

_EXPORT_PATH_LIKE_RE = re.compile(r"(?:[A-Za-z]:[\\/])?[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+){1,}")
_EXPORT_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _coerce_string(value: Any) -> str | None:
    """Normalize a potential string to a trimmed non-empty value.

    Parameters
    ----------
    value:
        Candidate value.

    Returns
    -------
    str | None
        Trimmed string when valid, otherwise ``None``.
    """

    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _coerce_string_list(value: Any) -> list[str]:
    """Normalize a value to a list of trimmed strings.

    Parameters
    ----------
    value:
        Candidate list-like value.

    Returns
    -------
    list[str]
        Filtered trimmed strings. Non-list inputs return an empty list.
    """

    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def ticket_export_anchors(ticket: dict[str, Any]) -> set[str]:
    """Extract path-like anchors from ticket narrative fields.

    Parameters
    ----------
    ticket:
        Ticket payload to fingerprint/export.

    Returns
    -------
    set[str]
        Normalized lowercase anchors discovered in textual fields.
    """

    chunks: list[str] = []
    for key in ("title", "problem", "user_impact", "proposed_fix"):
        value = _coerce_string(ticket.get(key))
        if value:
            chunks.append(value)
    chunks.extend(_coerce_string_list(ticket.get("investigation_steps")))

    anchors: set[str] = set()
    for chunk in chunks:
        for match in _EXPORT_PATH_LIKE_RE.findall(chunk):
            anchors.add(match.lower().replace("\\", "/"))
    return anchors


def ticket_export_fingerprint(ticket: dict[str, Any]) -> str:
    """Compute deterministic short fingerprint for export dedupe/routing.

    Parameters
    ----------
    ticket:
        Ticket payload.

    Returns
    -------
    str
        Stable 16-character hexadecimal fingerprint.
    """

    title = _coerce_string(ticket.get("title")) or ""
    title_tokens = sorted(set(_EXPORT_TOKEN_RE.findall(title.lower())))
    anchors = sorted(ticket_export_anchors(ticket))

    change_surface_raw = ticket.get("change_surface")
    change_surface = change_surface_raw if isinstance(change_surface_raw, dict) else {}
    kinds = sorted(set(_coerce_string_list(change_surface.get("kinds"))))

    owner = (
        _coerce_string(ticket.get("suggested_owner"))
        or _coerce_string(ticket.get("component"))
        or "unknown"
    )

    payload = {
        "title_tokens": title_tokens[:24],
        "anchors": anchors[:24],
        "kinds": kinds[:24],
        "owner": owner,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return sha256(blob).hexdigest()[:16]


