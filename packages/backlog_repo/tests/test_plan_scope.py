from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from backlog_repo.plan_scope import (
    assess_pr_plan_scope,
    build_plan_target_contract,
    parse_plan_target_contract_markdown,
    render_plan_target_contract_markdown,
    validate_plan_target_contract,
)


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "src" / "core.py"
    source.parent.mkdir(parents=True)
    source.write_text("def target():\n    return 1\n", encoding="utf-8")
    _git("init", cwd=tmp_path)
    _git("config", "user.name", "Plan Scope Test", cwd=tmp_path)
    _git("config", "user.email", "scope@example.test", cwd=tmp_path)
    _git("add", ".", cwd=tmp_path)
    _git("commit", "-m", "baseline", cwd=tmp_path)
    return tmp_path, _git("rev-parse", "HEAD", cwd=tmp_path)


def _plan(revision: str) -> dict:
    return {
        "case_id": "case:test",
        "problem_id": "problem:test",
        "selected_option_id": "option:test",
        "repo_revision": revision,
        "change_targets": [
            {
                "action": "modify",
                "path": "src/core.py",
                "symbols": ["core.target"],
                "change": "Apply the researched guard at the target decision.",
            },
            {
                "action": "create",
                "path": "tests/test_core.py",
                "symbols": ["test_target"],
                "change": "Replay the original scenario as a regression test.",
            },
        ],
    }


def _assess(
    contract: dict,
    changed_files: list[str],
    *,
    reviewed_head: str = "b" * 40,
    verified_head: str | None = None,
) -> dict:
    return assess_pr_plan_scope(
        contract=contract,
        changed_files=changed_files,
        diff_text="arbitrary wider semantic diff retained for model review",
        reviewed_head_oid=reviewed_head,
        verified_implementation_head=verified_head or reviewed_head,
    )


def test_target_contract_binds_case_option_revision_paths_symbols_and_intervention(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)

    contract = build_plan_target_contract(_plan(revision), repo_root=repo)

    assert validate_plan_target_contract(contract) == contract
    assert contract["schema_version"] == 3
    assert contract["repo_revision"] == revision
    assert contract["case_id"] == "case:test"
    assert contract["selected_option_id"] == "option:test"
    assert contract["targets"][0]["path"] == "src/core.py"
    assert contract["targets"][0]["symbols"] == ["core.target"]
    assert contract["targets"][0]["target_role"] == "production"
    assert contract["targets"][1]["target_role"] == "test"

    markdown = render_plan_target_contract_markdown(contract)
    assert parse_plan_target_contract_markdown(markdown) == contract
    with pytest.raises(ValueError, match="hash_mismatch"):
        parse_plan_target_contract_markdown(markdown.replace(revision, "f" * 40))


def test_version_two_target_contract_remains_readable(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    current = build_plan_target_contract(_plan(revision), repo_root=repo)
    payload = {
        **{key: value for key, value in current.items() if key != "contract_sha256"},
        "schema_version": 2,
        "contract_source": "runner_stage6_target_intent_v2",
        "targets": [
            {key: value for key, value in target.items() if key != "destination_path"}
            for target in current["targets"]
        ],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    legacy = {
        **payload,
        "contract_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }

    assert validate_plan_target_contract(legacy) == legacy


def test_target_contract_supports_file_level_and_relocation_actions(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    plan = _plan(revision)
    plan["change_targets"] = [
        {
            "action": "modify",
            "path": "config/runtime.json",
            "change": "Change the provider boundary configuration value.",
        },
        {
            "action": "delete",
            "path": "assets/obsolete-schema.json",
            "symbols": [],
            "change": "Remove the superseded schema asset.",
        },
        {
            "action": "rename",
            "path": "docs/old-name.md",
            "destination_path": "docs/new-name.md",
            "change": "Rename the retained compatibility document.",
        },
        {
            "action": "move",
            "path": "assets/source.json",
            "destination_path": "schemas/source.json",
            "change": "Move the schema to the runtime-consumed location.",
        },
        {
            "action": "create",
            "path": "schemas/new.json",
            "change": "Add the newly selected schema contract.",
        },
    ]

    contract = build_plan_target_contract(plan, repo_root=repo)

    assert [target["action"] for target in contract["targets"]] == [
        "modify",
        "delete",
        "rename",
        "move",
        "create",
    ]
    assert all(target["symbols"] == [] for target in contract["targets"])
    assert contract["targets"][2]["destination_path"] == "docs/new-name.md"
    assert contract["targets"][3]["destination_path"] == "schemas/source.json"
    assert validate_plan_target_contract(contract) == contract


def test_relocation_target_is_touched_by_its_destination_path(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    plan = _plan(revision)
    plan["change_targets"] = [
        {
            "action": "move",
            "path": "src/core.py",
            "destination_path": "src/runtime/core.py",
            "change": "Move the verified implementation boundary without changing behavior.",
        }
    ]
    contract = build_plan_target_contract(plan, repo_root=repo)

    receipt = _assess(contract, ["src/runtime/core.py"])

    assert receipt["status"] == "verified"
    assert receipt["target_receipts"][0]["path_touched"] is True
    assert receipt["target_receipts"][0]["destination_path"] == "src/runtime/core.py"


def test_contract_requires_exact_researched_revision_but_not_ast_or_file_grounding(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    plan = _plan(revision)
    plan["change_targets"] = [
        {
            "action": "create",
            "path": "new/nested/config.toml",
            "symbols": ["config:/tool/new/key", "import:future.adapter"],
            "change": "Create the selected intervention surface.",
        }
    ]

    contract = build_plan_target_contract(plan, repo_root=repo)

    assert contract["targets"][0]["symbols"] == [
        "config:/tool/new/key",
        "import:future.adapter",
    ]
    plan["repo_revision"] = "f" * 40
    with pytest.raises(ValueError, match="repository_revision_mismatch"):
        build_plan_target_contract(plan, repo_root=repo)


def test_required_planned_production_path_must_be_touched(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    contract = build_plan_target_contract(_plan(revision), repo_root=repo)

    receipt = _assess(contract, ["tests/test_core.py"])

    assert receipt["status"] == "failed"
    assert receipt["errors"] == [
        "implementation_scope_required_production_path_untouched:src/core.py"
    ]


def test_extra_paths_wider_hunks_and_untouched_test_target_are_review_advisories(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    contract = build_plan_target_contract(_plan(revision), repo_root=repo)

    receipt = _assess(
        contract,
        ["src/core.py", "src/support.py", "pyproject.toml"],
    )

    assert receipt["status"] == "verified"
    assert receipt["errors"] == []
    assert "implementation_scope_unplanned_path:src/support.py" in receipt["advisories"]
    assert "implementation_scope_unplanned_path:pyproject.toml" in receipt["advisories"]
    assert (
        "implementation_scope_planned_nonproduction_path_untouched:tests/test_core.py"
        in receipt["advisories"]
    )
    assert receipt["diff_sha256"]


def test_reviewed_head_must_equal_the_verified_implementation_head(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    contract = build_plan_target_contract(_plan(revision), repo_root=repo)

    receipt = _assess(
        contract,
        ["src/core.py"],
        reviewed_head="b" * 40,
        verified_head="c" * 40,
    )

    assert receipt["status"] == "failed"
    assert any("reviewed_head_not_verified" in error for error in receipt["errors"])
