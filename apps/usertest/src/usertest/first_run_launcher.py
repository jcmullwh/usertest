from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

_PIP_FLAGS = ("--disable-pip-version-check", "--retries", "10", "--timeout", "30")
_EDITABLE_INSTALL_TARGETS = (
    "packages/normalized_events",
    "packages/agent_adapters",
    "packages/run_artifacts",
    "packages/reporter",
    "packages/sandbox_runner",
    "packages/runner_core",
    "packages/triage_engine",
    "packages/backlog_core",
    "packages/backlog_miner",
    "packages/backlog_repo",
    "packages/token_monitoring",
    "apps/usertest",
    "apps/usertest_backlog",
    "apps/usertest_implement",
)
_SOURCE_RELATIVE_PATHS = (
    "apps/usertest/src",
    "apps/usertest_backlog/src",
    "apps/usertest_implement/src",
    "packages/runner_core/src",
    "packages/agent_adapters/src",
    "packages/normalized_events/src",
    "packages/reporter/src",
    "packages/sandbox_runner/src",
    "packages/triage_engine/src",
    "packages/backlog_core/src",
    "packages/backlog_miner/src",
    "packages/backlog_repo/src",
    "packages/token_monitoring/src",
    "packages/run_artifacts/src",
)
_SMOKE_IMPORT_MODULES = (
    "usertest",
    "usertest.cli",
    "usertest_backlog",
    "usertest_backlog.cli",
    "usertest_implement",
    "usertest_implement.cli",
    "agent_adapters",
    "backlog_core",
    "backlog_miner",
    "backlog_repo",
    "token_monitoring",
    "normalized_events",
    "reporter",
    "run_artifacts",
    "runner_core",
    "sandbox_runner",
    "triage_engine",
)
_SMOKE_TEST_TARGETS = (
    "apps/usertest/tests/test_smoke.py",
    "apps/usertest/tests/test_golden_fixture.py",
    "apps/usertest_backlog/tests/test_smoke.py",
    "apps/usertest_implement/tests/test_smoke.py",
)


@dataclass(frozen=True)
class PythonSelection:
    command_path: str
    source: str
    executable: str | None = None
    version: str | None = None


@dataclass(frozen=True)
class ShellStyle:
    name: str
    smoke_command: str
    smoke_use_pythonpath_command: str
    smoke_skip_install_command: str
    smoke_require_doctor_command: str
    offline_command: str
    set_pythonpath_script: str
    activate_hint: str
    venv_hint: str


_POSIX_SHELL = ShellStyle(
    name="posix",
    smoke_command="bash ./scripts/smoke.sh",
    smoke_use_pythonpath_command="bash ./scripts/smoke.sh --use-pythonpath",
    smoke_skip_install_command="bash ./scripts/smoke.sh --skip-install --use-pythonpath",
    smoke_require_doctor_command="bash ./scripts/smoke.sh --require-doctor",
    offline_command="bash ./scripts/offline_first_success.sh",
    set_pythonpath_script="scripts/set_pythonpath.sh",
    activate_hint="source .venv/bin/activate  # or: source .venv/Scripts/activate (Git Bash)",
    venv_hint="${python} -m venv .venv && source .venv/bin/activate",
)
_POWERSHELL = ShellStyle(
    name="powershell",
    smoke_command=r"powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1",
    smoke_use_pythonpath_command=(
        r"powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -UsePythonPath"
    ),
    smoke_skip_install_command=(
        "powershell -NoProfile -ExecutionPolicy Bypass -File "
        r".\scripts\smoke.ps1 -SkipInstall -UsePythonPath"
    ),
    smoke_require_doctor_command=(
        r"powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -RequireDoctor"
    ),
    offline_command=(
        r"powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline_first_success.ps1"
    ),
    set_pythonpath_script="scripts/set_pythonpath.ps1",
    activate_hint=r". .\.venv\Scripts\Activate.ps1",
    venv_hint=r"${python} -m venv .venv ; . .\.venv\Scripts\Activate.ps1",
)


