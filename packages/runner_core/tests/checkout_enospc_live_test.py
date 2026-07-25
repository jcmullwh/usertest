"""Windows outcome probe for bounded Git-checkout ENOSPC recovery.

Controlled mode injects only the authenticated initial clone failure and is safe for routine
offline verification. Live mode is intentionally operator-gated: it requires an already nearly
full runs volume and never attempts to manufacture that precondition.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _prepend_reviewed_checkout_sources() -> Path:
    """Load runner_core and its monorepo dependencies from this probe's checkout."""

    repo_root = Path(__file__).resolve().parents[3]
    package_names = (
        "runner_core",
        "agent_adapters",
        "normalized_events",
        "reporter",
        "sandbox_runner",
        "run_artifacts",
    )
    source_dirs = [repo_root / "packages" / name / "src" for name in package_names]
    missing = [source_dir for source_dir in source_dirs if not source_dir.is_dir()]
    if missing:
        raise RuntimeError(f"reviewed checkout is missing package sources: {missing}")
    for source_dir in reversed(source_dirs):
        sys.path.insert(0, str(source_dir))
    return repo_root


REVIEWED_REPO_ROOT = _prepend_reviewed_checkout_sources()

from runner_core import RunnerConfig, RunRequest, RunResult, run_once  # noqa: E402
from runner_core import target_acquire as target_acquire_mod  # noqa: E402
from runner_core.target_acquire import remove_acquired_workspace  # noqa: E402

EXPECTED_TARGET_ACQUIRE = (
    REVIEWED_REPO_ROOT / "packages" / "runner_core" / "src" / "runner_core" / "target_acquire.py"
).resolve()
LOADED_TARGET_ACQUIRE = Path(target_acquire_mod.__file__ or "").resolve()
if LOADED_TARGET_ACQUIRE != EXPECTED_TARGET_ACQUIRE:
    raise RuntimeError(
        "checkout probe loaded runner_core from outside the reviewed checkout: "
        f"expected {EXPECTED_TARGET_ACQUIRE}, loaded {LOADED_TARGET_ACQUIRE}"
    )

MIB = 1024 * 1024


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(args: list[str], *, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _setup_target(source: Path, *, live: bool) -> tuple[str, str]:
    source.mkdir(parents=True)
    _git(["init"], cwd=source)
    _git(["config", "user.email", "usertest@local"], cwd=source)
    _git(["config", "user.name", "usertest"], cwd=source)
    _write(source / "README.md", "ENOSPC outcome probe\n")
    if live:
        payload = source / "probe-payload.bin"
        with payload.open("wb") as stream:
            for _ in range(32):
                stream.write(os.urandom(MIB))
    _git(["add", "-A"], cwd=source)
    _git(["commit", "-m", "probe target"], cwd=source)
    _git(["branch", "-M", "probe-ref"], cwd=source)
    return "probe-ref", _git(["rev-parse", "HEAD"], cwd=source)


def _setup_runner_root(root: Path) -> None:
    _write(
        root / "configs" / "catalog.yaml",
        "\n".join(
            [
                "version: 1",
                "personas_dirs:",
                "  - configs/personas",
                "missions_dirs:",
                "  - configs/missions",
                "prompt_templates_dir: configs/prompt_templates",
                "report_schemas_dir: configs/report_schemas",
                "defaults:",
                "  persona_id: p",
                "  mission_id: m",
                "",
            ]
        ),
    )
    _write(
        root / "configs" / "personas" / "p.persona.md",
        "---\nid: p\nname: P\nextends: null\n---\nProbe persona\n",
    )
    _write(
        root / "configs" / "missions" / "m.mission.md",
        "\n".join(
            [
                "---",
                "id: m",
                "name: M",
                "extends: null",
                "execution_mode: single_pass_inline_report",
                "prompt_template: t.prompt.md",
                "report_schema: s.schema.json",
                "---",
                "Probe mission",
                "",
            ]
        ),
    )
    _write(
        root / "configs" / "prompt_templates" / "t.prompt.md",
        "Return the probe report.\n${report_schema_json}\n",
    )
    _write(
        root / "configs" / "report_schemas" / "s.schema.json",
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "string"}},
            }
        )
        + "\n",
    )


