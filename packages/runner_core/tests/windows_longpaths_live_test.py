from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER_CORE_SRC = REPO_ROOT / "packages" / "runner_core" / "src"
if str(RUNNER_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(RUNNER_CORE_SRC))

from runner_core import target_acquire  # noqa: E402


def _isolated_git_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    for name in (
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_WORK_TREE",
    ):
        env.pop(name, None)
    for name in list(env):
        if name == "GIT_CONFIG_COUNT" or name.startswith("GIT_CONFIG_KEY_"):
            env.pop(name, None)
        elif name.startswith("GIT_CONFIG_VALUE_"):
            env.pop(name, None)
    return env


def _git(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    safe_dir = str(cwd.resolve()).replace("\\", "/")
    proc = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={safe_dir}",
            "-c",
            "core.longpaths=true",
            *args,
        ],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"probe git {' '.join(args)} failed: {message}")
    return proc


def _create_source(source: Path, *, env: dict[str, str]) -> tuple[str, str, str]:
    source.mkdir(parents=True)
    _git(["init"], cwd=source, env=env)
    _git(["config", "user.email", "usertest@local"], cwd=source, env=env)
    _git(["config", "user.name", "usertest"], cwd=source, env=env)

    long_directory = "cookiecutter_" + "d" * 104
    long_filename = "generated_" + "f" * 112 + ".txt"
    tracked = source / long_directory / long_filename
    tracked.parent.mkdir()
    tracked.write_text("long-path acquisition probe\n", encoding="utf-8")
    (source / "README.md").write_text("# long-path probe\n", encoding="utf-8")
    _git(["add", "-A"], cwd=source, env=env)
    _git(["commit", "-m", "long path probe"], cwd=source, env=env)
    sha = _git(["rev-parse", "HEAD"], cwd=source, env=env).stdout.strip()
    return sha, long_directory, long_filename


def _deep_temp_root(
    base: Path,
    *,
    destination_name: str,
    max_file_rel: int,
    max_dir_rel: int,
) -> Path:
    deep = base / "deep_temp"
    for index in range(12):
        candidates = [
            deep / "usertest_workspaces" / destination_name,
            deep / "ut" / destination_name,
            deep / "ut" / "ws_000000000000",
        ]
        if all(
            not target_acquire._windows_path_lengths_ok(
                dest_dir=candidate,
                max_file_rel=max_file_rel,
                max_dir_rel=max_dir_rel,
            )
            for candidate in candidates
        ):
            deep.mkdir(parents=True, exist_ok=True)
            return deep
        deep = deep / f"nested_{index:02d}_" / ("x" * 18)
    raise RuntimeError("could not construct an all-candidates-over-limit TEMP root")


def _persistent_core_longpaths_absent(workspace: Path, *, env: dict[str, str]) -> bool:
    safe_dir = str(workspace.resolve()).replace("\\", "/")
    proc = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={safe_dir}",
            "-C",
            str(workspace),
            "config",
            "--local",
            "--get",
            "core.longpaths",
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 1 and not proc.stdout.strip()


