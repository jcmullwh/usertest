from __future__ import annotations

import sys
from pathlib import Path


def lint_no_regex_in_policy(*, repo_root: Path) -> list[str]:
    """
    Enforce analysis principles that prevent semantic regex gating in policy logic.

    This lint is intentionally narrow and offline-safe. It inspects specific policy modules
    where introducing regex-based semantics would reintroduce brittle, phrase-dependent
    behavior.

    Parameters
    ----------
    repo_root:
        Monorepo root directory.

    Returns
    -------
    list[str]
        A list of human-readable lint errors. Empty means success.
    """

    policy_paths = [
        repo_root / "packages" / "backlog_core" / "src" / "backlog_core" / "backlog_policy.py",
    ]

    errors: list[str] = []
    for path in policy_paths:
        if not path.exists():
            errors.append(f"missing_policy_module: {path}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "re.compile(" in text:
            errors.append(f"regex_compile_forbidden: {path}")
        if "\nimport re\n" in text or "\nfrom re " in text:
            errors.append(f"regex_import_forbidden: {path}")
    return errors


def main(argv: list[str] | None = None) -> int:
    """
    CLI entrypoint.

    This script is designed to be run from the repo root:

        python tools/lint_analysis_principles.py
    """

    _ = argv
    repo_root = Path(__file__).resolve().parents[1]
    errors = lint_no_regex_in_policy(repo_root=repo_root)
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
