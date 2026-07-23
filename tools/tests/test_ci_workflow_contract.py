from __future__ import annotations

from pathlib import Path


def _job_lines(workflow: str, job_name: str) -> list[str]:
    lines = workflow.splitlines()
    start = lines.index(f"  {job_name}:") + 1
    end = next(
        (
            index
            for index in range(start, len(lines))
            if lines[index].startswith("  ")
            and not lines[index].startswith("    ")
            and lines[index].endswith(":")
        ),
        len(lines),
    )
    return lines[start:end]


def test_package_smoke_jobs_have_bounded_runtime() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    workflow = (repo_root / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    for job_name in ("package_smoke_linux", "package_smoke_windows"):
        assert "    timeout-minutes: 20" in _job_lines(workflow, job_name)
