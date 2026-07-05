from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


def _load_scaffold_module():
    scaffold_path = Path(__file__).resolve().with_name("scaffold.py")
    spec = importlib.util.spec_from_file_location("scaffold_cli_module_install_cache_tests", scaffold_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load scaffold module from {scaffold_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scaffold = _load_scaffold_module()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _cache_state(*, repo_root: Path, project_dir: Path, project_id: str = "demo"):
    return scaffold._build_install_cache_state(
        repo_root=repo_root,
        project_dir=project_dir,
        project_id=project_id,
        install_cmd=["pdm", "install"],
        cache_enabled=True,
    )


def test_install_cache_local_hit_skips_running_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    project_dir = repo_root / "packages" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write(project_dir / "pyproject.toml", "[project]\nname='demo'\nversion='0.1.0'\n")
    _write(project_dir / "pdm.lock", "lock-version = \"4.5.0\"\n")
    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)
    monkeypatch.setenv("USERTEST_MAINT_VENV_CACHE_ENABLED", "1")
    monkeypatch.setenv("USERTEST_MAINT_VENV_CACHE_ROOT", str(tmp_path / "cache"))

    state = _cache_state(repo_root=repo_root, project_dir=project_dir)
    (project_dir / ".venv").mkdir(parents=True, exist_ok=True)
    scaffold._write_local_install_metadata(state=state)

    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, capture
        calls.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaffold, "_run", fake_run)
    cp = scaffold._run_manifest_task(
        cmd=["pdm", "install"],
        cwd=project_dir,
        task_name="install",
        project_id="demo",
    )
    assert cp.returncode == 0
    assert calls == []


def test_install_cache_force_install_bypasses_local_hit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    project_dir = repo_root / "packages" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write(project_dir / "pyproject.toml", "[project]\nname='demo'\nversion='0.1.0'\n")
    _write(project_dir / "pdm.lock", "lock-version = \"4.5.0\"\n")
    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)
    monkeypatch.setenv("USERTEST_MAINT_VENV_CACHE_ENABLED", "1")
    monkeypatch.setenv("USERTEST_MAINT_VENV_CACHE_ROOT", str(tmp_path / "cache"))

    state = _cache_state(repo_root=repo_root, project_dir=project_dir)
    (project_dir / ".venv").mkdir(parents=True, exist_ok=True)
    scaffold._write_local_install_metadata(state=state)

    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del env, capture
        calls.append(argv)
        (cwd / ".venv").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaffold, "_run", fake_run)
    cp = scaffold._run_manifest_task(
        cmd=["pdm", "install"],
        cwd=project_dir,
        task_name="install",
        project_id="demo",
        force_install=True,
    )
    assert cp.returncode == 0
    assert calls == [["pdm", "install"]]


def test_install_cache_disabled_runs_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    project_dir = repo_root / "packages" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write(project_dir / "pyproject.toml", "[project]\nname='demo'\nversion='0.1.0'\n")
    _write(project_dir / "pdm.lock", "lock-version = \"4.5.0\"\n")
    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)
    monkeypatch.setenv("USERTEST_MAINT_VENV_CACHE_ENABLED", "0")
    monkeypatch.delenv("USERTEST_MAINT_VENV_CACHE_ROOT", raising=False)

    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del env, capture
        calls.append(argv)
        (cwd / ".venv").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaffold, "_run", fake_run)
    cp = scaffold._run_manifest_task(
        cmd=["pdm", "install"],
        cwd=project_dir,
        task_name="install",
        project_id="demo",
    )
    assert cp.returncode == 0
    assert calls == [["pdm", "install"]]


