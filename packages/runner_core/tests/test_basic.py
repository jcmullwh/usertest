from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

import runner_core.target_acquire as target_acquire_mod
from runner_core.pathing import slugify
from runner_core.prompt import TemplateSubstitutionError, build_prompt_from_template
from runner_core.target_acquire import acquire_existing_target, acquire_target


def test_slugify() -> None:
    assert slugify("https://github.com/org/repo.git") == "repo"
    assert slugify(r"I:\code\some_repo") == "some_repo"


def test_build_prompt_from_template_substitutes() -> None:
    template = "Hello ${name}.\nPolicy:\n${policy_json}\n"
    out = build_prompt_from_template(
        template_text=template,
        variables={"name": "World", "policy_json": '{"allow_edits": false}'},
    )
    assert "Hello World." in out
    assert '{"allow_edits": false}' in out


def test_build_prompt_from_template_errors_on_missing_vars() -> None:
    template = "Hello ${name}. Missing: ${nope}\n"
    with pytest.raises(TemplateSubstitutionError):
        build_prompt_from_template(template_text=template, variables={"name": "World"})


def test_acquire_target_relocates_dest_when_inside_source(tmp_path: Path) -> None:
    src = tmp_path / "src_repo"
    src.mkdir()
    (src / "README.md").write_text("# hi\n", encoding="utf-8")

    dest_inside = src / "runs" / "_workspaces" / f"ws_{uuid4().hex}"
    acquired = acquire_target(repo=str(src), dest_dir=dest_inside, ref=None)

    try:
        assert acquired.workspace_dir.is_dir()
        assert not acquired.workspace_dir.resolve().is_relative_to(src.resolve())
    finally:
        if not acquired.workspace_dir.resolve().is_relative_to(src.resolve()):
            shutil.rmtree(acquired.workspace_dir, ignore_errors=True)


def test_acquire_target_copy_ignores_generated_dirs(tmp_path: Path) -> None:
    src = tmp_path / "src_repo"
    src.mkdir()

    (src / "keep.txt").write_text("ok\n", encoding="utf-8")
    (src / ".venv" / "pyvenv.cfg").parent.mkdir(parents=True)
    (src / ".venv" / "pyvenv.cfg").write_text("venv\n", encoding="utf-8")
    (src / "node_modules" / "x" / "y.js").parent.mkdir(parents=True)
    (src / "node_modules" / "x" / "y.js").write_text("x\n", encoding="utf-8")
    (src / "runs" / "_workspaces" / "ws1" / "nested.txt").parent.mkdir(parents=True)
    (src / "runs" / "_workspaces" / "ws1" / "nested.txt").write_text("nope\n", encoding="utf-8")

    # Ensure "runs" is only ignored at repo root (not globally).
    (src / "src" / "runs" / "keep2.txt").parent.mkdir(parents=True)
    (src / "src" / "runs" / "keep2.txt").write_text("ok\n", encoding="utf-8")

    dest = tmp_path / f"dest_{uuid4().hex}"
    acquired = acquire_target(repo=str(src), dest_dir=dest, ref=None)
    try:
        workspace = acquired.workspace_dir
        assert (workspace / "keep.txt").exists()
        assert not (workspace / ".venv").exists()
        assert not (workspace / "node_modules").exists()
        assert not (workspace / "runs").exists()
        assert (workspace / "src" / "runs" / "keep2.txt").exists()
    finally:
        shutil.rmtree(acquired.workspace_dir, ignore_errors=True)


def test_acquire_existing_target_reuses_workspace_without_copying(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(
        ["git", "-C", str(workspace), "init"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.email", "usertest@local"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.name", "usertest"],
        check=True,
        capture_output=True,
        text=True,
    )
    (workspace / "README.md").write_text("# hi\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(workspace), "add", "-A"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-m", "init"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "checkout", "-b", "resume-branch"],
        check=True,
        capture_output=True,
        text=True,
    )

    acquired = acquire_existing_target(
        repo=str(workspace),
        workspace_dir=workspace,
        ref="resume-branch",
    )

    assert acquired.workspace_dir == workspace.resolve()
    assert acquired.mode == "existing"
    assert acquired.ref == "resume-branch"
    assert acquired.commit_sha


