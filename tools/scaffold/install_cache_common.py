from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


INSTALL_CACHE_SCHEMA_VERSION = 1
_PDM_VERSION_RE = re.compile(r"(?P<version>\d+(?:\.\d+)+)")


@dataclass(frozen=True)
class InstallCacheFingerprint:
    project_id: str
    project_path: str
    fingerprint: str
    payload: dict[str, Any]


def safe_cache_project_id(project_id: str) -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in {"_", ".", "-"} else "-" for ch in (project_id or "")
    ).strip("-.")
    return cleaned or "project"


def sha256_file_or_none(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text_file_normalized_or_none(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def project_relpath_for_cache(*, repo_root: Path, project_dir: Path) -> str:
    try:
        rel = project_dir.resolve().relative_to(repo_root.resolve())
    except Exception:
        return project_dir.name
    return rel.as_posix()


def probe_pdm_version(*, cwd: Path) -> str:
    try:
        cp = subprocess.run(
            ["pdm", "--version"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    combined = "\n".join(x for x in (cp.stdout, cp.stderr) if x).strip()
    line = combined.splitlines()[0].strip() if combined else ""
    return normalize_pdm_version(line) or "unknown"


def normalize_pdm_version(raw: str | None) -> str:
    cleaned = (raw or "").strip()
    if not cleaned:
        return "unknown"
    match = _PDM_VERSION_RE.search(cleaned)
    if match is None:
        return cleaned
    return match.group("version")


def build_install_cache_payload(
    *,
    repo_root: Path,
    project_dir: Path,
    project_id: str,
    install_cmd: list[str],
    python_major_minor: str | None = None,
    pdm_version: str | None = None,
) -> dict[str, Any]:
    resolved_python = python_major_minor or f"{sys.version_info.major}.{sys.version_info.minor}"
    resolved_pdm = normalize_pdm_version(pdm_version) if pdm_version else probe_pdm_version(cwd=project_dir)
    return {
        "schema_version": INSTALL_CACHE_SCHEMA_VERSION,
        "project_id": project_id,
        "project_path": project_relpath_for_cache(repo_root=repo_root, project_dir=project_dir),
        "pyproject_sha256": sha256_text_file_normalized_or_none(project_dir / "pyproject.toml"),
        "pdm_lock_sha256": sha256_text_file_normalized_or_none(project_dir / "pdm.lock"),
        "python_major_minor": resolved_python,
        "pdm_version": resolved_pdm,
        "install_cmd": list(install_cmd),
    }


def compute_install_cache_fingerprint(*, payload: dict[str, Any]) -> str:
    fingerprint_src = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(fingerprint_src.encode("utf-8")).hexdigest()


def build_install_cache_fingerprint(
    *,
    repo_root: Path,
    project_dir: Path,
    project_id: str,
    install_cmd: list[str],
    python_major_minor: str | None = None,
    pdm_version: str | None = None,
) -> InstallCacheFingerprint:
    payload = build_install_cache_payload(
        repo_root=repo_root,
        project_dir=project_dir,
        project_id=project_id,
        install_cmd=install_cmd,
        python_major_minor=python_major_minor,
        pdm_version=pdm_version,
    )
    return InstallCacheFingerprint(
        project_id=project_id,
        project_path=str(payload["project_path"]),
        fingerprint=compute_install_cache_fingerprint(payload=payload),
        payload=payload,
    )