def test_install_cache_local_metadata_mismatch_runs_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    project_dir = repo_root / "packages" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write(project_dir / "pyproject.toml", "[project]\nname='demo'\nversion='0.1.0'\n")
    _write(project_dir / "pdm.lock", "lock-version = \"4.5.0\"\n")
    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)
    monkeypatch.setenv("USERTEST_MAINT_VENV_CACHE_ENABLED", "1")
    monkeypatch.setenv("USERTEST_MAINT_VENV_CACHE_ROOT", str(tmp_path / "cache"))

    state = _cache_state(repo_root=repo_root, project_dir=project_dir)
    (project_dir / ".venv").mkdir(parents=True, exist_ok=True)
    scaffold._write_json_file(
        state.local_meta_path,
        {
            "schema_version": 1,
            "fingerprint": "not-the-current-fingerprint",
            "payload": {},
        },
    )

    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del env, capture
        calls.append(argv)
        (cwd / ".venv").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaffold, "_run", fake_run)
    cp = scaffold._run_manifest_task(
        cmd=["pdm", "install"],
        cwd=project_dir,
        task_name="install",
        project_id="demo",
    )
    assert cp.returncode == 0
    assert calls == [["pdm", "install"]]


def test_install_cache_restores_cached_venv_and_skips_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    project_dir = repo_root / "packages" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write(project_dir / "pyproject.toml", "[project]\nname='demo'\nversion='0.1.0'\n")
    _write(project_dir / "pdm.lock", "lock-version = \"4.5.0\"\n")
    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)
    monkeypatch.setenv("USERTEST_MAINT_VENV_CACHE_ENABLED", "1")
    monkeypatch.setenv("USERTEST_MAINT_VENV_CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(scaffold, "_validate_project_venv", lambda *, project_dir: True)

    state = _cache_state(repo_root=repo_root, project_dir=project_dir)
    assert state.venv_cache_dir is not None
    (state.venv_cache_dir / "marker.txt").parent.mkdir(parents=True, exist_ok=True)
    (state.venv_cache_dir / "marker.txt").write_text("cached\n", encoding="utf-8")
    if state.entry_meta_path is not None:
        scaffold._write_json_file(
            state.entry_meta_path,
            {
                "schema_version": 1,
                "fingerprint": state.fingerprint,
                "payload": state.fingerprint_payload,
            },
        )

    def fail_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del argv, cwd, env, capture
        raise AssertionError("install should not be executed on cache restore hit")

    monkeypatch.setattr(scaffold, "_run", fail_run)
    cp = scaffold._run_manifest_task(
        cmd=["pdm", "install"],
        cwd=project_dir,
        task_name="install",
        project_id="demo",
    )
    assert cp.returncode == 0
    assert (project_dir / ".venv" / "marker.txt").exists()


def test_install_cache_restore_failure_falls_back_to_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    project_dir = repo_root / "packages" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write(project_dir / "pyproject.toml", "[project]\nname='demo'\nversion='0.1.0'\n")
    _write(project_dir / "pdm.lock", "lock-version = \"4.5.0\"\n")
    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)
    monkeypatch.setenv("USERTEST_MAINT_VENV_CACHE_ENABLED", "1")
    monkeypatch.setenv("USERTEST_MAINT_VENV_CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(scaffold, "_validate_project_venv", lambda *, project_dir: False)

    state = _cache_state(repo_root=repo_root, project_dir=project_dir)
    assert state.venv_cache_dir is not None
    (state.venv_cache_dir / "marker.txt").parent.mkdir(parents=True, exist_ok=True)
    (state.venv_cache_dir / "marker.txt").write_text("cached\n", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del env, capture
        calls.append(argv)
        (cwd / ".venv").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaffold, "_run", fake_run)
    cp = scaffold._run_manifest_task(
        cmd=["pdm", "install"],
        cwd=project_dir,
        task_name="install",
        project_id="demo",
    )
    assert cp.returncode == 0
    assert calls == [["pdm", "install"]]


def test_install_cache_seed_restore_skips_running_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    project_dir = repo_root / "packages" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write(project_dir / "pyproject.toml", "[project]\nname='demo'\nversion='0.1.0'\n")
    _write(project_dir / "pdm.lock", 'lock-version = "4.5.0"\n')
    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)
    monkeypatch.setenv("USERTEST_MAINT_VENV_CACHE_ENABLED", "0")
    monkeypatch.delenv("USERTEST_MAINT_VENV_CACHE_ROOT", raising=False)
    monkeypatch.setenv("USERTEST_MAINT_VENV_SEED_ROOT", str(tmp_path / "seed"))
    monkeypatch.setattr(scaffold, "_validate_project_venv", lambda *, project_dir: True)

    state = scaffold._build_install_cache_state(
        repo_root=repo_root,
        project_dir=project_dir,
        project_id="demo",
        install_cmd=["pdm", "install"],
        cache_enabled=False,
    )
    assert state.seed_venv_dir is not None
    (state.seed_venv_dir / "marker.txt").parent.mkdir(parents=True, exist_ok=True)
    (state.seed_venv_dir / "marker.txt").write_text("seeded\n", encoding="utf-8")

    def fail_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del argv, cwd, env, capture
        raise AssertionError("install should not run when a seed venv is available")

    monkeypatch.setattr(scaffold, "_run", fail_run)
    cp = scaffold._run_manifest_task(
        cmd=["pdm", "install"],
        cwd=project_dir,
        task_name="install",
        project_id="demo",
    )
    assert cp.returncode == 0
    assert (project_dir / ".venv" / "marker.txt").read_text(encoding="utf-8") == "seeded\n"


