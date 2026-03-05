from __future__ import annotations

from pathlib import Path

from backlog_core.backlog_policy import BacklogPolicyConfig, apply_backlog_policy


def _legacy_policy() -> BacklogPolicyConfig:
    return BacklogPolicyConfig.from_dict(
        {
            "surface_area_high": [
                "new_command",
                "breaking_change",
                "new_top_level_mode",
                "new_config_schema",
                "new_api",
            ],
            "breadth_min_for_surface_area_high": {"missions": 2, "targets": 2, "repo_inputs": 2},
            "default_stage_for_high_surface_low_breadth": "research_required",
            "default_stage_for_labeled": "ready_for_ticket",
        }
    )


def _grouped_policy() -> BacklogPolicyConfig:
    return BacklogPolicyConfig.from_dict(
        {
            "default_stage_for_labeled": "ready_for_ticket",
            "high_surface_rules": [
                {
                    "rule_id": "command_surface",
                    "applies_to_kinds": [
                        "new_command",
                        "new_top_level_mode",
                        "new_config_schema",
                    ],
                    "breadth_min": {"missions": 2, "targets": 2, "repo_inputs": 2},
                    "default_stage_for_low_breadth": "research_required",
                    "investigation_steps": [
                        "Validate repo intent",
                        "Check if existing commands or flags can be parameterized",
                    ],
                    "risk_tag": "overfitting_risk",
                    "review_domain": "command_surface",
                },
                {
                    "rule_id": "behavior_compat",
                    "applies_to_kinds": ["breaking_change", "new_api"],
                    "breadth_min": {"runs": 5, "agents": 2},
                    "default_stage_for_low_breadth": "research_required",
                    "investigation_steps": [
                        "Validate recurrence breadth across runs and agents",
                        "Check compatibility impact within existing surfaces",
                    ],
                    "risk_tag": "compatibility_risk",
                    "review_domain": "behavior_compat",
                },
            ],
        }
    )


def test_policy_legacy_flat_config_still_parses_and_exposes_surface_area() -> None:
    cfg = _legacy_policy()

    assert {
        "new_command",
        "breaking_change",
        "new_top_level_mode",
        "new_config_schema",
        "new_api",
    } == set(cfg.surface_area_high)
    assert cfg.breadth_min_for_surface_area_high == {"missions": 2, "targets": 2, "repo_inputs": 2}
    assert cfg.default_stage_for_high_surface_low_breadth == "research_required"


def test_policy_high_surface_low_breadth_routes_to_research_required() -> None:
    cfg = _legacy_policy()
    ticket = {
        "title": "Add a new top-level command for onboarding",
        "stage": "triage",
        "risks": [],
        "investigation_steps": [],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "Adds a new command.",
        },
        "breadth": {"runs": 1, "missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1},
    }

    updated, meta = apply_backlog_policy([ticket], config=cfg)
    assert meta["tickets_total"] == 1
    assert updated[0]["stage"] == "research_required"
    assert "overfitting_risk" in updated[0]["risks"]
    assert "Validate repo intent" in updated[0]["investigation_steps"]


def test_policy_docs_change_requires_plan_before_ready() -> None:
    cfg = _legacy_policy()
    ticket = {
        "title": "Fix quickstart docs",
        "stage": "triage",
        "risks": [],
        "investigation_steps": [],
        "change_surface": {"user_visible": True, "kinds": ["docs_change"], "notes": "Docs only."},
        "breadth": {"runs": 1, "missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1},
    }

    updated, _ = apply_backlog_policy([ticket], config=cfg)
    assert updated[0]["stage"] == "research_required"
    assert "overfitting_risk" not in updated[0]["risks"]


def test_policy_high_surface_high_breadth_can_be_ready() -> None:
    cfg = _legacy_policy()
    ticket = {
        "title": "Add a new command",
        "stage": "triage",
        "risks": [],
        "investigation_steps": [],
        "selected_option_id": "option:test:most_robust",
        "change_plan_id": "plan:test:1",
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command requested.",
        },
        "breadth": {"runs": 6, "missions": 4, "targets": 2, "repo_inputs": 2, "agents": 3},
    }

    updated, _ = apply_backlog_policy([ticket], config=cfg)
    assert updated[0]["stage"] == "ready_for_ticket"
    assert "overfitting_risk" not in updated[0]["risks"]


def test_policy_grouped_rule_behavior_compat_can_pass_with_internal_observation_breadth() -> None:
    cfg = _grouped_policy()
    ticket = {
        "title": "Harden startup compatibility checks",
        "stage": "triage",
        "risks": [],
        "investigation_steps": [],
        "selected_option_id": "option:test:most_comprehensive",
        "change_plan_id": "plan:test:1",
        "change_surface": {
            "user_visible": True,
            "kinds": ["breaking_change"],
            "notes": "Existing surface hardening that can block previously permissive runs.",
        },
        "breadth": {"runs": 6, "missions": 1, "targets": 1, "repo_inputs": 1, "agents": 2},
    }

    updated, meta = apply_backlog_policy([ticket], config=cfg)
    assert updated[0]["stage"] == "ready_for_ticket"
    assert "compatibility_risk" not in updated[0]["risks"]
    assert meta["rules_matched"]["behavior_compat"] == 1


def test_policy_grouped_rule_command_surface_still_requires_cross_context_breadth() -> None:
    cfg = _grouped_policy()
    ticket = {
        "title": "Add a new top-level shortcut",
        "stage": "triage",
        "risks": [],
        "investigation_steps": [],
        "selected_option_id": "option:test:most_comprehensive",
        "change_plan_id": "plan:test:1",
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command surface.",
        },
        "breadth": {"runs": 12, "missions": 1, "targets": 1, "repo_inputs": 1, "agents": 3},
    }

    updated, _ = apply_backlog_policy([ticket], config=cfg)
    assert updated[0]["stage"] == "research_required"
    assert "overfitting_risk" in updated[0]["risks"]


def test_policy_grouped_rule_mixed_kinds_require_all_matching_rules_to_pass() -> None:
    cfg = _grouped_policy()
    ticket = {
        "title": "Add new command and tighten behavior",
        "stage": "triage",
        "risks": [],
        "investigation_steps": [],
        "selected_option_id": "option:test:most_comprehensive",
        "change_plan_id": "plan:test:1",
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command", "breaking_change"],
            "notes": "Mixed command-surface plus behavior-compat change.",
        },
        "breadth": {"runs": 9, "missions": 1, "targets": 1, "repo_inputs": 1, "agents": 2},
    }

    updated, _ = apply_backlog_policy([ticket], config=cfg)
    assert updated[0]["stage"] == "research_required"
    assert "overfitting_risk" in updated[0]["risks"]
    assert "compatibility_risk" not in updated[0]["risks"]


def test_policy_module_avoids_regex_gating_guardrail() -> None:
    import backlog_core.backlog_policy as mod

    path = Path(mod.__file__).resolve()
    text = path.read_text(encoding="utf-8")
    assert "re.compile(" not in text
    assert "\nimport re\n" not in text
