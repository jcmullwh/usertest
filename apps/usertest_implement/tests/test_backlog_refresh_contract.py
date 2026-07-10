from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from usertest_implement.backlog_refresh import (
    BacklogRefreshError,
    BacklogRefreshRequest,
    build_refresh_commands,
    run_shadow_backlog_refresh,
)


def _request(tmp_path: Path) -> BacklogRefreshRequest:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    return BacklogRefreshRequest(
        repo_root=repo_root,
        repo_input=str(repo_root),
        runs_dir=tmp_path / "runs",
        target="usertest",
        backlog_python=tmp_path / "python.exe",
        research_ref="origin/dev",
        breadth_profile="internal_maintenance",
        agent="codex",
        model="gpt-5.5",
    ).normalized()


def _cycle_id(index: int) -> str:
    return hashlib.sha256(f"cycle:{index}".encode()).hexdigest()


def _write_shadow_state(
    request: BacklogRefreshRequest,
    cycles: list[dict[str, object]],
) -> None:
    request.shadow_state_json.parent.mkdir(parents=True, exist_ok=True)
    for index, cycle in enumerate(cycles):
        if "artifact_receipts" in cycle:
            continue
        cycle_dir = request.compiled_dir / "test-cycles" / str(index)
        cycle_dir.mkdir(parents=True, exist_ok=True)
        registry = cycle_dir / "case_registry.json"
        registry.write_text('{"schema_version": 1, "cases": {}}\n', encoding="utf-8")
        registry_hash = hashlib.sha256(registry.read_bytes()).hexdigest()
        atoms = cycle_dir / "atoms.jsonl"
        atoms.write_text(
            json.dumps(
                {
                    "atom_id": f"atom:{index}",
                    "run_rel": f"target/20260710/codex/{index}",
                    "timestamp_utc": f"2026-07-10T12:00:0{index}Z",
                    "evidence_role": "observation",
                    "source": "confusion_point",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        atoms_hash = hashlib.sha256(atoms.read_bytes()).hexdigest()
        artifact_receipts = [
            {
                "name": "case_registry",
                "snapshot_path": str(registry),
                "sha256": registry_hash,
                "content_sha256": hashlib.sha256(b"{}").hexdigest(),
            },
            {
                "name": "atoms",
                "snapshot_path": str(atoms),
                "sha256": atoms_hash,
                "content_sha256": hashlib.sha256(b"atoms").hexdigest(),
            },
        ]
        cycle.update(
            {
                "generated_at": f"2026-07-10T12:00:0{index}Z",
                "artifact_receipts": artifact_receipts,
            }
        )
        cycle_receipt = cycle_dir / "cycle_receipt.json"
        cycle_receipt.write_text(json.dumps(cycle) + "\n", encoding="utf-8")
        cycle["cycle_receipt_path"] = str(cycle_receipt)
        cycle["cycle_receipt_sha256"] = hashlib.sha256(
            cycle_receipt.read_bytes()
        ).hexdigest()
    state = {
        "ready_for_export": len(cycles) >= 2,
        "consecutive_stable_passes": len(cycles),
        "validated_cycle_id": cycles[-1]["cycle_id"],
        "validated_backlog_sha256": "a" * 64,
        "cycles": cycles,
    }
    request.shadow_state_json.write_text(
        json.dumps(state, indent=2) + "\n",
        encoding="utf-8",
    )


def test_refresh_contract_is_locked_fresh_and_immediately_exported(tmp_path: Path) -> None:
    request = _request(tmp_path)
    commands = build_refresh_commands(request)

    assert [label for label, _ in commands] == [
        "preliminary shadow",
        "intent snapshot",
        "UX review",
        "qualifying shadow 1",
        "qualifying shadow 2",
        "ticket export",
    ]
    shadow_commands = [argv for label, argv in commands if "shadow" in label]
    assert len(shadow_commands) == 3
    assert shadow_commands[0] == shadow_commands[1] == shadow_commands[2]
    for argv in shadow_commands:
        assert "--shadow" in argv
        assert "--force" in argv
        assert "--no-resume" in argv
        assert argv[argv.index("--research-ref") + 1] == "origin/dev"
        assert argv[argv.index("--breadth-profile") + 1] == "internal_maintenance"
        assert argv[argv.index("--agent") + 1] == "codex"
        assert argv[argv.index("--model") + 1] == "gpt-5.5"

    observed: list[str] = []
    cycles: list[dict[str, object]] = []

    def execute(argv: list[str], cwd: Path, label: str) -> None:
        assert cwd == request.repo_root
        assert request.lock_path.is_file()
        observed.append(label)
        if "shadow" in label:
            cycles.append({"cycle_id": _cycle_id(len(cycles) + 1), "passed": True})
            _write_shadow_state(request, cycles)
        if label == "ticket export":
            request.export_json.write_text('{"exports": []}\n', encoding="utf-8")

    export_path = run_shadow_backlog_refresh(
        request,
        command_executor=execute,
    )

    assert export_path == request.export_json
    assert observed == [label for label, _ in commands]
    receipt = json.loads(request.receipt_path.read_text(encoding="utf-8"))
    assert receipt["preliminary_cycle_id"] == _cycle_id(1)
    assert receipt["qualifying_cycle_ids"] == [_cycle_id(2), _cycle_id(3)]
    assert receipt["schema_version"] == 3
    assert len(receipt["qualifying_cycles"]) == 2
    assert receipt["qualifying_cycles"][0]["source_observation_window"][
        "source_run_count"
    ] == 1
    assert len(receipt["receipt_content_sha256"]) == 64
    assert receipt["configuration"]["research_ref"] == "origin/dev"
    assert receipt["configuration"]["actions_yaml"].endswith("backlog_actions.yaml")


def test_refresh_ignores_unrelated_idea_and_release_pull_requests(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    executed: list[str] = []
    cycles: list[dict[str, object]] = []

    def execute(_argv: list[str], _cwd: Path, label: str) -> None:
        executed.append(label)
        if "shadow" in label:
            cycles.append({"cycle_id": _cycle_id(len(cycles) + 1), "passed": True})
            _write_shadow_state(request, cycles)
        if label == "ticket export":
            request.export_json.write_text('{"exports": []}\n', encoding="utf-8")

    probe_called = False

    def unrelated_prs(_: BacklogRefreshRequest) -> list[dict[str, object]]:
        nonlocal probe_called
        probe_called = True
        return [
            {
                "number": 212,
                "headRefName": "backlog/650b207f6203",
                "baseRefName": "dev",
                "origin": "IDEA",
            },
            {
                "number": 196,
                "headRefName": "codex/release-v0.2.0",
                "baseRefName": "main",
            },
        ]

    export_path = run_shadow_backlog_refresh(
        request,
        command_executor=execute,
        open_pr_probe=unrelated_prs,
    )

    assert export_path == request.export_json
    assert executed == [label for label, _ in build_refresh_commands(request)]
    assert request.export_json.is_file()
    # PR discovery is deliberately no longer part of the refresh correctness
    # boundary.  Active generated cases are suppressed by case/plan provenance
    # during export instead of blocking unrelated evidence work.
    assert probe_called is False


def test_refresh_refuses_export_when_qualifying_shadows_are_not_stable(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    cycles: list[dict[str, object]] = []
    executed: list[str] = []

    def execute(_argv: list[str], _cwd: Path, label: str) -> None:
        executed.append(label)
        if "shadow" not in label:
            return
        cycles.append(
            {
                "cycle_id": _cycle_id(len(cycles) + 1),
                "passed": label != "qualifying shadow 2",
            }
        )
        _write_shadow_state(request, cycles)

    with pytest.raises(BacklogRefreshError, match="did not pass"):
        run_shadow_backlog_refresh(
            request,
            command_executor=execute,
            open_pr_probe=lambda _: [],
        )

    assert "ticket export" not in executed
    assert not request.export_json.exists()
