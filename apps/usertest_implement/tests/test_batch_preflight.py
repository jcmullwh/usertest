from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from usertest_implement import batch_preflight


def _completed(argv: list[str], *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout="", stderr="")


def test_batch_preflight_skips_github_auth_for_local_exercise_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "default_profile": "local_exercise",
                "profiles": {
                    "local_exercise": {
                        "run_common": {
                            "push": False,
                            "pr": False,
                        }
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    called: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        called.append(argv)
        return _completed(argv)

    monkeypatch.setattr(batch_preflight, "_run", fake_run)
    monkeypatch.setattr(batch_preflight, "_git_branch", lambda _: "dev")
    monkeypatch.setattr(batch_preflight, "_git_head", lambda _: "abc123")
    monkeypatch.setattr(batch_preflight, "_gitlab_registry_probe", lambda: None)

    result = batch_preflight.run_batch_preflight(
        repo_root=tmp_path,
        batch_dir=tmp_path / "batch",
        batch_config={
            "defaults": {
                "require_clean_git": False,
                "require_local_green": False,
                "require_ci_green_for_base": False,
                "run_settings_path": str(settings_path),
                "run_settings_profile": "local_exercise",
            }
        },
        worker_roster=[],
        exec_backend="host",
    )

    assert result["blockers"] == []
    assert ["gh", "auth", "status"] not in called
    assert "skipped" in (tmp_path / "batch" / "preflight" / "gh_auth.log").read_text(
        encoding="utf-8"
    )


def test_batch_preflight_keeps_github_auth_for_default_remote_handoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    called: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        called.append(argv)
        if argv == ["gh", "auth", "status"]:
            return _completed(argv, returncode=1)
        return _completed(argv)

    monkeypatch.setattr(batch_preflight, "_run", fake_run)
    monkeypatch.setattr(batch_preflight, "_git_branch", lambda _: "dev")
    monkeypatch.setattr(batch_preflight, "_git_head", lambda _: "abc123")
    monkeypatch.setattr(batch_preflight, "_gitlab_registry_probe", lambda: None)

    result = batch_preflight.run_batch_preflight(
        repo_root=tmp_path,
        batch_dir=tmp_path / "batch",
        batch_config={
            "defaults": {
                "require_clean_git": False,
                "require_local_green": False,
                "require_ci_green_for_base": False,
            }
        },
        worker_roster=[],
        exec_backend="host",
    )

    assert ["gh", "auth", "status"] in called
    assert [item["blocker_id"] for item in result["blockers"]] == ["batch_control_plane"]
