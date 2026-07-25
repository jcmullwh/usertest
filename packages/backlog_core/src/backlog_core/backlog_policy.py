from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backlog_core.stage_contracts import assess_research_readiness
from backlog_core.ticket_readiness import assess_ticket_readiness

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

_ALLOWED_BREADTH_DIMS: frozenset[str] = frozenset(
    {"runs", "missions", "targets", "repo_inputs", "agents", "personas"}
)
_REVIEW_DOMAIN_ENUM: frozenset[str] = frozenset({"command_surface", "behavior_compat"})


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


def _has_selected_solution(ticket: dict[str, Any]) -> bool:
    """Return whether *ticket* has an explicit selected solution.

    The six-stage pipeline treats selection as a prerequisite for promotion to
    ``ready_for_ticket``. Legacy one-pass tickets may still contain solution-like
    fields, but they do not satisfy this prerequisite unless a selection marker
    is present.
    """
    if isinstance(ticket.get("selected_solution"), dict):
        return True
    selected_option_id = _coerce_string(ticket.get("selected_option_id"))
    if selected_option_id is not None:
        return True
    return False


def _has_change_plan(ticket: dict[str, Any]) -> bool:
    """Return whether *ticket* has an explicit implementation/change plan.

    Stage 6 produces a change plan. Until a plan exists, a ticket should not be
    considered ``ready_for_ticket`` even if it is labeled.
    """
    if isinstance(ticket.get("change_plan"), dict):
        return True
    change_plan_id = _coerce_string(ticket.get("change_plan_id"))
    if change_plan_id is not None:
        return True
    return False


def _has_ready_research_proof(ticket: dict[str, Any]) -> bool:
    """Return whether the ticket carries a strict, sufficient research proof.

    Historical dossiers remain readable through the stage-contract legacy parser,
    but absence of a version-2 proof is intentionally not equivalent to success.
    """
    ready, _ = assess_research_readiness(
        ticket.get("research") if isinstance(ticket.get("research"), dict) else None
    )
    return ready


_DEFAULT_INVESTIGATION_STEPS_HIGH_SURFACE_LOW_BREADTH: tuple[str, ...] = (
    "Validate repo intent",
    "Check if existing commands/flags can be parameterized",
    "Propose a consolidation plan (avoid new top-level commands)",
)


@dataclass(frozen=True)
class HighSurfaceRule:
    """Policy rule for a subset of high-surface change kinds."""

    rule_id: str
    applies_to_kinds: frozenset[str]
    breadth_min: dict[str, int]
    default_stage_for_low_breadth: str = "research_required"
    investigation_steps: tuple[str, ...] = field(
        default_factory=lambda: _DEFAULT_INVESTIGATION_STEPS_HIGH_SURFACE_LOW_BREADTH
    )
    risk_tag: str = "overfitting_risk"
    review_domain: str = "command_surface"


@dataclass(frozen=True)
class BacklogPolicyConfig:
    """
    Configuration for routing backlog tickets based on structured surface-area + breadth fields.

    This config is intended to be loaded by an application layer (e.g. the usertest CLI) from a
    YAML/JSON file. The reporter library itself does not assume a particular on-disk format.
    """

    high_surface_rules: tuple[HighSurfaceRule, ...]
    default_stage_for_labeled: str = "ready_for_ticket"

    @property
    def surface_area_high(self) -> frozenset[str]:
        kinds: set[str] = set()
        for rule in self.high_surface_rules:
            kinds.update(rule.applies_to_kinds)
        return frozenset(kinds)

    @property
    def breadth_min_for_surface_area_high(self) -> dict[str, int]:
        if not self.high_surface_rules:
            return {}
        return dict(self.high_surface_rules[0].breadth_min)

    @property
    def default_stage_for_high_surface_low_breadth(self) -> str:
        if not self.high_surface_rules:
            return "research_required"
        return self.high_surface_rules[0].default_stage_for_low_breadth

    @property
    def investigation_steps_for_high_surface_low_breadth(self) -> tuple[str, ...]:
        if not self.high_surface_rules:
            return _DEFAULT_INVESTIGATION_STEPS_HIGH_SURFACE_LOW_BREADTH
        return self.high_surface_rules[0].investigation_steps

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BacklogPolicyConfig:
        """
        Build and validate a policy config from an untyped mapping.

        Raises
        ------
        ValueError
            If required fields are missing or invalid.
        """

        default_stage_labeled = _coerce_string(data.get("default_stage_for_labeled")) or (
            "ready_for_ticket"
        )
        if default_stage_labeled not in TICKET_STAGE_ENUM:
            raise ValueError(
                "backlog_policy.default_stage_for_labeled must be one of: "
                + ", ".join(sorted(TICKET_STAGE_ENUM))
            )

        rules_raw = data.get("high_surface_rules")
        if rules_raw is not None:
            if not isinstance(rules_raw, list) or not rules_raw:
                raise ValueError("backlog_policy.high_surface_rules must be a non-empty list")
            rules = tuple(_parse_high_surface_rule(item, idx) for idx, item in enumerate(rules_raw))
        else:
            rules = (_parse_legacy_high_surface_rule(data),)

        return cls(
            high_surface_rules=rules,
            default_stage_for_labeled=default_stage_labeled,
        )