def _make_dummy_codex(root: Path, invocation_log: Path) -> str:
    script = root / "dummy_codex.py"
    _write(
        script,
        "\n".join(
            [
                "from __future__ import annotations",
                "import json, os, sys",
                "from pathlib import Path",
                "argv = sys.argv[1:]",
                "if '--version' in argv:",
                "    print('codex-cli 0.0.0-probe')",
                "    raise SystemExit(0)",
                "cd = argv[argv.index('--cd') + 1]",
                "out = argv[argv.index('--output-last-message') + 1]",
                "os.chdir(cd)",
                f"log = Path({str(invocation_log)!r})",
                "with log.open('a', encoding='utf-8') as stream:",
                "    stream.write(json.dumps({'cwd': str(Path.cwd()), 'argv': argv}) + '\\n')",
                "sys.stdin.read()",
                "print(json.dumps({'type': 'thread.started', "
                "                  'thread_id': '019f2cca-9011-7e32-88ae-6c25af578b49'}))",
                "print(json.dumps({'id': 'probe', 'msg': {'type': 'agent_message', "
                "                  'message': 'probe complete'}}))",
                "Path(out).write_text(json.dumps({'ok': 'yes'}) + '\\n', encoding='utf-8')",
                "",
            ]
        ),
    )
    wrapper = root / "dummy_codex.cmd"
    _write(wrapper, f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n')
    return str(wrapper)


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def _assert_run(
    *,
    result: RunResult,
    expected_ref: str,
    expected_sha: str,
    runs_volume: str,
) -> Path:
    exit_code = result.exit_code
    run_dir = result.run_dir
    if exit_code != 0:
        raise RuntimeError(f"runner failed with exit code {exit_code}: {run_dir}")
    if result.report_validation_errors:
        raise RuntimeError(f"runner report validation failed: {result.report_validation_errors}")
    workspace_ref = _load_json(run_dir / "workspace_ref.json")
    target_ref = _load_json(run_dir / "target_ref.json")
    attempts = _load_json(run_dir / "agent_attempts.json")
    workspace = Path(str(workspace_ref["workspace_dir"]))
    if target_acquire_mod._windows_volume_identity(workspace) == runs_volume:
        raise RuntimeError(f"workspace did not move to a different volume: {workspace}")
    if target_ref.get("ref") != expected_ref or target_ref.get("commit_sha") != expected_sha:
        raise RuntimeError("requested ref or commit identity was not preserved")
    attempt_items = attempts.get("attempts")
    if not isinstance(attempt_items, list) or not attempt_items:
        raise RuntimeError("agent_attempts.json did not record an invocation")
    argv = attempt_items[0].get("argv")
    if not isinstance(argv, list) or "--cd" not in argv:
        raise RuntimeError("agent attempt did not record a --cd workspace")
    invoked_workspace = Path(str(argv[argv.index("--cd") + 1])).resolve()
    if invoked_workspace != workspace.resolve():
        raise RuntimeError("agent invocation did not use workspace_ref.json's relocated path")
    return workspace


def _run_probe(*, mode: str, repo: Path, runs_dir: Path) -> dict[str, bool | str]:
    if os.name != "nt":
        raise RuntimeError("checkout ENOSPC outcome probe requires Windows")
    if not repo.is_dir():
        raise RuntimeError(f"--repo is not a directory: {repo}")

    runs_dir.mkdir(parents=True, exist_ok=True)
    runs_volume = target_acquire_mod._windows_volume_identity(runs_dir)
    system_temp = Path(tempfile.gettempdir()).resolve()
    temp_volume = target_acquire_mod._windows_volume_identity(system_temp)
    if not runs_volume or not temp_volume or runs_volume == temp_volume:
        raise RuntimeError(
            "--runs-dir and the operating-system temp directory need distinct volumes"
        )

    if mode == "live":
        expected_runs_volume = target_acquire_mod._windows_volume_identity(Path("I:\\"))
        if runs_volume != expected_runs_volume:
            raise RuntimeError(
                f"live mode requires an I:-backed --runs-dir; observed volume {runs_volume!r}"
            )
        free = shutil.disk_usage(runs_dir).free
        if not 4 * MIB <= free <= 24 * MIB:
            raise RuntimeError(
                "live mode requires the runs volume to already have 4-24 MiB free; "
                f"observed {free} bytes"
            )

    probe_root = Path(tempfile.mkdtemp(prefix="ut_checkout_enospc_probe_", dir=system_temp))
    owned_kept_workspaces: list[Path] = []
    original_temp = tempfile.tempdir
    original_env = {name: os.environ.get(name) for name in ("TEMP", "TMP")}
    original_clone = target_acquire_mod._git_clone
    preferred_destinations: list[Path] = []
    enospc_observations: list[Path] = []
    try:
        temp_workspace_root = probe_root / "temp"
        temp_workspace_root.mkdir()
        for name in ("TEMP", "TMP"):
            os.environ[name] = str(temp_workspace_root)
        tempfile.tempdir = str(temp_workspace_root)

        source = probe_root / "source"
        expected_ref, expected_sha = _setup_target(source, live=mode == "live")
        runner_root = probe_root / "runner_root"
        _setup_runner_root(runner_root)
        invocation_log = probe_root / "invocations.jsonl"
        dummy_binary = _make_dummy_codex(runner_root, invocation_log)

        def observed_clone(*, repo: str, dest_dir: Path, no_local: bool = False) -> None:
            destination_volume = target_acquire_mod._windows_volume_identity(dest_dir)
            if mode == "controlled" and destination_volume == runs_volume:
                preferred_destinations.append(dest_dir)
                dest_dir.mkdir(parents=True)
                _write(dest_dir / "partial", "partial\n")
                enospc_observations.append(dest_dir)
                raise RuntimeError("checkout failed: No space left on device")
            try:
                original_clone(repo=repo, dest_dir=dest_dir, no_local=no_local)
            except RuntimeError as error:
                if (
                    destination_volume == runs_volume
                    and target_acquire_mod._is_windows_checkout_enospc_error(str(error))
                ):
                    preferred_destinations.append(dest_dir)
                    enospc_observations.append(dest_dir)
                raise

        target_acquire_mod._git_clone = observed_clone
        cfg = RunnerConfig(
            repo_root=runner_root,
            runs_dir=runs_dir,
            agents={"codex": {"binary": dummy_binary}},
            policies={"safe": {"codex": {"sandbox": "read-only", "allow_edits": False}}},
        )

        workspaces: list[Path] = []
        for keep, seed in ((False, 7001), (True, 7002)):
            result = run_once(
                cfg,
                RunRequest(
                    repo=str(source),
                    ref=expected_ref,
                    agent="codex",
                    policy="safe",
                    persona_id="p",
                    mission_id="m",
                    seed=seed,
                    keep_workspace=keep,
                    agent_rate_limit_retries=0,
                    agent_followup_attempts=0,
                ),
            )
            workspace = _assert_run(
                result=result,
                expected_ref=expected_ref,
                expected_sha=expected_sha,
                runs_volume=runs_volume,
            )
            workspaces.append(workspace)
            if keep:
                owned_kept_workspaces.append(workspace)

        if len(preferred_destinations) != 2 or len(enospc_observations) != 2:
            raise RuntimeError("both runner acquisitions must observe initial clone ENOSPC")
        if any(os.path.lexists(path) for path in preferred_destinations):
            raise RuntimeError("an initial partial destination remained after recovery")
        cleanup_removed = not workspaces[0].exists()
        keep_preserved = workspaces[1].is_dir()
        if not cleanup_removed or not keep_preserved:
            raise RuntimeError(
                "relocated workspace cleanup/retention behavior did not match requests"
            )
        if not invocation_log.is_file():
            raise RuntimeError("dummy agent was not invoked for both lifecycle branches")
        invocation_entries = [
            json.loads(line) for line in invocation_log.read_text(encoding="utf-8").splitlines()
        ]
        if len(invocation_entries) != 2:
            raise RuntimeError("dummy agent was not invoked for both lifecycle branches")
        actual_cwds = [Path(str(entry["cwd"])).resolve() for entry in invocation_entries]
        if actual_cwds != [workspace.resolve() for workspace in workspaces]:
            raise RuntimeError("dummy agent did not enter both recorded relocated workspaces")
        if _git(["rev-parse", "HEAD"], cwd=workspaces[1]) != expected_sha:
            raise RuntimeError("kept relocated checkout has the wrong commit")

        observation: dict[str, bool | str] = {
            "agent_invoked": True,
            "cleanup_removed_relocated_workspace": cleanup_removed,
            "commit_sha_matches": True,
            "fallback_volume_differs": True,
            "keep_preserved_relocated_workspace": keep_preserved,
            "partial_destination_exists": False,
            "requested_ref_matches": True,
            "status": "mitigated",
            "workspace_ref_matches_fallback": True,
        }
        if mode == "live":
            observation["natural_enospc_observed"] = True
        return observation
    finally:
        target_acquire_mod._git_clone = original_clone
        tempfile.tempdir = original_temp
        for name, value in original_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        for workspace in owned_kept_workspaces:
            if os.path.lexists(workspace):
                remove_acquired_workspace(workspace)
        if os.path.lexists(probe_root):
            remove_acquired_workspace(probe_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("controlled", "live"), required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--runs-dir", type=Path, required=True)
    args = parser.parse_args()
    observation = _run_probe(
        mode=args.mode,
        repo=args.repo.expanduser().resolve(),
        runs_dir=args.runs_dir.expanduser().resolve(),
    )
    print(json.dumps(observation, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
