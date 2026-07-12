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
    release_cycles = [cycle for cycle in cycles if cycle.get("cycle_mode") == "release"]
    operational = cycles[-1] if cycles and cycles[-1].get("cycle_mode") == "operational" else None
    anchor_ids = [str(cycle["cycle_id"]) for cycle in release_cycles[-2:]] if operational else []
    ready = len(release_cycles) >= 2 and (operational is None or operational.get("passed") is True)
    state = {
        "ready_for_export": ready,
        "required_consecutive_cycles": 2,
        "consecutive_stable_passes": len(release_cycles) if ready else 0,
        "activation_mode": (
            "operational_bound" if ready and operational is not None else "release_qualification"
        ),
        "release_anchor_cycle_ids": anchor_ids,
        "release_anchor_stability_inputs_sha256": "s" * 64 if anchor_ids else None,
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
        "operational shadow materialization",
        "intent snapshot",
        "UX review",
        "operational shadow validation",
        "ticket export",
    ]
    shadow_commands = [argv for label, argv in commands if "shadow" in label]
    assert len(shadow_commands) == 2
    assert "--score-operational-shadow" not in shadow_commands[0]
    assert "--score-operational-shadow" in shadow_commands[1]
    for argv in shadow_commands:
        assert "--operational-shadow" in argv
        assert "--shadow" not in argv
        assert "--force" in argv
        assert "--no-resume" in argv
        assert argv[argv.index("--research-ref") + 1] == "origin/dev"
        assert argv[argv.index("--breadth-profile") + 1] == "internal_maintenance"
        assert argv[argv.index("--agent") + 1] == "codex"
        assert argv[argv.index("--model") + 1] == "gpt-5.5"
        assert argv[argv.index("--shadow-state") + 1] == str(
            request.shadow_state_json
        )

    observed: list[str] = []
    cycles: list[dict[str, object]] = [
        {
            "cycle_id": _cycle_id(1),
            "cycle_mode": "release",
            "passed": True,
            "stability_inputs_sha256": "s" * 64,
            "qualification": {
                "status": "verified",
                "qualification_class": "positive_throughput",
            },
        },
        {
            "cycle_id": _cycle_id(2),
            "cycle_mode": "release",
            "passed": True,
            "stability_inputs_sha256": "s" * 64,
            "qualification": {
                "status": "verified",
                "qualification_class": "positive_throughput",
            },
        },
    ]
    _write_shadow_state(request, cycles)

    def execute(argv: list[str], cwd: Path, label: str) -> None:
        assert cwd == request.repo_root
        assert request.lock_path.is_file()
        observed.append(label)
        if label == "operational shadow validation":
            cycles.append(
                {
                    "cycle_id": _cycle_id(3),
                    "cycle_mode": "operational",
                    "passed": True,
                    "stability_inputs_sha256": "s" * 64,
                    "qualification": {
                        "status": "missing",
                        "qualification_class": "unqualified",
                    },
                }
            )
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
    assert receipt["operational_cycle_id"] == _cycle_id(3)
    assert receipt["observation_cycle_ids"] == [_cycle_id(3)]
    assert receipt["schema_version"] == 4
    assert receipt["activation_mode"] == "operational_bound"
    assert receipt["release_anchor_cycle_ids"] == [_cycle_id(1), _cycle_id(2)]
    assert receipt["observation_cycles"][0]["source_observation_window"][
        "source_run_count"
    ] == 1
    assert len(receipt["receipt_content_sha256"]) == 64
    assert receipt["configuration"]["research_ref"] == "origin/dev"
    assert receipt["configuration"]["actions_yaml"].endswith("backlog_actions.yaml")


def test_external_release_state_is_shared_by_operational_materialize_and_score(
    tmp_path: Path,
) -> None:
    base = _request(tmp_path)
    external_state = (tmp_path / "qualification-custody" / "release_state.json").resolve()
    request = BacklogRefreshRequest(
        repo_root=base.repo_root,
        repo_input=base.repo_input,
        runs_dir=base.runs_dir,
        target=base.target,
        backlog_python=base.backlog_python,
        research_ref=base.research_ref,
        breadth_profile=base.breadth_profile,
        agent=base.agent,
        model=base.model,
        actions_yaml=base.actions_yaml,
        atom_actions_yaml=base.atom_actions_yaml,
        qualified_shadow_state_path=external_state,
    ).normalized()

    commands = build_refresh_commands(request)
    shadow_commands = [argv for label, argv in commands if "shadow" in label]

    assert request.shadow_state_json == external_state
    assert len(shadow_commands) == 2
    assert all(
        argv[argv.index("--shadow-state") + 1] == str(external_state)
        for argv in shadow_commands
    )


