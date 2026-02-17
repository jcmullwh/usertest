from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class PublishCommandError(RuntimeError):
    pass


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    if proc.returncode == 0:
        return
    raise PublishCommandError(f"Command failed (exit {proc.returncode}): {' '.join(cmd)}")


def build_dist(package_dir: Path) -> Path:
    dist_dir = package_dir / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(dist_dir)],
        cwd=package_dir,
    )
    return dist_dir


def twine_upload(dist_dir: Path, repository_url: str, username: str, password: str) -> None:
    dists = sorted([p for p in dist_dir.iterdir() if p.is_file()])
    if not dists:
        raise PublishCommandError(f"No distributions found to upload in: {dist_dir}")

    env = os.environ.copy()
    env["TWINE_USERNAME"] = username
    env["TWINE_PASSWORD"] = password

    _run(
        [
            sys.executable,
            "-m",
            "twine",
            "upload",
            "--non-interactive",
            "--repository-url",
            repository_url,
            *[str(p) for p in dists],
        ],
        cwd=dist_dir,
        env=env,
    )