def _run_probe(mode: str) -> dict[str, bool | int | str]:
    if os.name != "nt":
        raise RuntimeError("windows_longpaths_live_test.py requires Windows")

    output_root = REPO_ROOT / ".usertest-runtime" / "windows-longpaths-live"
    output_root.mkdir(parents=True, exist_ok=True)
    result_path = output_root / f"{mode}-result.json"
    result_path.unlink(missing_ok=True)
    owned_root = output_root / f"_{mode}_probe"
    if os.path.lexists(owned_root):
        target_acquire.remove_acquired_workspace(owned_root)
    owned_root.mkdir()

    system_temp = Path(tempfile.gettempdir())
    source = system_temp / f"ut_longpaths_source_{uuid4().hex}"
    home = owned_root / "isolated_home"
    home.mkdir()
    transport_bundle = owned_root / "source.bundle" if mode == "original" else None
    env = _isolated_git_env(home)
    original_env = os.environ.copy()
    original_tempdir = tempfile.tempdir
    original_clone = target_acquire._git_clone
    original_run_git = target_acquire._run_git
    clone_calls: list[tuple[str, Path, bool, bool]] = []
    git_calls: list[tuple[Path, list[str], bool]] = []
    acquired_workspace: Path | None = None

    try:
        expected_sha, long_directory, long_filename = _create_source(source, env=env)
        # Git for Windows launches a shell for a --no-local directory transport. The
        # review sandbox denies that shell's signal-pipe creation before checkout, so
        # original mode carries the exact source objects in a local bundle instead.
        # acquire_target still detects, measures, and resolves the real source repository,
        # while the real clone/fetch/checkout path consumes the same commit without a
        # shell helper. Live mode intentionally retains the unrestricted directory
        # transport required by its separate verification role.
        if transport_bundle is not None:
            _git(["bundle", "create", str(transport_bundle), "--all"], cwd=source, env=env)
            _git(["bundle", "verify", str(transport_bundle)], cwd=source, env=env)
        transport_repo = str(transport_bundle or source)
        max_file_rel, max_dir_rel = target_acquire._max_tracked_relpath_lengths(src=source)
        destination_name = "workspace"
        deep_temp = _deep_temp_root(
            owned_root,
            destination_name=destination_name,
            max_file_rel=max_file_rel,
            max_dir_rel=max_dir_rel,
        )
        os.environ.clear()
        os.environ.update(env)
        os.environ["TEMP"] = str(deep_temp)
        os.environ["TMP"] = str(deep_temp)
        tempfile.tempdir = str(deep_temp)

        def observed_clone(
            *,
            repo: str,
            dest_dir: Path,
            no_local: bool = False,
            core_longpaths: bool = False,
        ) -> None:
            if Path(repo).resolve() != source.resolve():
                raise RuntimeError(f"unexpected clone source: {repo}")
            clone_calls.append((transport_repo, dest_dir, no_local, core_longpaths))
            original_clone(
                repo=transport_repo,
                dest_dir=dest_dir,
                no_local=no_local,
                core_longpaths=core_longpaths,
            )

        def observed_run_git(args: list[str], *, cwd: Path, core_longpaths: bool = False) -> str:
            transport_args = list(args)
            if (
                transport_args[:2] == ["fetch", "--no-tags"]
                and len(transport_args) >= 3
                and Path(transport_args[2]).resolve() == source.resolve()
                and transport_bundle is not None
            ):
                transport_args[2] = str(transport_bundle)
            git_calls.append((cwd, transport_args, core_longpaths))
            return original_run_git(
                transport_args,
                cwd=cwd,
                core_longpaths=core_longpaths,
            )

        target_acquire._git_clone = observed_clone
        target_acquire._run_git = observed_run_git

        requested = owned_root / "requested" / destination_name
        acquired = target_acquire.acquire_target(
            repo=str(source),
            dest_dir=requested,
            ref=expected_sha,
        )
        acquired_workspace = acquired.workspace_dir
        selected_candidates = target_acquire._workspace_candidates(dest_dir=requested)
        all_candidates_over_limit = all(
            not target_acquire._windows_path_lengths_ok(
                dest_dir=candidate,
                max_file_rel=max_file_rel,
                max_dir_rel=max_dir_rel,
            )
            for candidate in selected_candidates
        )
        tracked = acquired.workspace_dir / long_directory / long_filename
        post_clone_calls = [
            (args, enabled)
            for cwd, args, enabled in git_calls
            if cwd == acquired.workspace_dir
            and args
            and args[0] in {"fetch", "checkout", "fsck", "rev-parse"}
        ]
        connectivity_succeeded = any(
            args[:2] == ["fsck", "--connectivity-only"] and enabled
            for args, enabled in post_clone_calls
        )

        failed_requested = owned_root / "failed" / destination_name
        unrelated_failure_surfaced = False
        try:
            target_acquire.acquire_target(
                repo=str(source),
                dest_dir=failed_requested,
                ref="refs/heads/definitely-missing-ref",
            )
        except RuntimeError as error:
            unrelated_failure_surfaced = "definitely-missing-ref" in str(error)
        failed_decision = target_acquire._relocate_dest_for_windows_longpaths(
            dest_dir=failed_requested,
            max_file_rel=max_file_rel,
            max_dir_rel=max_dir_rel,
        )

        observation: dict[str, bool | int | str] = {
            "acquired_mode": acquired.mode,
            "all_candidates_over_limit": all_candidates_over_limit,
            "clone_core_longpaths": bool(clone_calls)
            and all(no_local and enabled for _, _, no_local, enabled in clone_calls),
            "clone_transport": "bundle" if transport_bundle is not None else "directory",
            "clone_transport_valid": bool(clone_calls)
            and all(repo == transport_repo for repo, _, _, _ in clone_calls),
            "commit_sha_matches": acquired.commit_sha == expected_sha,
            "connectivity_succeeded": connectivity_succeeded,
            "failed_destination_cleaned": not os.path.lexists(failed_decision.destination),
            "long_tracked_directory_exists": tracked.parent.is_dir(),
            "long_tracked_file_exists": tracked.is_file(),
            "max_dir_rel": max_dir_rel,
            "max_file_rel": max_file_rel,
            "persistent_core_longpaths_absent": _persistent_core_longpaths_absent(
                acquired.workspace_dir,
                env=env,
            )
            and _persistent_core_longpaths_absent(source, env=env)
            and not (home / ".gitconfig").exists(),
            "post_clone_core_longpaths": bool(post_clone_calls)
            and all(enabled for _, enabled in post_clone_calls),
            "status": "acquired",
            "unrelated_failure_surfaced": unrelated_failure_surfaced,
        }
        required_true = [
            "all_candidates_over_limit",
            "clone_core_longpaths",
            "clone_transport_valid",
            "commit_sha_matches",
            "connectivity_succeeded",
            "failed_destination_cleaned",
            "long_tracked_directory_exists",
            "long_tracked_file_exists",
            "persistent_core_longpaths_absent",
            "post_clone_core_longpaths",
            "unrelated_failure_surfaced",
        ]
        failed = [name for name in required_true if observation[name] is not True]
        if acquired.mode != "git":
            failed.append("acquired_mode")
        if failed:
            raise RuntimeError(f"long-path acquisition assertions failed: {', '.join(failed)}")
        result_path.write_text(
            json.dumps(observation, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        return observation
    finally:
        target_acquire._git_clone = original_clone
        target_acquire._run_git = original_run_git
        tempfile.tempdir = original_tempdir
        os.environ.clear()
        os.environ.update(original_env)
        if acquired_workspace is not None and os.path.lexists(acquired_workspace):
            target_acquire.remove_acquired_workspace(acquired_workspace)
        if os.path.lexists(source):
            target_acquire.remove_acquired_workspace(source)
        if os.path.lexists(owned_root):
            target_acquire.remove_acquired_workspace(owned_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("original", "live"), required=True)
    args = parser.parse_args()
    try:
        observation = _run_probe(args.mode)
    except Exception as error:
        print(json.dumps({"error": str(error), "status": "failed"}, separators=(",", ":")))
        return 1
    print(json.dumps(observation, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