def test_refresh_ignores_unrelated_idea_and_release_pull_requests(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    executed: list[str] = []
    cycles: list[dict[str, object]] = [
        {
            "cycle_id": _cycle_id(1),
            "cycle_mode": "release",
            "passed": True,
            "stability_inputs_sha256": "s" * 64,
            "qualification": {"status": "verified", "qualification_class": "positive_throughput"},
        },
        {
            "cycle_id": _cycle_id(2),
            "cycle_mode": "release",
            "passed": True,
            "stability_inputs_sha256": "s" * 64,
            "qualification": {"status": "verified", "qualification_class": "positive_throughput"},
        },
    ]
    _write_shadow_state(request, cycles)

    def execute(_argv: list[str], _cwd: Path, label: str) -> None:
        executed.append(label)
        if label == "operational shadow validation":
            cycles.append(
                {
                    "cycle_id": _cycle_id(3),
                    "cycle_mode": "operational",
                    "passed": True,
                    "stability_inputs_sha256": "s" * 64,
                    "qualification": {"status": "missing", "qualification_class": "unqualified"},
                }
            )
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


def test_refresh_refuses_export_when_operational_validation_fails(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    cycles: list[dict[str, object]] = [
        {
            "cycle_id": _cycle_id(1),
            "cycle_mode": "release",
            "passed": True,
            "stability_inputs_sha256": "s" * 64,
            "qualification": {"status": "verified", "qualification_class": "positive_throughput"},
        },
        {
            "cycle_id": _cycle_id(2),
            "cycle_mode": "release",
            "passed": True,
            "stability_inputs_sha256": "s" * 64,
            "qualification": {"status": "verified", "qualification_class": "positive_throughput"},
        },
    ]
    _write_shadow_state(request, cycles)
    executed: list[str] = []

    def execute(_argv: list[str], _cwd: Path, label: str) -> None:
        executed.append(label)
        if label != "operational shadow validation":
            return
        cycles.append(
            {
                "cycle_id": _cycle_id(3),
                "cycle_mode": "operational",
                "passed": False,
                "stability_inputs_sha256": "s" * 64,
                "qualification": {"status": "missing", "qualification_class": "unqualified"},
            }
        )
        _write_shadow_state(request, cycles)

    with pytest.raises(BacklogRefreshError, match="failed depth invariants"):
        run_shadow_backlog_refresh(
            request,
            command_executor=execute,
            open_pr_probe=lambda _: [],
        )

    assert "ticket export" not in executed
    assert not request.export_json.exists()


def test_refresh_never_reuses_a_stale_release_cycle_as_current_operation(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    cycles: list[dict[str, object]] = [
        {
            "cycle_id": _cycle_id(1),
            "cycle_mode": "release",
            "passed": True,
            "stability_inputs_sha256": "s" * 64,
            "qualification": {"status": "verified", "qualification_class": "positive_throughput"},
        },
        {
            "cycle_id": _cycle_id(2),
            "cycle_mode": "release",
            "passed": True,
            "stability_inputs_sha256": "s" * 64,
            "qualification": {"status": "verified", "qualification_class": "positive_throughput"},
        },
    ]
    _write_shadow_state(request, cycles)
    executed: list[str] = []

    def execute(_argv: list[str], _cwd: Path, label: str) -> None:
        executed.append(label)
        # A broken materializer/validator that leaves only old state must not be
        # mistaken for a successful current run.

    with pytest.raises(BacklogRefreshError, match="exactly one fresh latest cycle"):
        run_shadow_backlog_refresh(request, command_executor=execute)

    assert executed == [
        "operational shadow materialization",
        "intent snapshot",
        "UX review",
        "operational shadow validation",
    ]
    assert "ticket export" not in executed
