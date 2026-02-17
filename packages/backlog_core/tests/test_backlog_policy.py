from __future__ import annotations

from pathlib import Path

from backlog_core.backlog_policy import BacklogPolicyConfig, apply_backlog_policy


def _default_policy() -> BacklogPolicyConfig:
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


def test_policy_high_surface_low_breadth_routes_to_research_required() -> None:
    cfg = _default_policy()
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


def test_policy_docs_change_can_be_ready_with_narrow_breadth() -> None:
    cfg = _default_policy()
    ticket = {
        "title": "Fix quickstart docs",
        "stage": "triage",
        "risks": [],
        "investigation_steps": [],
        "change_surface": {"user_visible": True, "kinds": ["docs_change"], "notes": "Docs only."},
        "breadth": {"runs": 1, "missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1},
    }

    updated, _ = apply_backlog_policy([ticket], config=cfg)
    assert updated[0]["stage"] == "ready_for_ticket"
    assert "overfitting_risk" not in updated[0]["risks"]


def test_policy_high_surface_high_breadth_can_be_ready() -> None:
    cfg = _default_policy()
    ticket = {
        "title": "Add a new command",
        "stage": "triage",
        "risks": [],
        "investigation_steps": [],
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


def test_policy_module_avoids_regex_gating_guardrail() -> None:
    import backlog_core.backlog_policy as mod

    path = Path(mod.__file__).resolve()
    text = path.read_text(encoding="utf-8")
    assert "re.compile(" not in text
    assert "\nimport re\n" not in text
