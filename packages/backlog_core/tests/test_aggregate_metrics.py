from __future__ import annotations

from pathlib import Path

from backlog_core.aggregate_metrics import build_aggregate_metrics_atoms
from backlog_core.case_lineage import eligible_problem_mining_atoms, normalize_atom_lineage


def test_research_run_cannot_change_observation_aggregates_or_mining_eligibility(
    tmp_path: Path,
) -> None:
    source_run = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    research_run = tmp_path / "runs" / "target_a" / "20260102T000000Z" / "codex" / "0"
    source_run.mkdir(parents=True, exist_ok=True)
    research_run.mkdir(parents=True, exist_ok=True)
    source_rel = "target_a/20260101T000000Z/codex/0"
    research_rel = "target_a/20260102T000000Z/codex/0"
    source_record = {
        "run_dir": str(source_run),
        "run_rel": source_rel,
        "agent": "codex",
        "target_slug": "target_a",
        "target_ref": {
            "repo_input": "I:/code/usertest",
            "mission_id": "first_output_smoke",
            "persona_id": "quickstart_sprinter",
        },
        "metrics": {
            "commands_executed": 10,
            "commands_failed": 1,
            "failed_commands": [
                {
                    "command": "python -m pytest tests/test_smoke.py",
                    "exit_code": 1,
                    "output_excerpt": "one source failure",
                }
            ],
        },
    }
    research_record = {
        "run_dir": str(research_run),
        "run_rel": research_rel,
        "agent": "codex",
        "target_slug": "target_a",
        "target_ref": {
            "repo_input": "I:/code/usertest",
            "requested_mission_id": "backlog_repro_research",
            "persona_id": "repo_backlog_investigator",
        },
        "metrics": {
            "commands_executed": 100,
            "commands_failed": 100,
            "failed_commands": [
                {
                    "command": "python fabricated_research_failure.py",
                    "exit_code": 1,
                    "output_excerpt": "research infrastructure failure",
                }
            ],
        },
    }

    source_atoms = build_aggregate_metrics_atoms(
        [source_record],
        eligible_run_rels={source_rel},
        run_id_prefix="__aggregate__/target_a/all",
    )
    with_research_atoms = build_aggregate_metrics_atoms(
        [source_record, research_record],
        eligible_run_rels={source_rel, research_rel},
        run_id_prefix="__aggregate__/target_a/all",
    )

    assert with_research_atoms == source_atoms
    normalized_source = normalize_atom_lineage(source_atoms, strict_new_output=True)
    normalized_with_research = normalize_atom_lineage(
        with_research_atoms,
        strict_new_output=True,
    )
    assert eligible_problem_mining_atoms(normalized_with_research) == (
        eligible_problem_mining_atoms(normalized_source)
    )
    assert build_aggregate_metrics_atoms(
        [research_record],
        eligible_run_rels={research_rel},
        run_id_prefix="__aggregate__/target_a/all",
    ) == []


def test_build_aggregate_metrics_atoms_emits_breakdowns(tmp_path: Path) -> None:
    run_a = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_b = tmp_path / "runs" / "target_a" / "20260102T000000Z" / "codex" / "0"
    run_a.mkdir(parents=True, exist_ok=True)
    run_b.mkdir(parents=True, exist_ok=True)

    records = [
        {
            "run_dir": str(run_a),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "agent": "codex",
            "target_slug": "target_a",
            "target_ref": {
                "repo_input": "I:/code/usertest",
                "mission_id": "first_output_smoke",
                "persona_id": "quickstart_sprinter",
            },
            "metrics": {
                "commands_executed": 10,
                "commands_failed": 1,
                "failed_commands": [
                    {
                        "command": "python -m pip install -r requirements-dev.txt",
                        "exit_code": 1,
                        "output_excerpt": "Temporary failure in name resolution",
                    }
                ],
            },
        },
        {
            "run_dir": str(run_b),
            "run_rel": "target_a/20260102T000000Z/codex/0",
            "agent": "codex",
            "target_slug": "target_a",
            "target_ref": {
                "repo_input": "I:/code/usertest",
                "mission_id": "first_output_smoke",
                "persona_id": "quickstart_sprinter",
            },
            "metrics": {
                "commands_executed": 11,
                "commands_failed": 1,
                "failed_commands": [
                    {
                        "command": "python -m pip install -r requirements-dev.txt",
                        "exit_code": 1,
                        "output_excerpt": "Temporary failure in name resolution",
                    }
                ],
            },
        },
    ]

    atoms = build_aggregate_metrics_atoms(
        records,
        eligible_run_rels={
            "target_a/20260101T000000Z/codex/0",
            "target_a/20260102T000000Z/codex/0",
        },
        run_id_prefix="__aggregate__/target_a/all",
    )
    assert len(atoms) == 2

    baseline = atoms[0]
    assert baseline["source"] == "aggregate_metrics"
    breakdown = baseline["command_failure_breakdown"]
    assert breakdown["total_failed_commands"] == 2
    assert breakdown["failure_kind_counts"]["network_name_resolution"] == 2
    expected_command = "python -m pip install -r requirements-dev.txt"
    assert breakdown["top_failed_commands"][0]["command"] == expected_command
    assert breakdown["top_failed_commands"][0]["failures"] == 2


def test_build_aggregate_metrics_atoms_ignores_ripgrep_no_matches(tmp_path: Path) -> None:
    run_a = tmp_path / "runs" / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_b = tmp_path / "runs" / "target_a" / "20260102T000000Z" / "codex" / "0"
    run_a.mkdir(parents=True, exist_ok=True)
    run_b.mkdir(parents=True, exist_ok=True)

    records = [
        {
            "run_dir": str(run_a),
            "run_rel": "target_a/20260101T000000Z/codex/0",
            "agent": "codex",
            "target_slug": "target_a",
            "target_ref": {
                "repo_input": "I:/code/usertest",
                "mission_id": "first_output_smoke",
                "persona_id": "quickstart_sprinter",
            },
            "metrics": {
                "commands_executed": 10,
                "commands_failed": 1,
                "failed_commands": [
                    {
                        "command": "rg -n \"WindowsApps\" README.md docs -S",
                        "exit_code": 1,
                        "output_excerpt": "",
                    }
                ],
            },
        },
        {
            "run_dir": str(run_b),
            "run_rel": "target_a/20260102T000000Z/codex/0",
            "agent": "codex",
            "target_slug": "target_a",
            "target_ref": {
                "repo_input": "I:/code/usertest",
                "mission_id": "first_output_smoke",
                "persona_id": "quickstart_sprinter",
            },
            "metrics": {
                "commands_executed": 11,
                "commands_failed": 1,
                "failed_commands": [
                    {
                        "command": "rg -n \"WindowsApps\" README.md docs -S",
                        "exit_code": 1,
                        "output_excerpt": "",
                    }
                ],
            },
        },
    ]

    atoms = build_aggregate_metrics_atoms(
        records,
        eligible_run_rels={
            "target_a/20260101T000000Z/codex/0",
            "target_a/20260102T000000Z/codex/0",
        },
        run_id_prefix="__aggregate__/target_a/all",
    )
    assert len(atoms) == 2
    assert "command_failure_breakdown" not in atoms[0]
    assert "command_failure_breakdown" not in atoms[1]