class _LauncherFailure(RuntimeError):
    def __init__(self, exit_code: int, message: str = "") -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _shell_style(shell_name: str) -> ShellStyle:
    if shell_name == "posix":
        return _POSIX_SHELL
    if shell_name == "powershell":
        return _POWERSHELL
    raise ValueError(f"Unsupported shell style: {shell_name}")


def _venv_python_path(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _source_pythonpath(repo_root: Path) -> str:
    return os.pathsep.join(str(repo_root / rel_path) for rel_path in _SOURCE_RELATIVE_PATHS)


def _emit_captured(proc: subprocess.CompletedProcess[str]) -> None:
    if proc.stdout:
        sys.stdout.write(proc.stdout)
        if not proc.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        if not proc.stderr.endswith("\n"):
            sys.stderr.write("\n")


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=capture,
        check=False,
    )


def _run_step(name: str, argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    print(f"==> {name}")
    proc = _run(argv, cwd=cwd, env=env)
    if proc.returncode != 0:
        raise _LauncherFailure(proc.returncode)


def _print_python_banner(python: PythonSelection) -> None:
    print(f"==> Using Python: {python.source} -> {python.command_path}")
    if python.executable:
        print(f"==> Python executable: {python.executable}")
    if python.version:
        print(f"==> Python version: {python.version}")


def _print_setup_hint(shell: ShellStyle, python_command: str) -> None:
    print("==> Setup hint")
    print("    Choose a setup mode:")
    print("      - Default (recommended): installs deps + editable installs")
    print(f"          {shell.smoke_command}")
    print("      - From-source: installs deps + sets PYTHONPATH (no editables)")
    print(f"          {shell.smoke_use_pythonpath_command}")
    print("      - No-install: assumes deps + local packages are already importable")
    print(f"          {shell.smoke_skip_install_command}  # deps already provisioned")
    if not os.environ.get("VIRTUAL_ENV") and not os.environ.get("CI"):
        print("    Recommended venv:")
        print(f"      {python_command} -m venv .venv")
        print(f"      {shell.activate_hint}")


def _print_skip_install_guidance(shell: ShellStyle, python_command: str) -> None:
    err = sys.stderr
    print("    You passed --skip-install, so this script will not run any installs.", file=err)
    print(
        "    That means it will NOT install requirements-dev.txt and it will NOT "
        "install local monorepo packages.",
        file=err,
    )
    print("", file=err)
    print("    Choose one setup mode:", file=err)
    print("      - Default (recommended for dev):", file=err)
    print(f"          {shell.smoke_command}", file=err)
    print("      - From-source (no editables, but installs deps):", file=err)
    print(f"          {shell.smoke_use_pythonpath_command}", file=err)
    print("      - No-install (deps already provisioned):", file=err)
    print(f"          {shell.smoke_skip_install_command}", file=err)
    print("", file=err)
    print("    Tip: prefer a virtualenv to avoid global/user-site installs:", file=err)
    print(f"      {shell.venv_hint.replace('${python}', python_command)}", file=err)


def _configure_pythonpath_env(env: dict[str, str], repo_root: Path, shell: ShellStyle) -> None:
    print(f"==> Configure PYTHONPATH via {shell.set_pythonpath_script}")
    env["PYTHONPATH"] = _source_pythonpath(repo_root)
    print("PYTHONPATH set.")
    print(env["PYTHONPATH"])


def _ensure_pip_available(python_command: str, *, repo_root: Path, env: dict[str, str]) -> None:
    probe = _run([python_command, "-m", "pip", "--version"], cwd=repo_root, env=env, capture=True)
    if probe.returncode == 0:
        return

    print("==> Bootstrap pip (ensurepip)")
    _emit_captured(probe)
    ensurepip = _run(
        [python_command, "-m", "ensurepip", "--upgrade"],
        cwd=repo_root,
        env=env,
        capture=True,
    )
    _emit_captured(ensurepip)
    if ensurepip.returncode != 0:
        raise _LauncherFailure(
            ensurepip.returncode,
            "pip is required for first-run installs, but ensurepip could not provision it.",
        )

    reprobe = _run([python_command, "-m", "pip", "--version"], cwd=repo_root, env=env, capture=True)
    if reprobe.returncode != 0:
        _emit_captured(reprobe)
        raise _LauncherFailure(
            reprobe.returncode,
            "pip is required for first-run installs, but is still unavailable after ensurepip.",
        )


def _run_skip_install_preflight(
    python_command: str,
    *,
    repo_root: Path,
    env: dict[str, str],
    shell: ShellStyle,
) -> None:
    module_list = ",\n    ".join(f'"{module}"' for module in _SMOKE_IMPORT_MODULES)
    preflight_code = (
        "import importlib\n\n"
        f"mods = [\n    {module_list},\n]\n\n"
        "errors = []\n"
        "for mod in mods:\n"
        "    try:\n"
        "        importlib.import_module(mod)\n"
        "    except Exception as e:\n"
        "        errors.append((mod, f\"{type(e).__name__}: {e}\"))\n\n"
        "if errors:\n"
        "    for mod, msg in errors:\n"
        "        print(f\"{mod}: {msg}\")\n"
        "    raise SystemExit(1)\n"
    )
    proc = _run([python_command, "-c", preflight_code], cwd=repo_root, env=env, capture=True)
    if proc.returncode == 0:
        return

    print(
        "==> Smoke preflight failed: required imports are not available in this "
        "Python environment.",
        file=sys.stderr,
    )
    merged = [line for line in (proc.stdout + "\n" + proc.stderr).splitlines() if line.strip()]
    for line in merged:
        print(f"    - {line}", file=sys.stderr)
    print("", file=sys.stderr)
    _print_skip_install_guidance(shell, python_command)
    raise _LauncherFailure(1)


def _guard_import_origin(
    python_command: str,
    *,
    repo_root: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return _run(
        [python_command, "tools/smoke_import_guard.py", "--repo-root", str(repo_root)],
        cwd=repo_root,
        env=env,
        capture=True,
    )


def _is_root_without_venv() -> bool:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    try:
        return geteuid() == 0 and not os.environ.get("VIRTUAL_ENV")
    except OSError:
        return False


def _editable_install_argv(python_command: str) -> list[str]:
    argv = [python_command, "-m", "pip", "install", "--no-deps"]
    for target in _EDITABLE_INSTALL_TARGETS:
        argv.extend(["-e", target])
    return argv


def run_smoke(
    *,
    repo_root: Path,
    python: PythonSelection,
    shell: ShellStyle,
    skip_install: bool,
    use_pythonpath: bool,
    require_doctor: bool,
) -> int:
    repo_root = repo_root.resolve()
    env = os.environ.copy()
    env["USERTEST_PYTHON"] = python.command_path

    _print_python_banner(python)

    if require_doctor:
        if shutil.which("pdm") is None:
            print("Scaffold doctor required but pdm was not found on PATH.", file=sys.stderr)
            print(
                f"Install pdm (recommended): {python.command_path} -m pip install -U pdm",
                file=sys.stderr,
            )
            print(f"Or rerun without --require-doctor: {shell.smoke_command}", file=sys.stderr)
            return 1
        try:
            _run_step(
                "Scaffold doctor",
                [python.command_path, "tools/scaffold/scaffold.py", "doctor"],
                cwd=repo_root,
                env=env,
            )
        except _LauncherFailure as exc:
            return exc.exit_code
    else:
        print("==> Scaffold doctor (tool checks skipped; pdm optional)")
        print("    Note: pdm is optional; continuing with the pip-based flow.")
        print(f"    To enable tool checks: {python.command_path} -m pip install -U pdm")
        print(f"    To require doctor: {shell.smoke_require_doctor_command}")
        doctor = _run(
            [python.command_path, "tools/scaffold/scaffold.py", "doctor", "--skip-tool-checks"],
            cwd=repo_root,
            env=env,
        )
        if doctor.returncode != 0:
            return doctor.returncode

    _print_setup_hint(shell, python.command_path)

    if not skip_install:
        if _is_root_without_venv():
            print(
                "==> Note: running as root without an active virtualenv; pip installs "
                "may land in system site-packages"
            )
            print("    Recommended:")
            print("      python -m venv .venv")
            print("      source .venv/bin/activate")
        try:
            _ensure_pip_available(python.command_path, repo_root=repo_root, env=env)
        except _LauncherFailure as exc:
            print(str(exc) or "pip is required for first-run installs.", file=sys.stderr)
            return exc.exit_code

        try:
            _run_step(
                "Install base Python deps",
                [
                    python.command_path,
                    "-m",
                    "pip",
                    "install",
                    *_PIP_FLAGS,
                    "-r",
                    "requirements-dev.txt",
                ],
                cwd=repo_root,
                env=env,
            )
            if use_pythonpath:
                _configure_pythonpath_env(env, repo_root, shell)
            else:
                _run_step(
                    "Install monorepo packages (editable, no deps)",
                    _editable_install_argv(python.command_path),
                    cwd=repo_root,
                    env=env,
                )
        except _LauncherFailure as exc:
            return exc.exit_code
    elif use_pythonpath:
        _configure_pythonpath_env(env, repo_root, shell)

    if skip_install:
        try:
            _run_skip_install_preflight(
                python.command_path,
                repo_root=repo_root,
                env=env,
                shell=shell,
            )
        except _LauncherFailure as exc:
            return exc.exit_code

    print("==> Import-origin guard smoke")
    guard = _guard_import_origin(python.command_path, repo_root=repo_root, env=env)
    _emit_captured(guard)
    if guard.returncode != 0:
        if not use_pythonpath:
            print(
                "==> WARNING: 'usertest' did not import from this workspace; "
                "switching to PYTHONPATH mode."
            )
            print(
                "    (This commonly happens when another checkout is installed "
                "editable in the same interpreter.)"
            )
            use_pythonpath = True
            _configure_pythonpath_env(env, repo_root, shell)
            if skip_install:
                try:
                    _run_skip_install_preflight(
                        python.command_path,
                        repo_root=repo_root,
                        env=env,
                        shell=shell,
                    )
                except _LauncherFailure as exc:
                    return exc.exit_code
            print("==> Import-origin guard smoke")
            guard = _guard_import_origin(python.command_path, repo_root=repo_root, env=env)
            _emit_captured(guard)
            if guard.returncode != 0:
                return guard.returncode
        else:
            return guard.returncode

    try:
        for name, module_name in (
            ("CLI help smoke", "usertest.cli"),
            ("Backlog CLI help smoke", "usertest_backlog.cli"),
            ("Implement CLI help smoke", "usertest_implement.cli"),
        ):
            _run_step(
                name,
                [python.command_path, "-m", module_name, "--help"],
                cwd=repo_root,
                env=env,
            )

        _run_step(
            "Pytest smoke suite",
            [python.command_path, "-m", "pytest", "-q", *_SMOKE_TEST_TARGETS],
            cwd=repo_root,
            env=env,
        )
    except _LauncherFailure as exc:
        return exc.exit_code

    print("==> Smoke complete: all checks passed.")
    return 0


def _venv_is_healthy(venv_python: Path, *, repo_root: Path) -> bool:
    if not venv_python.exists():
        return False
    probe = _run(
        [str(venv_python), "-c", "import encodings, sys; print(sys.executable)"],
        cwd=repo_root,
        capture=True,
    )
    return probe.returncode == 0


def run_offline_first_success(
    *,
    repo_root: Path,
    python: PythonSelection,
    shell: ShellStyle,
    fixture_name: str,
) -> int:
    repo_root = repo_root.resolve()
    env = os.environ.copy()
    env["USERTEST_PYTHON"] = python.command_path

    _print_python_banner(python)

    venv_dir = repo_root / ".venv"
    venv_python = _venv_python_path(venv_dir)
    if venv_python.exists() and not _venv_is_healthy(venv_python, repo_root=repo_root):
        print("==> Existing .venv looks unhealthy; recreating it.", file=sys.stderr)
        shutil.rmtree(venv_dir, ignore_errors=True)

    if not venv_python.exists():
        print("==> Create venv (.venv)")
        create = _run(
            [python.command_path, "-m", "venv", str(venv_dir)],
            cwd=repo_root,
            env=env,
            capture=True,
        )
        if create.returncode != 0:
            _emit_captured(create)
            print(f"==> WARNING: could not create {venv_dir}.", file=sys.stderr)
            print(
                "==> Falling back to a temp venv (this does not modify your global Python).",
                file=sys.stderr,
            )
            shutil.rmtree(venv_dir, ignore_errors=True)
            venv_dir = Path(tempfile.mkdtemp(prefix="usertest_venv_"))
            venv_python = _venv_python_path(venv_dir)
            retry = _run([python.command_path, "-m", "venv", str(venv_dir)], cwd=repo_root, env=env)
            if retry.returncode != 0:
                return retry.returncode

    if not venv_python.exists():
        print(f"Failed to create venv at {venv_dir}", file=sys.stderr)
        return 1

    try:
        _run_step(
            "Install minimal deps (requirements-dev.txt)",
            [str(venv_python), "-m", "pip", "install", *_PIP_FLAGS, "-r", "requirements-dev.txt"],
            cwd=repo_root,
            env=env,
        )
    except _LauncherFailure as exc:
        return exc.exit_code

    _configure_pythonpath_env(env, repo_root, shell)

    src = repo_root / "examples" / "golden_runs" / fixture_name
    if not src.exists():
        print(f"Missing fixture dir: {src}", file=sys.stderr)
        return 1
    print("==> Copy fixture to temp dir")
    dst_root = Path(tempfile.mkdtemp(prefix="usertest_fixture_"))
    run_dir = dst_root / fixture_name
    shutil.copytree(src, run_dir)

    try:
        _run_step(
            "Re-render report from fixture copy",
            [
                str(venv_python),
                "-m",
                "usertest.cli",
                "report",
                "--repo-root",
                str(repo_root),
                "--run-dir",
                str(run_dir),
                "--recompute-metrics",
            ],
            cwd=repo_root,
            env=env,
        )
    except _LauncherFailure as exc:
        return exc.exit_code

    print(f"==> Success. Scratch run dir: {run_dir}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Shared first-run launcher for onboarding and smoke wrappers."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common_arguments(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--repo-root", default=".")
        subparser.add_argument("--python", default=sys.executable)
        subparser.add_argument("--python-source", default="launcher")
        subparser.add_argument("--python-executable", default="")
        subparser.add_argument("--python-version", default="")
        subparser.add_argument("--shell", choices=("posix", "powershell"), required=True)

    smoke = subparsers.add_parser("smoke", help="Run the shared smoke/onboarding flow.")
    add_common_arguments(smoke)
    smoke.add_argument("--skip-install", action="store_true")
    smoke.add_argument("--use-pythonpath", action="store_true")
    smoke.add_argument("--require-doctor", action="store_true")

    offline = subparsers.add_parser(
        "offline-first-success",
        help="Run the offline-safe first-success flow through the shared launcher.",
    )
    add_common_arguments(offline)
    offline.add_argument("--fixture-name", default="minimal_codex_run")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root)
    python = PythonSelection(
        command_path=args.python,
        source=args.python_source,
        executable=args.python_executable or None,
        version=args.python_version or None,
    )
    shell = _shell_style(args.shell)

    if args.command == "smoke":
        return run_smoke(
            repo_root=repo_root,
            python=python,
            shell=shell,
            skip_install=bool(args.skip_install),
            use_pythonpath=bool(args.use_pythonpath),
            require_doctor=bool(args.require_doctor),
        )
    if args.command == "offline-first-success":
        return run_offline_first_success(
            repo_root=repo_root,
            python=python,
            shell=shell,
            fixture_name=args.fixture_name,
        )
    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
