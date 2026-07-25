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


def _nested_object(value: Any) -> dict[str, Any]:
    """Normalize a candidate nested mapping to a dict."""

    return value if isinstance(value, dict) else {}


def _ticket_text_field(ticket: dict[str, Any], key: str) -> str | None:
    """Return a narrative text field from a ticket, falling back to stage-backed fields."""

    value = _coerce_string(ticket.get(key))
    if value:
        return value

    change_plan = _nested_object(ticket.get("change_plan"))
    problem_record = _nested_object(ticket.get("problem_record"))
    selected_solution = _nested_object(ticket.get("selected_solution"))

    for source in (change_plan, problem_record, selected_solution):
        v = _coerce_string(source.get(key))
        if v:
            return v

    if key == "proposed_fix":
        selected_option = _nested_object(selected_solution.get("selected_option"))
        summary = _coerce_string(selected_option.get("summary"))
        if summary:
            return summary

    return None


def _ticket_change_surface(ticket: dict[str, Any]) -> dict[str, Any]:
    change_surface_raw = ticket.get("change_surface")
    change_surface = _nested_object(change_surface_raw)
    if change_surface:
        return change_surface
    selected_solution = _nested_object(ticket.get("selected_solution"))
    return _nested_object(selected_solution.get("change_surface"))


def _ticket_owner(ticket: dict[str, Any]) -> str:
    owner = _coerce_string(ticket.get("suggested_owner")) or _coerce_string(ticket.get("component"))
    if owner:
        return owner

    change_plan = _nested_object(ticket.get("change_plan"))
    owner = _coerce_string(change_plan.get("suggested_owner"))
    if owner:
        return owner

    selected_solution = _nested_object(ticket.get("selected_solution"))
    owner = _coerce_string(selected_solution.get("component"))
    return owner or "unknown"


def ticket_export_case_id(ticket: dict[str, Any]) -> str | None:
    """Return the persisted canonical case identity carried by a ticket.

    The lookup intentionally does not derive identity from title or problem
    prose. Stage-backed tickets may carry the field at the top level or inside
    their problem/change-plan payloads.
    """

    for source in (
        ticket,
        _nested_object(ticket.get("problem_record")),
        _nested_object(ticket.get("change_plan")),
        _nested_object(ticket.get("selected_solution")),
    ):
        for key in ("case_id", "root_case_id"):
            value = _coerce_string(source.get(key))
            if value is not None:
                return value
    return None


def ticket_export_plan_revision_id(ticket: dict[str, Any]) -> str | None:
    """Return the persisted plan revision/change-plan identity for export."""

    for source in (ticket, _nested_object(ticket.get("change_plan"))):
        for key in ("plan_revision_id", "change_plan_id"):
            value = _coerce_string(source.get(key))
            if value is not None:
                return value
    return None


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
        value = _ticket_text_field(ticket, key)
        if value is not None:
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

    case_id = ticket_export_case_id(ticket)
    plan_revision_id = ticket_export_plan_revision_id(ticket)
    if case_id is not None:
        payload = {
            "case_id": case_id,
            # Pre-plan/research exports deliberately share a single stable
            # case fingerprint until an explicit plan revision exists.
            "plan_revision_id": plan_revision_id or "case",
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return sha256(blob).hexdigest()[:16]

    title = _ticket_text_field(ticket, "title") or ""
    title_tokens = sorted(set(_EXPORT_TOKEN_RE.findall(title.lower())))
    anchors = sorted(ticket_export_anchors(ticket))

    change_surface = _ticket_change_surface(ticket)
    kinds = sorted(set(_coerce_string_list(change_surface.get("kinds"))))

    owner = _ticket_owner(ticket)

    payload = {
        "title_tokens": title_tokens[:24],
        "anchors": anchors[:24],
        "kinds": kinds[:24],
        "owner": owner,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return sha256(blob).hexdigest()[:16]
