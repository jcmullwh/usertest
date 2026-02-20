from __future__ import annotations

import json

from backlog_miner.ensemble import _build_miner_prompt


def test_build_miner_prompt_includes_report_metadata_fields() -> None:
    atoms = [
        {
            "atom_id": "run:confusion_point:1",
            "run_rel": "target/20260101T000000Z/codex/0",
            "agent": "codex",
            "status": "ok",
            "source": "confusion_point",
            "severity_hint": "high",
            "text": "README quickstart missing",
            "report_kind": "task_run_v1",
            "report_issue_block": "issues",
            "issue_severity": "error",
            "issue_title": "README quickstart missing",
            "evidence_text": "README.md",
            "path_anchors": ["readme.md"],
            "linked_atom_ids": [],
        }
    ]
    rendered = _build_miner_prompt(template_text="{{ATOMS_JSON}}", atoms=atoms, max_tickets_per_miner=3)
    payload = json.loads(rendered)
    atom = payload["atoms"][0]

    for key in (
        "report_kind",
        "report_block",
        "report_issue_block",
        "report_ux_block",
        "issue_severity",
        "issue_title",
        "evidence_text",
        "path_anchors",
        "linked_atom_ids",
    ):
        assert key in atom
