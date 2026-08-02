from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from runner_core import target_acquire


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "usertest@local"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "usertest"],
        check=True,
        capture_output=True,
        text=True,
    )
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "-A"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        check=True,
        capture_output=True,
        text=True,
    )


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_with_longpaths(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.longpaths=true", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_git_clone_supports_no_local_flag(monkeypatch) -> None:
    recorded: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(argv: list[str], **_kwargs: object) -> _Proc:
        recorded.append(list(argv))
        return _Proc()

    monkeypatch.setattr(target_acquire.subprocess, "run", _fake_run)

    target_acquire._git_clone(repo="C:/src/repo", dest_dir=Path("C:/tmp/dest"), no_local=True)

    assert recorded == [["git", "clone", "--no-local", "C:/src/repo", str(Path("C:/tmp/dest"))]]


def test_git_clone_scopes_core_longpaths_before_clone(monkeypatch) -> None:
    recorded: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(argv: list[str], **_kwargs: object) -> _Proc:
        recorded.append(list(argv))
        return _Proc()

    monkeypatch.setattr(target_acquire.subprocess, "run", _fake_run)

    target_acquire._git_clone(
        repo="C:/src/repo",
        dest_dir=Path("C:/tmp/dest"),
        no_local=True,
        core_longpaths=True,
    )

    assert recorded == [
        [
            "git",
            "-c",
            "core.longpaths=true",
            "clone",
            "--no-local",
            "C:/src/repo",
            str(Path("C:/tmp/dest")),
        ]
    ]


def test_run_git_scopes_core_longpaths_after_safe_directory(monkeypatch, tmp_path: Path) -> None:
    recorded: list[list[str]] = []

    class _Proc:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def _fake_run(argv: list[str], **_kwargs: object) -> _Proc:
        recorded.append(list(argv))
        return _Proc()

    monkeypatch.setattr(target_acquire.subprocess, "run", _fake_run)

    assert target_acquire._run_git(
        ["rev-parse", "HEAD"], cwd=tmp_path, core_longpaths=True
    ) == "ok"
    safe_dir = str(tmp_path.resolve()).replace("\\", "/")
    assert recorded == [
        [
            "git",
            "-c",
            f"safe.directory={safe_dir}",
            "-c",
            "core.longpaths=true",
            "rev-parse",
            "HEAD",
        ]
    ]


def test_windows_long_path_decision_is_disabled_off_windows(monkeypatch) -> None:
    destination = Path("requested/workspace")
    monkeypatch.setattr(target_acquire, "_is_windows", lambda: False)

    decision = target_acquire._relocate_dest_for_windows_longpaths(
        dest_dir=destination,
        max_file_rel=400,
        max_dir_rel=390,
    )

    assert decision == target_acquire._WindowsLongPathDecision(
        destination=destination,
        requires_core_longpaths=False,
    )


def test_windows_long_path_decision_distinguishes_safe_and_exhausted_candidates(
    monkeypatch,
) -> None:
    first = Path("C:/deep/first")
    second = Path("C:/short/second")
    monkeypatch.setattr(target_acquire, "_is_windows", lambda: True)
    monkeypatch.setattr(
        target_acquire,
        "_workspace_candidates",
        lambda **_: [first, second],
    )
    monkeypatch.setattr(
        target_acquire,
        "_windows_path_lengths_ok",
        lambda *, dest_dir, **_: dest_dir == second,
    )

    safe = target_acquire._relocate_dest_for_windows_longpaths(
        dest_dir=Path("C:/requested"),
        max_file_rel=100,
        max_dir_rel=90,
    )
    assert safe == target_acquire._WindowsLongPathDecision(second, False)

    monkeypatch.setattr(target_acquire, "_windows_path_lengths_ok", lambda **_: False)
    exhausted = target_acquire._relocate_dest_for_windows_longpaths(
        dest_dir=Path("C:/requested"),
        max_file_rel=400,
        max_dir_rel=390,
    )
    assert exhausted == target_acquire._WindowsLongPathDecision(second, True)


def test_reactive_long_path_recovery_enables_scoped_configuration(
    tmp_path: Path, monkeypatch
) -> None:
    requested = tmp_path / "requested" / "workspace"
    fallback = tmp_path / "short" / "workspace"
    calls: list[tuple[Path, bool]] = []

    def controlled_clone(
        *,
        repo: str,
        dest_dir: Path,
        no_local: bool = False,
        core_longpaths: bool = False,
    ) -> None:
        del repo, no_local
        calls.append((dest_dir, core_longpaths))
        if len(calls) == 1:
            raise RuntimeError("Filename too long")

    monkeypatch.setattr(target_acquire, "_is_windows", lambda: True)
    monkeypatch.setattr(target_acquire, "_git_clone", controlled_clone)
    monkeypatch.setattr(target_acquire, "_workspace_candidates", lambda **_: [fallback])

    outcome = target_acquire._git_clone_with_windows_recovery(
        repo="C:/source",
        dest_dir=requested,
        no_local=True,
        requires_core_longpaths=False,
        protected_source=tmp_path / "source",
        enospc_owned_destinations=[],
    )

    assert calls == [(requested, False), (fallback, True)]
    assert outcome == target_acquire._GitCloneOutcome(
        destination=fallback,
        requires_core_longpaths=True,
    )


def test_acquire_target_exhausted_windows_candidates_scopes_all_workspace_git(
    tmp_path: Path, monkeypatch
) -> None:
    src = tmp_path / "src_repo"
    _init_git_repo(src)
    long_directory = "d" * 80
    long_file = "f" * 80 + ".txt"
    tracked = src / long_directory / long_file
    tracked.parent.mkdir()
    tracked.write_text("long path\n", encoding="utf-8")
    _git_with_longpaths(src, "add", "-A")
    _git_with_longpaths(src, "commit", "-m", "long path")
    expected_sha = _git_with_longpaths(src, "rev-parse", "HEAD")

    selected = tmp_path / "selected" / "workspace"
    requested = tmp_path / "requested" / "workspace"
    clone_calls: list[tuple[bool, bool, Path]] = []
    workspace_git_calls: list[tuple[list[str], bool]] = []
    original_run_git = target_acquire._run_git

    def controlled_clone(
        *,
        repo: str,
        dest_dir: Path,
        no_local: bool = False,
        core_longpaths: bool = False,
    ) -> None:
        clone_calls.append((no_local, core_longpaths, dest_dir))
        shutil.copytree(Path(repo), dest_dir)

    def observed_run_git(args: list[str], *, cwd: Path, core_longpaths: bool = False) -> str:
        if cwd == selected:
            workspace_git_calls.append((list(args), core_longpaths))
        return original_run_git(args, cwd=cwd, core_longpaths=core_longpaths)

    monkeypatch.setattr(target_acquire, "_is_windows", lambda: True)
    monkeypatch.setattr(target_acquire, "_windows_path_lengths_ok", lambda **_: False)
    monkeypatch.setattr(target_acquire, "_workspace_candidates", lambda **_: [selected])
    monkeypatch.setattr(target_acquire, "_git_clone", controlled_clone)
    monkeypatch.setattr(target_acquire, "_run_git", observed_run_git)

    acquired = target_acquire.acquire_target(
        repo=str(src),
        dest_dir=requested,
        ref=expected_sha,
    )

    assert acquired.mode == "git"
    assert acquired.workspace_dir == selected
    assert acquired.commit_sha == expected_sha
    assert (selected / long_directory / long_file).read_text(encoding="utf-8") == "long path\n"
    assert clone_calls == [(True, True, selected)]
    assert [args[0] for args, _ in workspace_git_calls] == [
        "fetch",
        "checkout",
        "fsck",
        "rev-parse",
    ]
    assert all(core_longpaths for _, core_longpaths in workspace_git_calls)
    config = subprocess.run(
        ["git", "-C", str(selected), "config", "--local", "--get", "core.longpaths"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert config.returncode == 1


def test_acquire_target_plain_directory_with_isolated_home(
    tmp_path: Path, monkeypatch
) -> None:
    src = tmp_path / "target_repo"
    src.mkdir(parents=True)
    (src / "README.md").write_text("# repo\n", encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    acquired = target_acquire.acquire_target(
        repo=str(src),
        dest_dir=tmp_path / "workspace",
        ref=None,
    )

    assert acquired.mode == "copy"
    safe_dir = str(acquired.workspace_dir.resolve()).replace("\\", "/")
    proc = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={safe_dir}",
            "-C",
            str(acquired.workspace_dir),
            "status",
            "--porcelain",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_acquire_target_local_git_repo_uses_safe_clone_and_connectivity_check(
    tmp_path: Path, monkeypatch
) -> None:
    src = tmp_path / "src_repo"
    _init_git_repo(src)

    clone_calls: list[tuple[bool, bool]] = []
    connectivity_checks: list[tuple[Path, bool]] = []

    original_git_clone = target_acquire._git_clone
    original_verify = target_acquire._verify_git_workspace_connectivity

    def _wrapped_git_clone(
        *, repo: str, dest_dir: Path, no_local: bool = False, core_longpaths: bool = False
    ) -> None:
        clone_calls.append((no_local, core_longpaths))
        original_git_clone(
            repo=repo,
            dest_dir=dest_dir,
            no_local=no_local,
            core_longpaths=core_longpaths,
        )

    def _wrapped_verify(*, cwd: Path, core_longpaths: bool = False) -> None:
        connectivity_checks.append((cwd, core_longpaths))
        original_verify(cwd=cwd, core_longpaths=core_longpaths)

    monkeypatch.setattr(target_acquire, "_git_clone", _wrapped_git_clone)
    monkeypatch.setattr(target_acquire, "_verify_git_workspace_connectivity", _wrapped_verify)

    dest = tmp_path / "workspace"
    acquired = target_acquire.acquire_target(repo=str(src), dest_dir=dest, ref=None)
    try:
        assert acquired.mode == "git"
        assert clone_calls == [(True, False)]
        assert connectivity_checks == [(acquired.workspace_dir, False)]

        proc = subprocess.run(
            [
                "git",
                "-C",
                str(acquired.workspace_dir),
                "fsck",
                "--connectivity-only",
                "--no-dangling",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout
    finally:
        shutil.rmtree(acquired.workspace_dir, ignore_errors=True)


def test_acquire_target_fetches_ref_reachable_only_from_source_remote_tracking_ref(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src_repo"
    _init_git_repo(src)
    primary_branch = _git(src, "branch", "--show-current")

    _git(src, "checkout", "-b", "temporary-frontier")
    (src / "frontier.txt").write_text("remote-tracking frontier\n", encoding="utf-8")
    _git(src, "add", "frontier.txt")
    _git(src, "commit", "-m", "frontier")
    frontier = _git(src, "rev-parse", "HEAD")
    _git(src, "update-ref", "refs/remotes/origin/dev", frontier)
    _git(src, "checkout", primary_branch)
    _git(src, "update-ref", "-d", "refs/heads/temporary-frontier")

    assert _git(src, "rev-parse", "refs/remotes/origin/dev") == frontier
    assert _git(src, "branch", "--contains", frontier) == ""

    dest = tmp_path / "workspace"
    acquired = target_acquire.acquire_target(repo=str(src), dest_dir=dest, ref=frontier)
    try:
        assert acquired.mode == "git"
        assert acquired.commit_sha == frontier
        assert (acquired.workspace_dir / "frontier.txt").read_text(encoding="utf-8") == (
            "remote-tracking frontier\n"
        )
        assert _git(acquired.workspace_dir, "branch", "--show-current") == ""
    finally:
        shutil.rmtree(acquired.workspace_dir, ignore_errors=True)