@pytest.mark.skipif(os.name != "nt", reason="Windows-only long path handling")
def test_acquire_target_relocates_dest_for_windows_long_paths(tmp_path: Path) -> None:
    src = Path(tempfile.gettempdir()) / f"ut_src_{uuid4().hex}"
    dest_name = f"ws_{uuid4().hex}"
    dest = tmp_path / ("a" * 80) / ("b" * 80) / dest_name

    src.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["git", "-C", str(src), "init"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(src), "config", "user.email", "usertest@local"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(src), "config", "user.name", "usertest"],
            check=True,
            capture_output=True,
            text=True,
        )

        tmp_root = Path(tempfile.gettempdir())
        base_len = len(str(tmp_root / "usertest_workspaces" / dest_name)) + 1
        long_dir_len = max(1, 248 - base_len)
        long_dir = "d" * long_dir_len

        tracked = src / long_dir / "x.txt"
        tracked.parent.mkdir(parents=True, exist_ok=True)
        tracked.write_text("x\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(src), "add", "-A"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(src), "commit", "-m", "init"],
            check=True,
            capture_output=True,
            text=True,
        )

        acquired = acquire_target(repo=str(src), dest_dir=dest, ref=None)
        try:
            assert acquired.workspace_dir != dest
            assert "ut" in acquired.workspace_dir.parts
            assert (acquired.workspace_dir / long_dir / "x.txt").exists()
        finally:
            shutil.rmtree(acquired.workspace_dir, ignore_errors=True)
    finally:
        shutil.rmtree(src, ignore_errors=True)