def _parse_change_surface_kinds(value: Any, *, field_label: str) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field_label} must be a non-empty list")
    kinds = {item.strip() for item in value if isinstance(item, str) and item.strip()}
    if not kinds:
        raise ValueError(f"{field_label} must contain at least one change-surface kind")
    unknown = [item for item in kinds if item not in CHANGE_SURFACE_KIND_ENUM]
    if unknown:
        raise ValueError(
            f"{field_label} contains unknown kinds: " + ", ".join(sorted(unknown))
        )
    return frozenset(kinds)


def _parse_breadth_min(value: Any, *, field_label: str) -> dict[str, int]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{field_label} must be a non-empty mapping")
    breadth_min: dict[str, int] = {}
    for key, raw_value in value.items():
        if not isinstance(key, str) or key.strip() not in _ALLOWED_BREADTH_DIMS:
            raise ValueError(
                f"{field_label} key must be one of: " + ", ".join(sorted(_ALLOWED_BREADTH_DIMS))
            )
        parsed = _coerce_int(raw_value, default=-1)
        if parsed < 0:
            raise ValueError(f"{field_label}.{key} must be an integer >= 0")
        breadth_min[key.strip()] = parsed
    return breadth_min


def _parse_stage(value: Any, *, field_label: str, default: str) -> str:
    parsed = _coerce_string(value) or default
    if parsed not in TICKET_STAGE_ENUM:
        raise ValueError(
            f"{field_label} must be one of: " + ", ".join(sorted(TICKET_STAGE_ENUM))
        )
    return parsed


def _parse_investigation_steps(value: Any, *, field_label: str) -> tuple[str, ...]:
    if value is None:
        return _DEFAULT_INVESTIGATION_STEPS_HIGH_SURFACE_LOW_BREADTH
    steps = _coerce_string_list(value)
    if not steps:
        raise ValueError(f"{field_label} must be a non-empty list")
    return tuple(steps)


def _parse_high_surface_rule(raw_rule: Any, idx: int) -> HighSurfaceRule:
    if not isinstance(raw_rule, dict):
        raise ValueError(f"backlog_policy.high_surface_rules[{idx}] must be a mapping")
    prefix = f"backlog_policy.high_surface_rules[{idx}]"
    rule_id = _coerce_string(raw_rule.get("rule_id")) or f"rule_{idx + 1}"
    applies_to_kinds = _parse_change_surface_kinds(
        raw_rule.get("applies_to_kinds"),
        field_label=f"{prefix}.applies_to_kinds",
    )
    breadth_min = _parse_breadth_min(
        raw_rule.get("breadth_min"),
        field_label=f"{prefix}.breadth_min",
    )
    default_stage = _parse_stage(
        raw_rule.get("default_stage_for_low_breadth"),
        field_label=f"{prefix}.default_stage_for_low_breadth",
        default="research_required",
    )
    investigation_steps = _parse_investigation_steps(
        raw_rule.get("investigation_steps"),
        field_label=f"{prefix}.investigation_steps",
    )
    risk_tag = _coerce_string(raw_rule.get("risk_tag")) or "overfitting_risk"
    review_domain = _coerce_string(raw_rule.get("review_domain")) or "command_surface"
    if review_domain not in _REVIEW_DOMAIN_ENUM:
        raise ValueError(
            f"{prefix}.review_domain must be one of: " + ", ".join(sorted(_REVIEW_DOMAIN_ENUM))
        )
    return HighSurfaceRule(
        rule_id=rule_id,
        applies_to_kinds=applies_to_kinds,
        breadth_min=breadth_min,
        default_stage_for_low_breadth=default_stage,
        investigation_steps=investigation_steps,
        risk_tag=risk_tag,
        review_domain=review_domain,
    )


