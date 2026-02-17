from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CHANGE_SURFACE_KIND_ENUM: set[str] = {
    "new_command",
    "new_flag",
    "docs_change",
    "behavior_change",
    "breaking_change",
    "new_top_level_mode",
    "new_config_schema",
    "new_api",
    "unknown",
}

TICKET_STAGE_ENUM: set[str] = {
    "triage",
    "research_required",
    "ready_for_ticket",
    "blocked",
}


def _coerce_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _coerce_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _coerce_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


_DEFAULT_INVESTIGATION_STEPS_HIGH_SURFACE_LOW_BREADTH: tuple[str, ...] = (
    "Validate repo intent",
    "Check if existing commands/flags can be parameterized",
    "Propose a consolidation plan (avoid new top-level commands)",
)


@dataclass(frozen=True)
class BacklogPolicyConfig:
    """
    Configuration for routing backlog tickets based on structured surface-area + breadth fields.

    This config is intended to be loaded by an application layer (e.g. the usertest CLI) from a
    YAML/JSON file. The reporter library itself does not assume a particular on-disk format.
    """

    surface_area_high: frozenset[str]
    breadth_min_for_surface_area_high: dict[str, int]
    default_stage_for_high_surface_low_breadth: str = "research_required"
    default_stage_for_labeled: str = "ready_for_ticket"
    investigation_steps_for_high_surface_low_breadth: tuple[str, ...] = field(
        default_factory=lambda: _DEFAULT_INVESTIGATION_STEPS_HIGH_SURFACE_LOW_BREADTH
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BacklogPolicyConfig:
        """
        Build and validate a policy config from an untyped mapping.

        Raises
        ------
        ValueError
            If required fields are missing or invalid.
        """

        surface_area_high_raw = data.get("surface_area_high")
        if not isinstance(surface_area_high_raw, list) or not surface_area_high_raw:
            raise ValueError("backlog_policy.surface_area_high must be a non-empty list")
        surface_area_high = {
            item.strip()
            for item in surface_area_high_raw
            if isinstance(item, str) and item.strip()
        }
        unknown = [item for item in surface_area_high if item not in CHANGE_SURFACE_KIND_ENUM]
        if unknown:
            raise ValueError(
                "backlog_policy.surface_area_high contains unknown kinds: "
                + ", ".join(sorted(unknown))
            )

        breadth_min_raw = data.get("breadth_min_for_surface_area_high")
        if not isinstance(breadth_min_raw, dict) or not breadth_min_raw:
            raise ValueError(
                "backlog_policy.breadth_min_for_surface_area_high must be a non-empty mapping"
            )
        allowed_dims = {"runs", "missions", "targets", "repo_inputs", "agents", "personas"}
        breadth_min: dict[str, int] = {}
        for key, raw_value in breadth_min_raw.items():
            if not isinstance(key, str) or key.strip() not in allowed_dims:
                raise ValueError(
                    "backlog_policy.breadth_min_for_surface_area_high key must be one of: "
                    + ", ".join(sorted(allowed_dims))
                )
            value = _coerce_int(raw_value, default=-1)
            if value < 0:
                raise ValueError(
                    "backlog_policy.breadth_min_for_surface_area_high."
                    f"{key} must be an integer >= 0"
                )
            breadth_min[key.strip()] = value

        default_stage = (
            _coerce_string(data.get("default_stage_for_high_surface_low_breadth"))
            or "research_required"
        )
        if default_stage not in TICKET_STAGE_ENUM:
            raise ValueError(
                "backlog_policy.default_stage_for_high_surface_low_breadth must be one of: "
                + ", ".join(sorted(TICKET_STAGE_ENUM))
            )

        default_stage_labeled = _coerce_string(data.get("default_stage_for_labeled")) or (
            "ready_for_ticket"
        )
        if default_stage_labeled not in TICKET_STAGE_ENUM:
            raise ValueError(
                "backlog_policy.default_stage_for_labeled must be one of: "
                + ", ".join(sorted(TICKET_STAGE_ENUM))
            )

        steps_raw = data.get("investigation_steps_for_high_surface_low_breadth")
        if steps_raw is None:
            steps = _DEFAULT_INVESTIGATION_STEPS_HIGH_SURFACE_LOW_BREADTH
        else:
            steps_list = _coerce_string_list(steps_raw)
            if not steps_list:
                raise ValueError(
                    "backlog_policy.investigation_steps_for_high_surface_low_breadth must be a "
                    "non-empty list"
                )
            steps = tuple(steps_list)

        return cls(
            surface_area_high=frozenset(surface_area_high),
            breadth_min_for_surface_area_high=breadth_min,
            default_stage_for_high_surface_low_breadth=default_stage,
            default_stage_for_labeled=default_stage_labeled,
            investigation_steps_for_high_surface_low_breadth=steps,
        )


def apply_backlog_policy(
    tickets: list[dict[str, Any]],
    *,
    config: BacklogPolicyConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Apply policy decisions to a list of backlog tickets.

    The policy engine depends only on structured fields:

    - ticket.change_surface.kinds
    - ticket.breadth.<dimension>
    - ticket.stage / ticket.risks / ticket.investigation_steps (for patching)

    Notes
    -----
    - If `change_surface.kinds` is missing or contains `unknown`, policy will not promote
      a ticket to `ready_for_ticket`.
    - Policy may upgrade a `ready_for_ticket` ticket to `research_required` if it is
      high-surface-area but supported by narrow evidence breadth.
    """

    meta: dict[str, Any] = {
        "tickets_total": len(tickets),
        "tickets_research_required": 0,
        "tickets_ready_for_ticket": 0,
        "tickets_unchanged": 0,
    }

    updated: list[dict[str, Any]] = []
    for ticket in tickets:
        item = dict(ticket)

        stage = _coerce_string(item.get("stage")) or "triage"
        if stage not in TICKET_STAGE_ENUM:
            stage = "triage"

        change_surface_raw = item.get("change_surface")
        change_surface = change_surface_raw if isinstance(change_surface_raw, dict) else {}
        kinds_raw = _coerce_string_list(change_surface.get("kinds"))
        kinds = [kind for kind in kinds_raw if kind in CHANGE_SURFACE_KIND_ENUM]
        if not kinds:
            kinds = ["unknown"]

        labeled = "unknown" not in kinds
        high_surface = bool(set(kinds) & config.surface_area_high)

        breadth_raw = item.get("breadth")
        breadth = breadth_raw if isinstance(breadth_raw, dict) else {}
        breadth_counts = {
            dim: _coerce_int(breadth.get(dim), default=0)
            for dim in ("runs", "missions", "targets", "repo_inputs", "agents", "personas")
        }

        low_breadth = False
        for dim, threshold in config.breadth_min_for_surface_area_high.items():
            if breadth_counts.get(dim, 0) < threshold:
                low_breadth = True
                break

        new_stage = stage
        risks_to_add: list[str] = []
        steps_to_add: list[str] = []

        if labeled:
            if high_surface and low_breadth and stage != "blocked":
                new_stage = config.default_stage_for_high_surface_low_breadth
                risks_to_add.append("overfitting_risk")
                steps_to_add.extend(list(config.investigation_steps_for_high_surface_low_breadth))
            elif stage == "triage":
                new_stage = config.default_stage_for_labeled

        existing_risks = _coerce_string_list(item.get("risks"))
        for risk in risks_to_add:
            if risk not in existing_risks:
                existing_risks.append(risk)

        existing_steps = _coerce_string_list(item.get("investigation_steps"))
        for step in steps_to_add:
            if step not in existing_steps:
                existing_steps.append(step)

        item["stage"] = new_stage
        item["risks"] = existing_risks
        item["investigation_steps"] = existing_steps
        updated.append(item)

        if new_stage == "research_required":
            meta["tickets_research_required"] += 1
        elif new_stage == "ready_for_ticket":
            meta["tickets_ready_for_ticket"] += 1
        elif new_stage == stage:
            meta["tickets_unchanged"] += 1

    return updated, meta