def test_install_cache_seed_restore_populates_host_cache_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    project_dir = repo_root / "packages" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write(project_dir / "pyproject.toml", "[project]\nname='demo'\nversion='0.1.0'\n")
    _write(project_dir / "pdm.lock", 'lock-version = "4.5.0"\n')
    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)
    monkeypatch.setenv("USERTEST_MAINT_VENV_CACHE_ENABLED", "1")
    monkeypatch.setenv("USERTEST_MAINT_VENV_CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setenv("USERTEST_MAINT_VENV_SEED_ROOT", str(tmp_path / "seed"))
    monkeypatch.setattr(scaffold, "_validate_project_venv", lambda *, project_dir: True)

    state = _cache_state(repo_root=repo_root, project_dir=project_dir)
    assert state.seed_venv_dir is not None
    assert state.venv_cache_dir is not None
    (state.seed_venv_dir / "marker.txt").parent.mkdir(parents=True, exist_ok=True)
    (state.seed_venv_dir / "marker.txt").write_text("seeded\n", encoding="utf-8")

    def fail_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del argv, cwd, env, capture
        raise AssertionError("install should not run when a seed venv is available")

    monkeypatch.setattr(scaffold, "_run", fail_run)
    cp = scaffold._run_manifest_task(
        cmd=["pdm", "install"],
        cwd=project_dir,
        task_name="install",
        project_id="demo",
    )
    assert cp.returncode == 0
    assert (state.venv_cache_dir / "marker.txt").read_text(encoding="utf-8") == "seeded\n"
    assert state.entry_meta_path is not None and state.entry_meta_path.exists()


def test_install_cache_save_populates_cache_entry_after_successful_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    project_dir = repo_root / "packages" / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)
    _write(project_dir / "pyproject.toml", "[project]\nname='demo'\nversion='0.1.0'\n")
    _write(project_dir / "pdm.lock", "lock-version = \"4.5.0\"\n")
    monkeypatch.setattr(scaffold, "_repo_root", lambda: repo_root)
    monkeypatch.setenv("USERTEST_MAINT_VENV_CACHE_ENABLED", "1")
    monkeypatch.setenv("USERTEST_MAINT_VENV_CACHE_ROOT", str(tmp_path / "cache"))

    calls: list[list[str]] = []

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del env, capture
        calls.append(argv)
        (cwd / ".venv").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scaffold, "_run", fake_run)
    cp = scaffold._run_manifest_task(
        cmd=["pdm", "install"],
        cwd=project_dir,
        task_name="install",
        project_id="demo",
    )
    assert cp.returncode == 0
    assert calls == [["pdm", "install"]]

    state = _cache_state(repo_root=repo_root, project_dir=project_dir)
    assert state.venv_cache_dir is not None
    assert state.venv_cache_dir.exists()
    assert state.local_meta_path.exists()