def _parse_legacy_high_surface_rule(data: dict[str, Any]) -> HighSurfaceRule:
    surface_area_high = _parse_change_surface_kinds(
        data.get("surface_area_high"),
        field_label="backlog_policy.surface_area_high",
    )
    breadth_min = _parse_breadth_min(
        data.get("breadth_min_for_surface_area_high"),
        field_label="backlog_policy.breadth_min_for_surface_area_high",
    )
    default_stage = _parse_stage(
        data.get("default_stage_for_high_surface_low_breadth"),
        field_label="backlog_policy.default_stage_for_high_surface_low_breadth",
        default="research_required",
    )
    investigation_steps = _parse_investigation_steps(
        data.get("investigation_steps_for_high_surface_low_breadth"),
        field_label="backlog_policy.investigation_steps_for_high_surface_low_breadth",
    )
    return HighSurfaceRule(
        rule_id="legacy_high_surface",
        applies_to_kinds=surface_area_high,
        breadth_min=breadth_min,
        default_stage_for_low_breadth=default_stage,
        investigation_steps=investigation_steps,
        risk_tag="overfitting_risk",
        review_domain="command_surface",
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
        "rules_total": len(config.high_surface_rules),
        "rules_matched": {},
        "rules_low_breadth": {},
    }

    updated: list[dict[str, Any]] = []
    for ticket in tickets:
        item = dict(ticket)

        stage = _coerce_string(item.get("stage")) or "triage"
        if stage not in TICKET_STAGE_ENUM:
            stage = "triage"

        ready_prereqs, readiness_reasons = assess_ticket_readiness(item)
        research_ready = _has_ready_research_proof(item)
        item["ticket_readiness"] = {
            "ready": ready_prereqs,
            "reasons": readiness_reasons,
        }

        change_surface_raw = item.get("change_surface")
        change_surface = change_surface_raw if isinstance(change_surface_raw, dict) else {}
        kinds_raw = _coerce_string_list(change_surface.get("kinds"))
        kinds = [kind for kind in kinds_raw if kind in CHANGE_SURFACE_KIND_ENUM]
        if not kinds:
            kinds = ["unknown"]

        labeled = "unknown" not in kinds
        kind_set = set(kinds)

        breadth_raw = item.get("breadth")
        breadth = breadth_raw if isinstance(breadth_raw, dict) else {}
        breadth_counts = {
            dim: _coerce_int(breadth.get(dim), default=0)
            for dim in ("runs", "missions", "targets", "repo_inputs", "agents", "personas")
        }

        matched_rules = [
            rule for rule in config.high_surface_rules if kind_set & set(rule.applies_to_kinds)
        ]
        for rule in matched_rules:
            meta_rules_matched = meta["rules_matched"]
            meta_rules_matched[rule.rule_id] = int(meta_rules_matched.get(rule.rule_id, 0)) + 1

        failing_rules: list[HighSurfaceRule] = []
        for rule in matched_rules:
            for dim, threshold in rule.breadth_min.items():
                if breadth_counts.get(dim, 0) < threshold:
                    failing_rules.append(rule)
                    meta_rules_low = meta["rules_low_breadth"]
                    meta_rules_low[rule.rule_id] = int(meta_rules_low.get(rule.rule_id, 0)) + 1
                    break

        new_stage = stage
        risks_to_add: list[str] = []
        steps_to_add: list[str] = []

        # Guardrail: `ready_for_ticket` is only valid once a selected solution
        # and a change plan exist and research has established the mechanism.
        if stage == "ready_for_ticket" and not ready_prereqs:
            new_stage = "research_required"
            if any(
                reason.startswith(("selection_", "solution_option_", "change_plan_"))
                for reason in readiness_reasons
            ):
                risks_to_add.append("missing_change_plan")
            if not research_ready:
                risks_to_add.append("research_evidence_incomplete")

        if labeled:
            if matched_rules and failing_rules and stage != "blocked":
                new_stage = failing_rules[0].default_stage_for_low_breadth
                for rule in failing_rules:
                    if rule.risk_tag not in risks_to_add:
                        risks_to_add.append(rule.risk_tag)
                    steps_to_add.extend(list(rule.investigation_steps))
            elif stage == "triage":
                new_stage = (
                    config.default_stage_for_labeled
                    if ready_prereqs
                    else "research_required"
                )

        # No policy rule, UX label, or configured default can override the evidence
        # chain. Routing remains fail-closed at the final transition boundary.
        if new_stage == "ready_for_ticket" and not ready_prereqs:
            new_stage = "research_required"
            if "readiness_contract_failed" not in risks_to_add:
                risks_to_add.append("readiness_contract_failed")

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
