from __future__ import annotations

from pathlib import Path

import pytest
from runner_core import find_repo_root

from usertest.cli import main


def test_init_usertest_writes_scaffold_and_is_non_destructive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())

    with pytest.raises(SystemExit) as exc:
        main(["init-usertest", "--repo-root", str(repo_root), "--repo", str(tmp_path)])
    assert exc.value.code == 0

    usertest_dir = tmp_path / ".usertest"
    catalog = usertest_dir / "catalog.yaml"
    manifest = usertest_dir / "sandbox_cli_install.yaml"
    personas_dir = usertest_dir / "personas"
    missions_dir = usertest_dir / "missions"
    assert usertest_dir.exists()
    assert catalog.exists()
    assert manifest.exists()
    assert personas_dir.exists()
    assert missions_dir.exists()
    assert (personas_dir / ".gitkeep").exists()
    assert (missions_dir / ".gitkeep").exists()
    catalog_text = catalog.read_text(encoding="utf-8")
    assert "version: 1" in catalog_text
    assert "defaults:" in catalog_text
    assert "personas_dirs:" in catalog_text
    assert "missions_dirs:" in catalog_text
    assert "Path semantics (important):" in catalog_text
    assert "target repo root" in catalog_text
    assert "sandbox_cli_install:" in manifest.read_text(encoding="utf-8")

    # Ensure a local persona/mission dropped into the scaffold becomes discoverable without
    # additional catalog edits.
    (personas_dir / "local_test.persona.md").write_text(
        "\n".join(
            [
                "---",
                "id: local_test_persona_init_usertest",
                "name: Local Test Persona (init-usertest)",
                "---",
                "",
                "Local persona body.",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    (missions_dir / "local_test.mission.md").write_text(
        "\n".join(
            [
                "---",
                "id: local_test_mission_init_usertest",
                "name: Local Test Mission (init-usertest)",
                "extends: first_output_smoke",
                "---",
                "",
                "Local mission body.",
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(SystemExit) as exc_list_personas:
        main(["personas", "list", "--repo-root", str(repo_root), "--repo", str(tmp_path)])
    assert exc_list_personas.value.code == 0
    out_personas = capsys.readouterr().out
    assert "local_test_persona_init_usertest" in out_personas

    with pytest.raises(SystemExit) as exc_list_missions:
        main(["missions", "list", "--repo-root", str(repo_root), "--repo", str(tmp_path)])
    assert exc_list_missions.value.code == 0
    out_missions = capsys.readouterr().out
    assert "local_test_mission_init_usertest" in out_missions

    with pytest.raises(SystemExit) as exc2:
        main(["init-usertest", "--repo-root", str(repo_root), "--repo", str(tmp_path)])
    assert exc2.value.code == 2

    with pytest.raises(SystemExit) as exc3:
        main(
            [
                "init-usertest",
                "--repo-root",
                str(repo_root),
                "--repo",
                str(tmp_path),
                "--force",
            ]
        )
    assert exc3.value.code == 0
