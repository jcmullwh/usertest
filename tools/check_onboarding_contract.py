from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
from types import ModuleType


TOOLS_DIR = Path(__file__).resolve().parent


def _load_contract() -> ModuleType:
    module_path = TOOLS_DIR / "onboarding_contract.py"
    spec = importlib.util.spec_from_file_location("onboarding_contract", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["onboarding_contract"] = module
    spec.loader.exec_module(module)
    return module


contract = _load_contract()


@dataclass(frozen=True)
class SurfaceRequirement:
    snippets: tuple[str, ...]
    ordered_snippets: tuple[str, ...] = ()


DEFAULT_SURFACES: dict[Path, SurfaceRequirement] = {
    Path("README.md"): SurfaceRequirement(
        snippets=(
            f"Quickstart ({contract.CANONICAL_NEWCOMER_LABEL})",
            contract.OFFLINE_FIRST_SUCCESS.windows,
            contract.OFFLINE_FIRST_SUCCESS.posix,
            f"Developer smoke ({contract.SECONDARY_SMOKE_LABEL})",
            contract.FIRST_REAL_RUN_CONSOLE,
            contract.FIRST_REAL_RUN_MODULE,
            contract.READ_ONLY_FIRST_RUN_CONSOLE,
            contract.PRIMARY_FIRST_RUN_POLICY_NOTE,
        ),
        ordered_snippets=(
            f"Quickstart ({contract.CANONICAL_NEWCOMER_LABEL})",
            f"Developer smoke ({contract.SECONDARY_SMOKE_LABEL})",
            contract.FIRST_REAL_RUN_CONSOLE,
        ),
    ),
    Path("docs/tutorials/getting-started.md"): SurfaceRequirement(
        snippets=(
            f"offline-safe first success ({contract.CANONICAL_NEWCOMER_LABEL})",
            contract.OFFLINE_FIRST_SUCCESS.windows,
            contract.OFFLINE_FIRST_SUCCESS.posix,
            f"Developer smoke ({contract.SECONDARY_SMOKE_LABEL})",
            contract.FIRST_REAL_RUN_CONSOLE,
            contract.READ_ONLY_FIRST_RUN_CONSOLE,
            contract.PRIMARY_FIRST_RUN_POLICY_NOTE,
            contract.READ_ONLY_ALTERNATIVE_NOTE,
        ),
        ordered_snippets=(
            f"offline-safe first success ({contract.CANONICAL_NEWCOMER_LABEL})",
            f"Developer smoke ({contract.SECONDARY_SMOKE_LABEL})",
            contract.FIRST_REAL_RUN_CONSOLE,
        ),
    ),
    Path("docs/how-to/run-usertest.md"): SurfaceRequirement(
        snippets=(
            contract.FIRST_REAL_RUN_MODULE,
            contract.READ_ONLY_FIRST_RUN_MODULE,
            contract.PRIMARY_FIRST_RUN_POLICY_NOTE,
            "documented fallback for source-run and explicit examples",
        )
    ),
    Path("scripts/README.md"): SurfaceRequirement(
        snippets=(
            f"Offline-safe first success ({contract.CANONICAL_NEWCOMER_LABEL})",
            contract.OFFLINE_FIRST_SUCCESS.windows,
            contract.OFFLINE_FIRST_SUCCESS.posix,
            "offline_fixture_rerender.sh",
            "offline_fixture_rerender.ps1",
            f"Smoke scripts ({contract.SECONDARY_SMOKE_LABEL})",
        ),
        ordered_snippets=(
            f"Offline-safe first success ({contract.CANONICAL_NEWCOMER_LABEL})",
            f"Smoke scripts ({contract.SECONDARY_SMOKE_LABEL})",
        ),
    ),
    Path("docs/design/adr_usertest_smoke_command.md"): SurfaceRequirement(
        snippets=(
            contract.OFFLINE_FIRST_SUCCESS.windows,
            contract.OFFLINE_FIRST_SUCCESS.posix,
            contract.SECONDARY_SMOKE_LABEL,
            "Do not add a new top-level `usertest smoke` command.",
        )
    ),
    Path("docs/README.md"): SurfaceRequirement(
        snippets=(
            "`tools/onboarding_contract.py`",
            "`python tools/check_onboarding_contract.py`",
        )
    ),
    Path("apps/usertest/README.md"): SurfaceRequirement(
        snippets=(
            contract.FIRST_REAL_RUN_CONSOLE,
            contract.READ_ONLY_FIRST_RUN_CONSOLE,
            contract.PRIMARY_FIRST_RUN_POLICY_NOTE,
        )
    ),
}


def _normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def validate_text(*, path: Path, text: str, requirement: SurfaceRequirement) -> list[str]:
    issues: list[str] = []
    normalized_text = _normalize_whitespace(text)
    for snippet in requirement.snippets:
        normalized_snippet = _normalize_whitespace(snippet)
        if normalized_snippet not in normalized_text:
            issues.append(f"{path}: missing required snippet: {snippet}")
    if requirement.ordered_snippets:
        last_index = -1
        for snippet in requirement.ordered_snippets:
            normalized_snippet = _normalize_whitespace(snippet)
            idx = normalized_text.find(normalized_snippet)
            if idx == -1:
                continue
            if idx < last_index:
                issues.append(f"{path}: snippet out of order: {snippet}")
                break
            last_index = idx
    return issues


def validate_repo(repo_root: Path, *, surfaces: dict[Path, SurfaceRequirement] | None = None) -> list[str]:
    selected = surfaces or DEFAULT_SURFACES
    issues: list[str] = []
    for rel_path, requirement in selected.items():
        full_path = repo_root / rel_path
        if not full_path.exists():
            issues.append(f"{rel_path}: file does not exist")
            continue
        try:
            text = full_path.read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(f"{rel_path}: failed reading file: {exc}")
            continue
        issues.extend(validate_text(path=rel_path, text=text, requirement=requirement))
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate onboarding docs/runtime snippets against the canonical onboarding contract."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path("."),
        help="Repository root (defaults to current directory).",
    )
    args = parser.parse_args(argv)

    issues = validate_repo(args.repo_root.resolve())
    if issues:
        print("Onboarding contract violations detected:")
        for issue in issues:
            print(f"- {issue}")
        return 1

    print(f"Onboarding contract passed for {len(DEFAULT_SURFACES)} surface(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
