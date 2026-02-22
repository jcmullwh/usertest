from __future__ import annotations

from pathlib import Path

from backlog_core.aggregate_metrics import build_aggregate_metrics_atoms


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
