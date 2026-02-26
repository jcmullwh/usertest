from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent_adapters.docker_exec_env import inject_docker_exec_env, looks_like_docker_exec_prefix


@dataclass(frozen=True)
class PipBootstrapResult:
    env_overrides: dict[str, str]
    meta: dict[str, Any]


def _normalize_gitlab_base_url(raw: str | None) -> str:
    base = (raw or "").strip() or "https://gitlab.com"
    if "://" not in base:
        base = "https://" + base
    return base.rstrip("/")


def _gitlab_machine(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.netloc:
        return parsed.netloc
    # Fallback: strip any path-ish content.
    return base_url.replace("https://", "").replace("http://", "").split("/")[0]


def _gitlab_index_url(base_url: str, project_id: str) -> str:
    return f"{base_url.rstrip('/')}/api/v4/projects/{project_id}/packages/pypi/simple"


def _detect_gitlab_pypi_settings(env: Mapping[str, str]) -> tuple[str, str] | None:
    project_id = env.get("GITLAB_PYPI_PROJECT_ID", "").strip()
    if not project_id:
        return None
    username = env.get("GITLAB_PYPI_USERNAME", "").strip()
    password = env.get("GITLAB_PYPI_PASSWORD", "").strip()
    if not username or not password:
        raise RuntimeError(
            "GITLAB_PYPI_PROJECT_ID is set, but missing GITLAB_PYPI_USERNAME/"
            "GITLAB_PYPI_PASSWORD in environment."
        )
    base_url = _normalize_gitlab_base_url(env.get("GITLAB_BASE_URL"))
    return _gitlab_machine(base_url), _gitlab_index_url(base_url, project_id)


def _venv_bin_dir(*, is_windows: bool) -> str:
    return "Scripts" if is_windows else "bin"


def _venv_python_relpath(*, is_windows: bool) -> str:
    if is_windows:
        return ".venv/Scripts/python.exe"
    return ".venv/bin/python"


def _build_agent_venv_env_overrides(
    *,
    venv_dir_for_agent: str,
    venv_bin_for_agent: str,
    base_path: str,
    path_sep: str,
) -> dict[str, str]:
    base = base_path.strip()
    path_value = f"{venv_bin_for_agent}{path_sep}{base}" if base else venv_bin_for_agent
    return {
        "VIRTUAL_ENV": venv_dir_for_agent,
        "PATH": path_value,
    }


def _run_logged(
    argv: list[str],
    *,
    cwd: Path | None,
    env: dict[str, str] | None,
    log: list[str],
) -> subprocess.CompletedProcess[str]:
    log.append("$ " + " ".join(argv))
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    log.append(f"exit_code={proc.returncode}")
    if proc.stdout.strip():
        log.append("stdout:")
        log.append(proc.stdout.rstrip())
    if proc.stderr.strip():
        log.append("stderr:")
        log.append(proc.stderr.rstrip())
    log.append("")
    return proc


def _write_log(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def bootstrap_pip_requirements(
    *,
    workspace_dir: Path,
    requirements_relpath: str,
    run_dir: Path,
    command_prefix: list[str],
    workspace_mount: str | None,
    installer: str = "pip",
) -> PipBootstrapResult:
    """
    Create a fresh `.venv/` in the workspace and install requirements via pip or pdm.

    GitLab PyPI support
    -------------------
    If `GITLAB_PYPI_PROJECT_ID` is set, we treat it as the default index and use a temporary
    netrc file for auth based on `GITLAB_PYPI_USERNAME`/`GITLAB_PYPI_PASSWORD`.
    """

    installer_mode = installer.strip().lower()
    if installer_mode not in {"pip", "pdm"}:
        raise ValueError(
            f"Unsupported package installer mode: {installer!r} (expected 'pip' or 'pdm')."
        )

    backend_is_docker = looks_like_docker_exec_prefix(command_prefix)

    gitlab = _detect_gitlab_pypi_settings(os.environ)
    gitlab_host: str | None = None
    gitlab_index_url: str | None = None
    extra_index_url: str | None = None
    if gitlab is not None:
        gitlab_host, gitlab_index_url = gitlab
        extra_index_url = "https://pypi.org/simple"

    venv_dir = "/workspace/.venv" if backend_is_docker else ".venv"
    venv_python = f"{venv_dir}/bin/python" if backend_is_docker else _venv_python_relpath(
        is_windows=os.name == "nt"
    )

    bootstrap_meta: dict[str, Any] = {
        "kind": "pip_bootstrap",
        "installer": installer_mode,
        "backend": "docker" if backend_is_docker else "local",
        "venv_dir_requested": venv_dir,
        "venv_dir": venv_dir,
        "requirements_file": requirements_relpath,
        "gitlab_index_url": gitlab_index_url,
        "extra_index_url": extra_index_url,
    }

    log_lines: list[str] = []

    if backend_is_docker:
        # A single sh script keeps secrets out of host-side command lines.
        env_overrides = {
            "USERTEST_GITLAB_HOST": gitlab_host or "",
            "USERTEST_GITLAB_INDEX_URL": gitlab_index_url or "",
            "USERTEST_PIP_EXTRA_INDEX_URL": extra_index_url or "",
        }
        prefix = inject_docker_exec_env(command_prefix, env_overrides)
        requested_venv_dir = venv_dir
        requested_venv_python = venv_python
        effective_venv_dir = requested_venv_dir
        effective_venv_python = requested_venv_python

        def _run_docker_bootstrap_for_venv(
            docker_venv_dir: str,
            docker_venv_python: str,
        ) -> subprocess.CompletedProcess[str]:
            if installer_mode == "pdm":
                script = "\n".join(
                    [
                        "set -e",
                        "cd /workspace",
                        f"rm -rf {docker_venv_dir}",
                        (
                            "if command -v python3 >/dev/null 2>&1; then py=python3; "
                            "else py=python; fi"
                        ),
                        f"$py -m venv --copies {docker_venv_dir}",
                        f"venv_py={docker_venv_python}",
                        (
                            "PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 "
                            "$venv_py -m pip install --upgrade pip"
                        ),
                        'if [ -n "${USERTEST_GITLAB_INDEX_URL:-}" ]; then',
                        (
                            '  if [ -z "${GITLAB_PYPI_USERNAME:-}" ] || '
                            '[ -z "${GITLAB_PYPI_PASSWORD:-}" ]; then'
                        ),
                        (
                            '    echo "Missing GITLAB_PYPI_USERNAME/GITLAB_PYPI_PASSWORD '
                            'in container env." 1>&2'
                        ),
                        "    exit 2",
                        "  fi",
                        "  home=$(mktemp -d)",
                        "  trap 'rm -rf \"$home\"' EXIT",
                        "  netrc=\"$home/.netrc\"",
                        "  (",
                        "    echo \"machine ${USERTEST_GITLAB_HOST}\"",
                        "    echo \"login ${GITLAB_PYPI_USERNAME}\"",
                        "    echo \"password ${GITLAB_PYPI_PASSWORD}\"",
                        "  ) > \"$netrc\"",
                        "  chmod 600 \"$netrc\"",
                        (
                            "  HOME=\"$home\" PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 "
                            "$venv_py -m pip install pdm"
                        ),
                        (
                            "  HOME=\"$home\" PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 "
                            "PIP_INDEX_URL=\"${USERTEST_GITLAB_INDEX_URL}\" "
                            "PIP_EXTRA_INDEX_URL=\"${USERTEST_PIP_EXTRA_INDEX_URL}\" "
                            "$venv_py -m pdm install --no-self"
                        ),
                        "else",
                        (
                            "  PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 "
                            "$venv_py -m pip install pdm"
                        ),
                        (
                            "  PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 "
                            "$venv_py -m pdm install --no-self"
                        ),
                        "fi",
                        "exit 0",
                        "",
                    ]
                )
            else:
                script = "\n".join(
                    [
                        "set -e",
                        "cd /workspace",
                        f"rm -rf {docker_venv_dir}",
                        (
                            "if command -v python3 >/dev/null 2>&1; then py=python3; "
                            "else py=python; fi"
                        ),
                        f"$py -m venv --copies {docker_venv_dir}",
                        f"venv_py={docker_venv_python}",
                        'if [ -n "${USERTEST_GITLAB_INDEX_URL:-}" ]; then',
                        (
                            '  if [ -z "${GITLAB_PYPI_USERNAME:-}" ] || '
                            '[ -z "${GITLAB_PYPI_PASSWORD:-}" ]; then'
                        ),
                        (
                            '    echo "Missing GITLAB_PYPI_USERNAME/GITLAB_PYPI_PASSWORD '
                            'in container env." 1>&2'
                        ),
                        "    exit 2",
                        "  fi",
                        "  home=$(mktemp -d)",
                        "  trap 'rm -rf \"$home\"' EXIT",
                        "  netrc=\"$home/.netrc\"",
                        "  (",
                        "    echo \"machine ${USERTEST_GITLAB_HOST}\"",
                        "    echo \"login ${GITLAB_PYPI_USERNAME}\"",
                        "    echo \"password ${GITLAB_PYPI_PASSWORD}\"",
                        "  ) > \"$netrc\"",
                        "  chmod 600 \"$netrc\"",
                        (
                            "  pip_args=\"--index-url ${USERTEST_GITLAB_INDEX_URL} "
                            "--extra-index-url ${USERTEST_PIP_EXTRA_INDEX_URL} --pre\""
                        ),
                        (
                            '  HOME="$home" PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 '
                            "$venv_py -m pip install --upgrade pip"
                        ),
                        (
                            f'  HOME="$home" PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 '
                            f"$venv_py -m pip install $pip_args -r {requirements_relpath}"
                        ),
                        "else",
                        (
                            "  PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 "
                            "$venv_py -m pip install --upgrade pip"
                        ),
                        (
                            f"  PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 "
                            f"$venv_py -m pip install -r {requirements_relpath}"
                        ),
                        "fi",
                        "exit 0",
                        "",
                    ]
                )
            return _run_logged([*prefix, "sh", "-lc", script], cwd=None, env=None, log=log_lines)

        proc = _run_docker_bootstrap_for_venv(effective_venv_dir, effective_venv_python)
        stderr_lc = (proc.stderr or "").lower()
        should_fallback_to_tmp = (
            proc.returncode != 0
            and effective_venv_dir == "/workspace/.venv"
            and "operation not permitted" in stderr_lc
            and "/workspace/.venv" in stderr_lc
        )
        if should_fallback_to_tmp:
            fallback_venv_dir = "/tmp/usertest_pip_venv"
            fallback_venv_python = f"{fallback_venv_dir}/bin/python"
            log_lines.append(
                "Workspace venv creation failed; retrying pip bootstrap with "
                f"{fallback_venv_dir}."
            )
            log_lines.append("")
            proc = _run_docker_bootstrap_for_venv(fallback_venv_dir, fallback_venv_python)
            if proc.returncode == 0:
                effective_venv_dir = fallback_venv_dir
                effective_venv_python = fallback_venv_python

        bootstrap_meta["venv_dir"] = effective_venv_dir
        bootstrap_meta["venv_fallback_used"] = effective_venv_dir != requested_venv_dir
        _write_log(run_dir / "bootstrap_pip.log", log_lines)
        if proc.returncode != 0:
            raise RuntimeError(
                "pip bootstrap failed inside docker sandbox. "
                "See bootstrap_pip.log. "
                "Tip: pass required env vars via --exec-env (e.g., "
                "GITLAB_PYPI_PROJECT_ID/GITLAB_PYPI_USERNAME/GITLAB_PYPI_PASSWORD)."
            )

        pip_list = subprocess.run(
            [*command_prefix, effective_venv_python, "-m", "pip", "list", "--format=json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if pip_list.returncode == 0 and pip_list.stdout.strip():
            (run_dir / "bootstrap_pip_list.json").write_text(pip_list.stdout, encoding="utf-8")

        base_path_proc = subprocess.run(
            [*command_prefix, "sh", "-lc", 'printf "%s" "$PATH"'],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        base_path = base_path_proc.stdout.strip() if base_path_proc.returncode == 0 else ""

        venv_dir_for_agent = effective_venv_dir
        venv_bin_for_agent = f"{effective_venv_dir}/bin"
        agent_env = _build_agent_venv_env_overrides(
            venv_dir_for_agent=venv_dir_for_agent,
            venv_bin_for_agent=venv_bin_for_agent,
            base_path=base_path,
            path_sep=":",
        )

        (run_dir / "bootstrap_pip.json").write_text(
            json.dumps({**bootstrap_meta, "status": "success"}, indent=2, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        return PipBootstrapResult(env_overrides=agent_env, meta=bootstrap_meta)

    # Local backend: create venv and install into it without mutating global site-packages.
    local_venv_dir = workspace_dir / ".venv"
    if local_venv_dir.exists():
        shutil.rmtree(local_venv_dir, ignore_errors=True)

    proc = _run_logged(
        [sys.executable, "-m", "venv", ".venv"],
        cwd=workspace_dir,
        env=None,
        log=log_lines,
    )
    if proc.returncode != 0:
        _write_log(run_dir / "bootstrap_pip.log", log_lines)
        raise RuntimeError("Failed to create virtualenv for pip bootstrap. See bootstrap_pip.log.")

    venv_python = workspace_dir / Path(venv_python)
    base_env = os.environ.copy()
    base_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    base_env["PIP_NO_INPUT"] = "1"

    temp_root = run_dir / "tmp"
    temp_root_str: str | None = None
    try:
        temp_root.mkdir(parents=True, exist_ok=True)
        temp_root_str = str(temp_root)
        # Ensure pip/pytest subprocesses use a guaranteed-writable temp root. This mitigates
        # sandboxed / enterprise Windows environments where the default temp directory can be
        # non-writable (e.g., pip build-tracker / editable install failures).
        base_env["TMPDIR"] = temp_root_str
        base_env["TMP"] = temp_root_str
        base_env["TEMP"] = temp_root_str
    except OSError:
        temp_root_str = None

    if temp_root_str is not None:
        log_lines.append(f"temp_root={temp_root_str}")
        log_lines.append(f"TMPDIR={base_env.get('TMPDIR', '')}")
        log_lines.append(f"TMP={base_env.get('TMP', '')}")
        log_lines.append(f"TEMP={base_env.get('TEMP', '')}")
        log_lines.append("")

    def _run_pip(argv: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return _run_logged(argv, cwd=workspace_dir, env=env, log=log_lines)

    td_kwargs: dict[str, str] = {}
    if temp_root_str is not None:
        td_kwargs["dir"] = temp_root_str

    with tempfile.TemporaryDirectory(prefix="usertest_pip_home_", **td_kwargs) as td:
        home = Path(td)
        # pip supports netrc for HTTP basic auth; create both names for Windows safety.
        if gitlab_index_url is not None:
            username = base_env.get("GITLAB_PYPI_USERNAME", "").strip()
            password = base_env.get("GITLAB_PYPI_PASSWORD", "").strip()
            netrc_body = "\n".join(
                [
                    f"machine {gitlab_host}",
                    f"login {username}",
                    f"password {password}",
                    "",
                ]
            )
            (home / ".netrc").write_text(netrc_body, encoding="utf-8")
            (home / "_netrc").write_text(netrc_body, encoding="utf-8")

        env = {**base_env}
        env["HOME"] = str(home)
        if os.name == "nt":
            env["USERPROFILE"] = str(home)

        upgrade = _run_pip([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"], env=env)
        if upgrade.returncode != 0:
            _write_log(run_dir / "bootstrap_pip.log", log_lines)
            raise RuntimeError(
                "pip bootstrap failed while upgrading pip. See bootstrap_pip.log."
            )

        if installer_mode == "pdm":
            install_pdm = _run_pip([str(venv_python), "-m", "pip", "install", "pdm"], env=env)
            if install_pdm.returncode != 0:
                _write_log(run_dir / "bootstrap_pip.log", log_lines)
                raise RuntimeError(
                    "pdm bootstrap failed while installing pdm. See bootstrap_pip.log."
                )

            pdm_install = [str(venv_python), "-m", "pdm", "install", "--no-self"]
            if gitlab_index_url is not None:
                assert extra_index_url is not None
                env["PIP_INDEX_URL"] = gitlab_index_url
                env["PIP_EXTRA_INDEX_URL"] = extra_index_url
            proc = _run_pip(pdm_install, env=env)
        else:
            pip_install = [str(venv_python), "-m", "pip", "install", "-r", requirements_relpath]
            if gitlab_index_url is not None:
                assert extra_index_url is not None
                pip_install = [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--index-url",
                    gitlab_index_url,
                    "--extra-index-url",
                    extra_index_url,
                    "--pre",
                    "-r",
                    requirements_relpath,
                ]
            proc = _run_pip(pip_install, env=env)
        _write_log(run_dir / "bootstrap_pip.log", log_lines)
        if proc.returncode != 0:
            raise RuntimeError("pip bootstrap failed. See bootstrap_pip.log.")

    pip_list = subprocess.run(
        [str(venv_python), "-m", "pip", "list", "--format=json"],
        cwd=str(workspace_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=base_env,
    )
    if pip_list.returncode == 0 and pip_list.stdout.strip():
        (run_dir / "bootstrap_pip_list.json").write_text(pip_list.stdout, encoding="utf-8")

    venv_dir_for_agent = str(local_venv_dir)
    venv_bin_for_agent = str(local_venv_dir / _venv_bin_dir(is_windows=os.name == "nt"))
    agent_env = _build_agent_venv_env_overrides(
        venv_dir_for_agent=venv_dir_for_agent,
        venv_bin_for_agent=venv_bin_for_agent,
        base_path=os.environ.get("PATH", ""),
        path_sep=os.pathsep,
    )

    (run_dir / "bootstrap_pip.json").write_text(
        json.dumps({**bootstrap_meta, "status": "success"}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return PipBootstrapResult(env_overrides=agent_env, meta=bootstrap_meta)
