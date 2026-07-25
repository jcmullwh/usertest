from __future__ import annotations

import json
from pathlib import Path

import pytest
from runner_core import RunResult, find_repo_root

from usertest.cli import main
from usertest.commands import batch as batch_command
from usertest.commands import matrix as matrix_command
from usertest.commands import run as run_command


def _invoke(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return int(exc.value.code)


def test_failed_verification_exit_code_propagates_through_public_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    target = tmp_path / "target"
    target.mkdir()
    (target / "README.md").write_text("# target\n", encoding="utf-8")

    fake_run_dir = tmp_path / "failed-run"
    fake_run_dir.mkdir()
    failed_result = RunResult(
        run_dir=fake_run_dir,
        exit_code=1,
        report_validation_errors=[],
    )
    monkeypatch.setattr(run_command, "run_once", lambda *_args, **_kwargs: failed_result)
    monkeypatch.setattr(matrix_command, "run_once", lambda *_args, **_kwargs: failed_result)
    monkeypatch.setattr(batch_command, "run_once", lambda *_args, **_kwargs: failed_result)

    matrix_path = tmp_path / "matrix.yaml"
    matrix_path.write_text(
        "\n".join(
            [
                f"repo: {target.as_posix()!r}",
                "policy: safe",
                "personas: [quickstart_sprinter]",
                "missions: [first_output_smoke]",
                "seeds: [0]",
                "agents:",
                "  - agent: codex",
                "    policy: safe",
                "",
            ]
        ),
        encoding="utf-8",
    )
    targets_path = tmp_path / "targets.yaml"
    targets_path.write_text(
        "\n".join(
            [
                "targets:",
                f"  - repo: {target.as_posix()!r}",
                "    agent: codex",
                "    policy: safe",
                "    persona_id: quickstart_sprinter",
                "    mission_id: first_output_smoke",
                "",
            ]
        ),
        encoding="utf-8",
    )

    run_exit_code = _invoke(
        [
            "run",
            "--repo-root",
            str(repo_root),
            "--repo",
            str(target),
            "--exec-backend",
            "local",
        ]
    )
    matrix_exit_code = _invoke(
        [
            "matrix",
            "run",
            "--repo-root",
            str(repo_root),
            "--spec",
            str(matrix_path),
            "--exec-backend",
            "local",
            "--skip-command-probes",
            "--out-targets",
            str(tmp_path / "compiled-targets.yaml"),
            "--out-report",
            str(tmp_path / "matrix-report.json"),
        ]
    )
    batch_exit_code = _invoke(
        [
            "batch",
            "--repo-root",
            str(repo_root),
            "--targets",
            str(targets_path),
            "--exec-backend",
            "local",
            "--skip-command-probes",
        ]
    )

    assert run_exit_code == 2
    assert matrix_exit_code == 2
    assert batch_exit_code == 2
    print(
        json.dumps(
            {
                "batch_exit_code": batch_exit_code,
                "matrix_exit_code": matrix_exit_code,
                "run_exit_code": run_exit_code,
            },
            sort_keys=True,
        )
    )