def _init_committed_repo(path: Path, *, branch: str = "main") -> str:
    path.mkdir(parents=True)
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
    (path / "README.md").write_text("checkout recovery\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "README.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "branch", "-M", branch],
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_acquire_target_recovers_enospc_on_distinct_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "source"
    expected_sha = _init_committed_repo(src, branch="recovery-ref")
    preferred = tmp_path / "preferred" / "workspace"
    fallback = Path(tempfile.gettempdir()) / f"ut_enospc_{uuid4().hex}"
    original_clone = target_acquire_mod._git_clone
    original_connectivity = target_acquire_mod._verify_git_workspace_connectivity
    clone_calls: list[tuple[Path, bool]] = []
    connectivity_calls: list[Path] = []

    def controlled_clone(*, repo: str, dest_dir: Path, no_local: bool = False) -> None:
        clone_calls.append((dest_dir, no_local))
        if len(clone_calls) == 1:
            dest_dir.mkdir(parents=True)
            (dest_dir / "partial").write_text("partial\n", encoding="utf-8")
            raise RuntimeError("error: checkout failed: No space left on device")
        original_clone(repo=repo, dest_dir=dest_dir, no_local=no_local)

    def checked_connectivity(*, cwd: Path) -> None:
        connectivity_calls.append(cwd)
        original_connectivity(cwd=cwd)

    monkeypatch.setattr(target_acquire_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(target_acquire_mod, "_workspace_candidates", lambda **_: [fallback])
    # Keep the test independent of the host's drive layout while exercising the real candidate.
    monkeypatch.setattr(
        target_acquire_mod,
        "_windows_volume_identity",
        lambda path: "fallback:" if path == fallback else "preferred:",
    )
    monkeypatch.setattr(target_acquire_mod, "_git_clone", controlled_clone)
    monkeypatch.setattr(
        target_acquire_mod, "_verify_git_workspace_connectivity", checked_connectivity
    )

    try:
        acquired = acquire_target(repo=str(src), dest_dir=preferred, ref="recovery-ref")
        actual_sha = subprocess.run(
            ["git", "-C", str(acquired.workspace_dir), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert acquired.workspace_dir == fallback
        assert acquired.ref == "recovery-ref"
        assert acquired.commit_sha == expected_sha == actual_sha
        assert clone_calls == [(preferred, True), (fallback, True)]
        assert connectivity_calls == [fallback]
        assert not preferred.exists()
        assert (fallback / "README.md").is_file()

        observation = {
            "commit_sha_matches": actual_sha == expected_sha,
            "connectivity_checked": connectivity_calls == [fallback],
            "fallback_volume_differs": True,
            "partial_destination_exists": preferred.exists(),
            "requested_ref_matches": acquired.ref == "recovery-ref",
            "status": "acquired",
        }
        print(json.dumps(observation, sort_keys=True, separators=(",", ":")))
    finally:
        shutil.rmtree(fallback, ignore_errors=True)


def test_acquire_target_does_not_retry_non_enospc_clone_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "source"
    _init_committed_repo(src)
    preferred = tmp_path / "preferred"
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    calls: list[Path] = []

    def failing_clone(*, repo: str, dest_dir: Path, no_local: bool = False) -> None:
        del repo, no_local
        calls.append(dest_dir)
        dest_dir.mkdir(parents=True)
        raise RuntimeError("fatal: Authentication failed")

    monkeypatch.setattr(target_acquire_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(target_acquire_mod, "_git_clone", failing_clone)
    with pytest.raises(RuntimeError, match="Authentication failed"):
        acquire_target(repo=str(src), dest_dir=preferred, ref=None)

    assert calls == [preferred]
    assert not preferred.exists()
    assert src.exists()
    assert unrelated.exists()


@pytest.mark.parametrize(
    "candidate_kind",
    ["same-volume", "unknown", "pre-existing", "unwritable", "inside-source"],
)
def test_acquire_target_rejects_unsafe_enospc_candidates_without_deleting_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_kind: str,
) -> None:
    src = tmp_path / "source"
    _init_committed_repo(src)
    preferred = tmp_path / "preferred"
    candidate = src / "candidate" if candidate_kind == "inside-source" else tmp_path / "candidate"
    if candidate_kind == "pre-existing":
        candidate.mkdir()
        (candidate / "sentinel").write_text("keep\n", encoding="utf-8")
    calls: list[Path] = []

    def failing_clone(*, repo: str, dest_dir: Path, no_local: bool = False) -> None:
        del repo, no_local
        calls.append(dest_dir)
        dest_dir.mkdir(parents=True)
        raise RuntimeError("No SpAcE LeFt On DeViCe")

    def volume_identity(path: Path) -> str:
        if path == preferred:
            return "preferred:"
        if candidate_kind == "unknown":
            return ""
        if candidate_kind == "same-volume":
            return "preferred:"
        return "fallback:"

    monkeypatch.setattr(target_acquire_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(target_acquire_mod, "_git_clone", failing_clone)
    monkeypatch.setattr(target_acquire_mod, "_workspace_candidates", lambda **_: [candidate])
    monkeypatch.setattr(target_acquire_mod, "_windows_volume_identity", volume_identity)
    if candidate_kind == "unwritable":
        monkeypatch.setattr(target_acquire_mod, "_probe_workspace_parent", lambda _: False)

    with pytest.raises(RuntimeError) as exc_info:
        acquire_target(repo=str(src), dest_dir=preferred, ref=None)

    assert "No SpAcE LeFt On DeViCe" in str(exc_info.value)
    assert "fallback context" in str(exc_info.value)
    assert calls == [preferred]
    assert not preferred.exists()
    assert src.exists()
    if candidate_kind == "pre-existing":
        assert (candidate / "sentinel").read_text(encoding="utf-8") == "keep\n"
    else:
        assert not candidate.exists()


def test_acquire_target_enospc_fallback_failure_reports_both_and_cleans_partials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "source"
    _init_committed_repo(src)
    preferred = tmp_path / "preferred"
    fallback = tmp_path / "fallback"
    calls: list[Path] = []

    def failing_clone(*, repo: str, dest_dir: Path, no_local: bool = False) -> None:
        del repo, no_local
        calls.append(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        if len(calls) == 1:
            raise RuntimeError("No space left on device")
        raise RuntimeError("fallback repository write failed")

    monkeypatch.setattr(target_acquire_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(target_acquire_mod, "_git_clone", failing_clone)
    monkeypatch.setattr(target_acquire_mod, "_workspace_candidates", lambda **_: [fallback])
    monkeypatch.setattr(
        target_acquire_mod,
        "_windows_volume_identity",
        lambda path: "fallback:" if path == fallback else "preferred:",
    )

    with pytest.raises(RuntimeError) as exc_info:
        acquire_target(repo=str(src), dest_dir=preferred, ref=None)

    message = str(exc_info.value)
    assert "No space left on device" in message
    assert "fallback repository write failed" in message
    assert str(preferred) in message
    assert str(fallback) in message
    assert calls == [preferred, fallback]
    assert not preferred.exists()
    assert not fallback.exists()
    assert src.exists()


def test_acquire_target_enospc_does_not_delete_candidate_created_during_selection_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "source"
    _init_committed_repo(src)
    preferred = tmp_path / "preferred"
    fallback = tmp_path / "fallback"
    clone_calls: list[Path] = []

    def initial_failure(*, repo: str, dest_dir: Path, no_local: bool = False) -> None:
        del repo, no_local
        clone_calls.append(dest_dir)
        dest_dir.mkdir(parents=True)
        raise RuntimeError("No space left on device")

    def raced_candidate(**_: object) -> Path:
        fallback.mkdir()
        (fallback / "sentinel").write_text("not ours\n", encoding="utf-8")
        return fallback

    monkeypatch.setattr(target_acquire_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(target_acquire_mod, "_git_clone", initial_failure)
    monkeypatch.setattr(
        target_acquire_mod,
        "_select_distinct_windows_workspace_candidate",
        raced_candidate,
    )

    with pytest.raises(RuntimeError) as exc_info:
        acquire_target(repo=str(src), dest_dir=preferred, ref=None)

    assert "could not reserve the fallback destination" in str(exc_info.value)
    assert clone_calls == [preferred]
    assert (fallback / "sentinel").read_text(encoding="utf-8") == "not ours\n"
    assert not preferred.exists()
    assert src.exists()


def test_acquire_target_rejects_preexisting_dangling_destination_symlink(
    tmp_path: Path,
) -> None:
    src = tmp_path / "source"
    _init_committed_repo(src)
    preferred = tmp_path / "dangling-workspace"
    try:
        preferred.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")

    with pytest.raises(FileExistsError):
        acquire_target(repo=str(src), dest_dir=preferred, ref=None)

    assert os.path.lexists(preferred)
    assert preferred.is_symlink()
    assert src.exists()


@pytest.mark.parametrize("validation_stage", ["ref", "connectivity", "commit"])
def test_acquire_target_enospc_fallback_validation_failure_keeps_context_and_cleans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validation_stage: str,
) -> None:
    src = tmp_path / "source"
    _init_committed_repo(src)
    preferred = tmp_path / "preferred"
    fallback = tmp_path / "fallback"
    original_clone = target_acquire_mod._git_clone
    original_run_git = target_acquire_mod._run_git
    clone_count = 0

    def controlled_clone(*, repo: str, dest_dir: Path, no_local: bool = False) -> None:
        nonlocal clone_count
        clone_count += 1
        if clone_count == 1:
            dest_dir.mkdir(parents=True)
            raise RuntimeError("No space left on device")
        original_clone(repo=repo, dest_dir=dest_dir, no_local=no_local)

    def controlled_run_git(args: list[str], *, cwd: Path) -> str:
        if validation_stage == "commit" and cwd == fallback and args == ["rev-parse", "HEAD"]:
            raise RuntimeError("commit validation exploded")
        return original_run_git(args, cwd=cwd)

    monkeypatch.setattr(target_acquire_mod, "_is_windows", lambda: True)
    monkeypatch.setattr(target_acquire_mod, "_git_clone", controlled_clone)
    monkeypatch.setattr(target_acquire_mod, "_workspace_candidates", lambda **_: [fallback])
    monkeypatch.setattr(
        target_acquire_mod,
        "_windows_volume_identity",
        lambda path: "fallback:" if path == fallback else "preferred:",
    )
    if validation_stage == "connectivity":
        monkeypatch.setattr(
            target_acquire_mod,
            "_verify_git_workspace_connectivity",
            lambda **_: (_ for _ in ()).throw(RuntimeError("connectivity validation exploded")),
        )
    if validation_stage == "commit":
        monkeypatch.setattr(target_acquire_mod, "_run_git", controlled_run_git)

    requested_ref = "missing-ref" if validation_stage == "ref" else None
    with pytest.raises(RuntimeError) as exc_info:
        acquire_target(repo=str(src), dest_dir=preferred, ref=requested_ref)

    message = str(exc_info.value)
    assert "No space left on device" in message
    assert str(preferred) in message
    assert str(fallback) in message
    if validation_stage == "ref":
        assert "missing-ref" in message
    else:
        assert f"{validation_stage} validation exploded" in message
    assert clone_count == 2
    assert not preferred.exists()
    assert not fallback.exists()
    assert src.exists()
