from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

STANDALONE_HEADING = "Standalone package checkout (recommended first path)"
MONOREPO_HEADING = "Monorepo contributor workflow"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(?P<title>.+?)\s*$")

_REQUIRED_STANDALONE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("`pdm run smoke` command", re.compile(r"\bpdm\s+run\s+smoke\b")),
    ("`pdm run test` command", re.compile(r"\bpdm\s+run\s+test\b")),
    ("`pdm run lint` command", re.compile(r"\bpdm\s+run\s+lint\b")),
)

_BANNED_STANDALONE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("monorepo scaffold command", re.compile(r"tools/scaffold/scaffold\.py\s+run")),
    ("monorepo package install path", re.compile(r"pip\s+install\s+-e\s+packages/")),
    (
        "monorepo `packages/<name>` path reference",
        re.compile(r"(^|[\s'\"`])(?:\./)?packages/[A-Za-z0-9_.-]+", re.MULTILINE),
    ),
)


def _extract_section(text: str, title: str) -> str | None:
    target = title.strip().lower()
    lines = text.splitlines()

    start_index: int | None = None
    heading_level: int | None = None
    for idx, line in enumerate(lines):
        match = _HEADING_RE.match(line.strip())
        if match is None:
            continue
        if match.group("title").strip().lower() != target:
            continue
        start_index = idx
        heading_level = len(match.group(1))
        break

    if start_index is None or heading_level is None:
        return None

    body: list[str] = []
    for line in lines[start_index + 1 :]:
        match = _HEADING_RE.match(line.strip())
        if match is not None and len(match.group(1)) <= heading_level:
            break
        body.append(line)

    return "\n".join(body).strip()


def validate_readme_text(*, readme_path: Path, text: str) -> list[str]:
    issues: list[str] = []

    standalone = _extract_section(text, STANDALONE_HEADING)
    if standalone is None:
        issues.append(
            f"{readme_path}: missing required heading `{STANDALONE_HEADING}`."
        )
    else:
        for label, pattern in _REQUIRED_STANDALONE_PATTERNS:
            if pattern.search(standalone) is None:
                issues.append(
                    f"{readme_path}: standalone section is missing {label}."
                )
        for label, pattern in _BANNED_STANDALONE_PATTERNS:
            if pattern.search(standalone) is not None:
                issues.append(
                    f"{readme_path}: standalone section contains forbidden {label}."
                )

    monorepo = _extract_section(text, MONOREPO_HEADING)
    if monorepo is None:
        issues.append(
            f"{readme_path}: missing required heading `{MONOREPO_HEADING}`."
        )

    return issues


def discover_package_readmes(repo_root: Path) -> list[Path]:
    packages_dir = repo_root / "packages"
    if not packages_dir.exists():
        return []
    out: list[Path] = []
    for pkg_dir in sorted(packages_dir.iterdir()):
        if not pkg_dir.is_dir():
            continue
        readme = pkg_dir / "README.md"
        pyproject = pkg_dir / "pyproject.toml"
        if readme.exists() and pyproject.exists():
            out.append(readme)
    return out


def _validate_readmes(readmes: list[Path], repo_root: Path) -> list[str]:
    issues: list[str] = []
    for readme in readmes:
        try:
            text = readme.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            issues.append(f"{readme}: failed reading file: {exc}")
            continue
        rel = readme.resolve().relative_to(repo_root.resolve())
        issues.extend(validate_readme_text(readme_path=rel, text=text))
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate package README standalone/monorepo command context contract."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path("."),
        help="Repository root (defaults to current directory).",
    )
    parser.add_argument(
        "--readme",
        action="append",
        type=Path,
        default=[],
        help=(
            "Optional README path to validate (repeatable). "
            "Defaults to packages/*/README.md files with pyproject.toml."
        ),
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    readmes = (
        [((repo_root / p).resolve() if not p.is_absolute() else p.resolve()) for p in args.readme]
        if args.readme
        else discover_package_readmes(repo_root)
    )
    if not readmes:
        print("No package READMEs found to validate.")
        return 0

    issues = _validate_readmes(readmes, repo_root)
    if issues:
        print("README contract violations detected:")
        for issue in issues:
            print(f"- {issue}")
        return 1

    print(f"README contract passed for {len(readmes)} package README(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
