from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from backlog_core import BacklogPolicyConfig, assign_plan_revision_id
from runner_core import find_repo_root

import usertest_backlog.workflows.qualification as qualification_module
import usertest_backlog.workflows.shadow_validation as shadow_module
import usertest_backlog.workflows.staged as staged_module
from usertest_backlog.cli import main
from usertest_backlog.workflows.pipeline_provenance import (
    first_party_module_binding_errors,
)
from usertest_backlog.workflows.qualification import (
    build_qualification_corpus_manifest,
)
from usertest_backlog.workflows.qualification_repair_materialization import (
    materialize_repaired_shadow_run,
)
from usertest_backlog.workflows.qualification_repair_runtime import (
    QualificationRepairRuntimeResult,
)
from usertest_backlog.workflows.qualification_transaction import (
    build_qualification_adjudication_template,
    build_qualification_input_bundle,
    capture_qualification_preparation_snapshot,
    capture_qualification_source_snapshot,
    current_pipeline_runtime_compatibility,
    finalize_qualification_adjudication,
    qualification_input_bundle_errors,
    qualification_runtime_compatibility_errors,
    write_qualification_input_bundle,
)
from usertest_backlog.workflows.shadow_validation import write_pending_shadow_run
from usertest_backlog.workflows.staged import (
    _prepare_or_validate_qualification_cycle_namespace,
    _qualification_cycle_contract,
)


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path, name: str = "repo") -> tuple[Path, str]:
    repo = tmp_path / name
    (repo / "apps" / "usertest_backlog" / "src").mkdir(parents=True)
    (repo / "apps" / "usertest_backlog" / "src" / "pipeline.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    source_modules = {
        "apps/usertest/src/usertest/__init__.py": "\"\"\"CLI source.\"\"\"\n",
        "apps/usertest_backlog/src/usertest_backlog/__init__.py": (
            "\"\"\"Backlog source.\"\"\"\n"
        ),
        "apps/usertest_implement/src/usertest_implement/__init__.py": (
            "\"\"\"Implementation source.\"\"\"\n"
        ),
        **{
            f"packages/{package}/src/{package}/__init__.py": (
                f"\"\"\"{package} source.\"\"\"\n"
            )
            for package in (
                "agent_adapters",
                "backlog_core",
                "backlog_miner",
                "backlog_repo",
                "normalized_events",
                "reporter",
                "run_artifacts",
                "runner_core",
                "sandbox_runner",
                "token_monitoring",
                "triage_engine",
            )
        },
        "packages/sandbox_runner/src/sandbox_runner/builtins/docker/contexts/"
        "sandbox_cli/scripts/install_manifests.sh": "#!/bin/sh\nexit 0\n",
    }
    for relative, content in source_modules.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for shim in ("usertest", "usertest_backlog", "usertest_implement"):
        path = repo / shim / "__init__.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"\"\"\"{shim} checkout shim.\"\"\"\n", encoding="utf-8")
    component_roots = [
        repo / "apps" / "usertest",
        repo / "apps" / "usertest_backlog",
        repo / "apps" / "usertest_implement",
        *(repo / "packages" / package for package in (
            "agent_adapters",
            "backlog_core",
            "backlog_miner",
            "backlog_repo",
            "normalized_events",
            "reporter",
            "run_artifacts",
            "runner_core",
            "sandbox_runner",
            "token_monitoring",
            "triage_engine",
        )),
    ]
    for component_root in component_roots:
        (component_root / "pyproject.toml").write_text(
            "[project]\nname = 'fixture'\nversion = '0.0.0'\n",
            encoding="utf-8",
        )
        (component_root / "pdm.lock").write_text("# fixture lock\n", encoding="utf-8")
        (component_root / "README.md").write_text("# fixture project\n", encoding="utf-8")
    (repo / "configs").mkdir()
    (repo / "configs" / "backlog_policy.yaml").write_text(
        "backlog_policy: {}\n",
        encoding="utf-8",
    )
    configuration_files = {
        "policies.yaml": "policies: {}\n",
        "catalog.yaml": "defaults: {}\n",
        "maintenance_docker.yaml": "maintenance_docker: {}\n",
        "prompt_templates/inline_report_v1.prompt.md": "Report prompt.\n",
        "report_schemas/troubleshoot_v1.schema.json": "{}\n",
        "personas/builtin/developer_integrator.persona.md": "Developer.\n",
        "personas/builtin/repo_backlog_investigator.persona.md": "Investigator.\n",
    }
    for relative, content in configuration_files.items():
        path = repo / "configs" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    maintenance_script = repo / "tools" / "maintenance_image" / "prepare_context.py"
    maintenance_script.parent.mkdir(parents=True)
    maintenance_script.write_text("VALUE = 'maintenance'\n", encoding="utf-8")
    scaffold_manifest = repo / "tools" / "scaffold" / "monorepo.toml"
    scaffold_manifest.parent.mkdir(parents=True)
    scaffold_manifest.write_text("schema_version = 1\n", encoding="utf-8")
    (repo / "requirements-dev.txt").write_text("pytest\n", encoding="utf-8")
    scripts = repo / "scripts"
    scripts.mkdir()
    (scripts / "smoke.ps1").write_text("exit 0\n", encoding="utf-8")
    (scripts / "smoke.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "qualification@example.test")
    _git(repo, "config", "user.name", "Qualification Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _bundle_inputs(tmp_path: Path) -> dict[str, object]:
    repo, revision = _repo(tmp_path)
    source_runs = tmp_path / "frozen" / "usertest"
    implementation_runs = tmp_path / "frozen" / "usertest_implement"
    _write_json(source_runs / "run" / "report.json", {"status": "failed"})
    _write_json(implementation_runs / "run" / "report.json", {"status": "failed"})
    ledger = tmp_path / "custody" / "atom_actions.yaml"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("{}\n", encoding="utf-8")
    seed = _write_json(tmp_path / "custody" / "case_registry.json", {"cases": {}})
    return {
        "atoms": [
            {
                "atom_id": "atom:observed",
                "source": "run_failure",
                "evidence_role": "observation",
                "evidence_class": "observed_failure",
                "severity": "high",
                "text": "The automated run failed after collecting evidence.",
            }
        ],
        "repo_root": repo,
        "repo_input": repo,
        "research_ref": revision,
        "source_runs_dir": source_runs,
        "atom_actions_path": ledger,
        "case_registry_seed_path": seed,
        "target": "fixture",
        "breadth_profile": "standard",
    }


def _build_bundle(inputs: dict[str, object], **overrides: object) -> dict[str, object]:
    return build_qualification_input_bundle(**{**inputs, **overrides})  # type: ignore[arg-type]


def test_bundle_identity_ignores_prior_decisions_and_excludes_idea_atoms(
    tmp_path: Path,
) -> None:
    inputs = _bundle_inputs(tmp_path)
    first = _build_bundle(inputs)
    changed_atoms = deepcopy(inputs["atoms"])
    assert isinstance(changed_atoms, list)
    changed_atoms[0].update(
        {
            "case_id": "case:prior",
            "disposition": "novel_case",
            "disposition_status": "decided",
            "novel_case_rationale": "A prior model decision.",
            "status_reopen_audit": {"reopened_at": "2099-01-01T00:00:00Z"},
        }
    )
    changed_atoms.append(
        {
            "atom_id": "atom:idea",
            "source": "idea",
            "text": "A user-originated proposal is outside automated mining.",
        }
    )
    second = _build_bundle(inputs, atoms=changed_atoms)

    assert first["content_sha256"] == second["content_sha256"]
    assert [item["atom_id"] for item in second["atoms"]] == ["atom:observed"]
    assert "case_id" not in second["atoms"][0]
    assert "novel_case_rationale" not in second["atoms"][0]
    assert "status_reopen_audit" not in second["atoms"][0]


def test_bundle_rejects_source_change_during_extraction(tmp_path: Path) -> None:
    inputs = _bundle_inputs(tmp_path)
    source_runs = inputs["source_runs_dir"]
    assert isinstance(source_runs, Path)
    snapshot = capture_qualification_source_snapshot(source_runs)
    _write_json(source_runs / "arrived-during-extraction" / "report.json", {"new": True})

    with pytest.raises(ValueError, match="source_changed_during_extraction"):
        _build_bundle(inputs, source_input_snapshot=snapshot)


def test_source_snapshot_excludes_maintenance_virtualenv_copies(tmp_path: Path) -> None:
    inputs = _bundle_inputs(tmp_path)
    source_runs = inputs["source_runs_dir"]
    assert isinstance(source_runs, Path)
    implementation_runs = source_runs.parent / "usertest_implement"
    cached_python = (
        implementation_runs
        / "run"
        / "sandbox"
        / "maintenance_venv_copies"
        / "agent_adapters"
        / "fingerprint"
        / "venv"
        / "bin"
        / "python"
    )
    cached_python.parent.mkdir(parents=True)
    cached_python.write_bytes(b"not evidence\n")

    snapshot = capture_qualification_source_snapshot(source_runs)
    implementation = snapshot["implementation_runs"]
    paths = [entry["path"] for entry in implementation["entries"]]

    assert implementation["ignored_directory_names"] == ["maintenance_venv_copies"]
    assert "run/report.json" in paths
    assert not any("maintenance_venv_copies" in path for path in paths)


def test_bundle_detects_pipeline_ledger_and_full_plan_tree_mutation(
    tmp_path: Path,
) -> None:
    inputs = _bundle_inputs(tmp_path)
    repo = inputs["repo_root"]
    ledger = inputs["atom_actions_path"]
    assert isinstance(repo, Path)
    assert isinstance(ledger, Path)
    idea = repo / ".agents" / "plans" / "1 - ideas" / "plain title.md"
    idea.parent.mkdir(parents=True)
    idea.write_text("A user idea with no marker text.\n", encoding="utf-8")
    bundle = _build_bundle(inputs)
    assert qualification_input_bundle_errors(bundle, verify_files=True) == []

    idea.unlink()
    assert "qualification_input_tree_changed:protected:0" in qualification_input_bundle_errors(
        bundle,
        verify_files=True,
    )
    idea.write_text("A user idea with no marker text.\n", encoding="utf-8")
    ledger.write_text("atom:observed:\n  status: new\n", encoding="utf-8")
    assert any(
        "atom_actions" in error and "changed" in error
        for error in qualification_input_bundle_errors(bundle, verify_files=True)
    )
    ledger.write_text("{}\n", encoding="utf-8")
    (repo / "apps" / "usertest_backlog" / "src" / "pipeline.py").write_text(
        "VALUE = 2\n",
        encoding="utf-8",
    )
    assert any(
        error.startswith("qualification_input_pipeline_")
        for error in qualification_input_bundle_errors(bundle, verify_files=True)
    )


def test_every_sealed_pipeline_file_mutation_invalidates_bundle(tmp_path: Path) -> None:
    inputs = _bundle_inputs(tmp_path)
    repo = inputs["repo_root"]
    assert isinstance(repo, Path)
    bundle = _build_bundle(inputs)
    pipeline = bundle["pipeline"]
    assert isinstance(pipeline, dict)
    manifest = pipeline["files"]
    assert isinstance(manifest, dict)
    receipts = manifest["files"]
    assert isinstance(receipts, list)
    relative_paths = {
        str(receipt["path"])
        for receipt in receipts
        if isinstance(receipt, dict) and isinstance(receipt.get("path"), str)
    }
    required_examples = {
        "usertest_backlog/__init__.py",
        "apps/usertest/src/usertest/__init__.py",
        "apps/usertest_backlog/src/usertest_backlog/__init__.py",
        "apps/usertest_backlog/README.md",
        "apps/usertest_implement/src/usertest_implement/__init__.py",
        "packages/run_artifacts/src/run_artifacts/__init__.py",
        "packages/runner_core/README.md",
        "packages/token_monitoring/src/token_monitoring/__init__.py",
        "packages/triage_engine/src/triage_engine/__init__.py",
        "packages/sandbox_runner/src/sandbox_runner/builtins/docker/contexts/"
        "sandbox_cli/scripts/install_manifests.sh",
        "configs/policies.yaml",
        "configs/catalog.yaml",
        "configs/maintenance_docker.yaml",
        "configs/prompt_templates/inline_report_v1.prompt.md",
        "configs/report_schemas/troubleshoot_v1.schema.json",
        "configs/personas/builtin/developer_integrator.persona.md",
        "configs/personas/builtin/repo_backlog_investigator.persona.md",
        "tools/maintenance_image/prepare_context.py",
        "tools/scaffold/monorepo.toml",
    }
    assert required_examples <= relative_paths

    for relative in sorted(relative_paths):
        path = repo / relative
        original = path.read_bytes()
        path.write_bytes(original + b"\nqualification mutation")
        try:
            assert "qualification_input_pipeline_changed" in (
                qualification_input_bundle_errors(bundle, verify_files=True)
            ), relative
        finally:
            path.write_bytes(original)
    assert qualification_input_bundle_errors(bundle, verify_files=True) == []


def test_preparation_snapshot_rejects_post_read_drift_across_all_input_classes(
    tmp_path: Path,
) -> None:
    for mutation in (
        "source_runs",
        "implementation_runs",
        "atom_actions",
        "case_registry",
        "pipeline_config",
        "protected_plans",
        "owner_remote",
        "owner_ref",
    ):
        inputs = _bundle_inputs(tmp_path / mutation)
        repo = inputs["repo_root"]
        source_runs = inputs["source_runs_dir"]
        ledger = inputs["atom_actions_path"]
        seed = inputs["case_registry_seed_path"]
        assert isinstance(repo, Path)
        assert isinstance(source_runs, Path)
        assert isinstance(ledger, Path)
        assert isinstance(seed, Path)
        protected_plan = repo / ".agents" / "plans" / "2 - ready" / "plan.md"
        if mutation == "protected_plans":
            protected_plan.parent.mkdir(parents=True)
            protected_plan.write_text("frozen plan\n", encoding="utf-8")
        snapshot = capture_qualification_preparation_snapshot(
            repo_root=repo,
            repo_input=repo,
            research_ref=str(inputs["research_ref"]),
            source_runs_dir=source_runs,
            atom_actions_path=ledger,
            case_registry_seed_path=seed,
            owner_roots=[repo],
        )

        if mutation == "source_runs":
            _write_json(source_runs / "late" / "report.json", {"late": True})
        elif mutation == "implementation_runs":
            _write_json(
                source_runs.parent / "usertest_implement" / "late" / "report.json",
                {"late": True},
            )
        elif mutation == "atom_actions":
            ledger.write_text("atom:late:\n  status: new\n", encoding="utf-8")
        elif mutation == "case_registry":
            _write_json(seed, {"cases": {"case:late": {}}})
        elif mutation == "pipeline_config":
            (repo / "configs" / "backlog_policy.yaml").write_text(
                "backlog_policy:\n  changed: true\n",
                encoding="utf-8",
            )
        elif mutation == "protected_plans":
            protected_plan.write_text("mutated plan\n", encoding="utf-8")
        elif mutation == "owner_remote":
            _git(repo, "remote", "add", "late", "https://example.test/org/repo.git")
        elif mutation == "owner_ref":
            _git(repo, "branch", "late-qualification-ref")

        with pytest.raises(ValueError, match="changed_during_extraction"):
            _build_bundle(inputs, preparation_input_snapshot=snapshot)


def test_runtime_compatibility_projection_ignores_unrelated_apps_but_tracks_backlog_behavior(
    tmp_path: Path,
) -> None:
    inputs = _bundle_inputs(tmp_path)
    repo = inputs["repo_root"]
    assert isinstance(repo, Path)
    bundle = _build_bundle(inputs)
    pipeline = bundle["pipeline"]
    assert isinstance(pipeline, dict)
    current = current_pipeline_runtime_compatibility(repo)
    assert pipeline["runtime_compatibility"] == current["manifest"]
    assert pipeline["runtime_compatibility_sha256"] == current["sha256"]
    assert qualification_runtime_compatibility_errors(bundle, repo_root=repo) == []

    unrelated_paths = (
        repo / "apps" / "usertest" / "src" / "usertest" / "__init__.py",
        repo
        / "apps"
        / "usertest_implement"
        / "src"
        / "usertest_implement"
        / "__init__.py",
    )
    for path in unrelated_paths:
        original = path.read_bytes()
        path.write_bytes(original + b"\nUNRELATED = True\n")
        try:
            assert qualification_runtime_compatibility_errors(bundle, repo_root=repo) == []
            assert "qualification_input_pipeline_changed" in qualification_input_bundle_errors(
                bundle,
                verify_files=True,
            )
        finally:
            path.write_bytes(original)

    behavior_paths = (
        repo
        / "apps"
        / "usertest_backlog"
        / "src"
        / "usertest_backlog"
        / "__init__.py",
        repo / "configs" / "prompt_templates" / "inline_report_v1.prompt.md",
    )
    for path in behavior_paths:
        original = path.read_bytes()
        path.write_bytes(original + b"\nbehavior mutation\n")
        try:
            assert qualification_runtime_compatibility_errors(
                bundle,
                repo_root=repo,
            ) == ["qualification_runtime_compatibility_changed"]
        finally:
            path.write_bytes(original)
    assert qualification_runtime_compatibility_errors(bundle, repo_root=repo) == []


def test_preparation_snapshot_records_normalized_git_scope_and_ancestry_results(
    tmp_path: Path,
) -> None:
    inputs = _bundle_inputs(tmp_path)
    repo = inputs["repo_root"]
    ledger = inputs["atom_actions_path"]
    assert isinstance(repo, Path)
    assert isinstance(ledger, Path)
    revision = _git(repo, "rev-parse", "HEAD")
    branch = _git(repo, "branch", "--show-current")
    _git(repo, "remote", "add", "origin", "git@Example.TEST:Org/Repo.git")
    ledger.write_text(
        yaml.safe_dump(
            {
                "atom:observed": {
                    "plan_outcomes": {
                        "revision:one": {
                            "outcome_record": {
                                "merged_commit": revision,
                                "target_branch": branch,
                                "ticket_provenance": {
                                    "verified_implementation_head": revision,
                                },
                            }
                        }
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    snapshot = capture_qualification_preparation_snapshot(
        repo_root=repo,
        repo_input=repo,
        research_ref=revision,
        source_runs_dir=inputs["source_runs_dir"],  # type: ignore[arg-type]
        atom_actions_path=ledger,
        case_registry_seed_path=inputs["case_registry_seed_path"],  # type: ignore[arg-type]
    )
    source = snapshot["source_inputs"]
    fact = next(
        item for item in source["owner_git_facts"] if item["root"] == str(repo.resolve())
    )
    assert fact["remote_urls"] == ["example.test/org/repo"]
    assert fact["head"] == {"returncode": 0, "stdout": revision}
    query = fact["outcome_queries"][0]
    assert query["merged_commit_resolution"] == {
        "returncode": 0,
        "stdout": revision,
    }
    assert query["verified_head_is_ancestor_of_merged"]["returncode"] == 0
    assert query["merged_is_ancestor_of_target"]["returncode"] == 0

def test_runtime_first_party_import_must_be_an_exact_sealed_file(tmp_path: Path) -> None:
    inputs = _bundle_inputs(tmp_path)
    repo = inputs["repo_root"]
    assert isinstance(repo, Path)
    bundle = _build_bundle(inputs)
    manifest = bundle["pipeline"]["files"]
    sealed_module = repo / "packages" / "backlog_core" / "src" / "backlog_core" / "__init__.py"
    outside_module = tmp_path / "installed" / "backlog_core" / "case_lineage.py"
    outside_module.parent.mkdir(parents=True)
    outside_module.write_text("VALUE = 'stale install'\n", encoding="utf-8")

    assert first_party_module_binding_errors(
        modules={"backlog_core": SimpleNamespace(__file__=str(sealed_module))},
        repo_root=repo,
        pipeline_manifest=manifest,
    ) == []
    assert first_party_module_binding_errors(
        modules={
            "backlog_core": SimpleNamespace(__file__=str(sealed_module)),
            "backlog_core.case_lineage": SimpleNamespace(__file__=str(outside_module)),
        },
        repo_root=repo,
        pipeline_manifest=manifest,
    ) == [
        f"first_party_module_not_sealed:backlog_core.case_lineage:{outside_module.resolve()}"
    ]


def test_phase_one_cli_rejects_loaded_first_party_module_absent_from_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    sealed_module = repo_root / "packages" / "backlog_core" / "src" / "backlog_core" / "__init__.py"
    outside_module = tmp_path / "stale-install" / "backlog_core" / "case_lineage.py"
    outside_module.parent.mkdir(parents=True)
    outside_module.write_text("VALUE = 'stale install'\n", encoding="utf-8")
    bundle = {
        "pipeline": {
            "files": {
                "repo_root": str(repo_root.resolve()),
                "files": [
                    {
                        "path": sealed_module.relative_to(repo_root).as_posix(),
                        "sha256": sha256(sealed_module.read_bytes()).hexdigest(),
                        "size_bytes": sealed_module.stat().st_size,
                    }
                ],
            }
        }
    }
    monkeypatch.setattr(
        staged_module,
        "load_qualification_input_bundle",
        lambda *_args, **_kwargs: bundle,
    )
    monkeypatch.setitem(
        sys.modules,
        "backlog_core.unsealed_external",
        SimpleNamespace(__file__=str(outside_module)),
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--shadow",
                "--qualification-input-bundle",
                str(tmp_path / "sealed-input.json"),
                "--qualification-cycle-root",
                str(tmp_path / "cycle"),
                "--shadow-state",
                str(tmp_path / "state.json"),
                "--qualification-manifest-sha256",
                "a" * 64,
            ]
        )

    assert exc.value.code == 2
    assert "Qualification runtime imports are absent from the sealed pipeline manifest" in (
        capsys.readouterr().err
    )


def test_bundle_protects_plan_trees_for_every_ledger_owner_root(
    tmp_path: Path,
) -> None:
    inputs = _bundle_inputs(tmp_path)
    owner, _revision = _repo(tmp_path, "other-owner")
    owner_idea = owner / ".agents" / "plans" / "1 - ideas" / "ordinary.md"
    owner_idea.parent.mkdir(parents=True)
    owner_idea.write_text("No magic marker is required.\n", encoding="utf-8")
    ledger = inputs["atom_actions_path"]
    assert isinstance(ledger, Path)
    ledger.write_text(
        "atom:observed:\n  status: new\n  queue_owner_roots:\n"
        f"    - '{owner.as_posix()}'\n",
        encoding="utf-8",
    )
    bundle = _build_bundle(inputs, owner_roots=[owner])
    assert str(owner.resolve()) in bundle["source_inputs"]["owner_roots"]

    owner_idea.write_text("Mutated by a model.\n", encoding="utf-8")
    assert any(
        error.startswith("qualification_input_tree_changed:protected:")
        for error in qualification_input_bundle_errors(bundle, verify_files=True)
    )


def test_cycle_namespace_resumes_only_the_same_contract_and_rejects_foreign_paths(
    tmp_path: Path,
) -> None:
    cycle_root = tmp_path / "external-cycles" / "cycle-1"
    stage_root = tmp_path / "external-stages" / "cycle-1"
    bundle_path = _write_json(tmp_path / "bundle.json", {"content_sha256": "a" * 64})
    contract = _qualification_cycle_contract(
        bundle_path=bundle_path,
        bundle_sha256="a" * 64,
        manifest_sha256="b" * 64,
        cycle_root=cycle_root,
        source_runs_dir=tmp_path / "frozen" / "usertest",
        stage_runs_dir=stage_root,
        out_json=cycle_root / "target.backlog.json",
        out_md=cycle_root / "target.backlog.md",
        state_path=tmp_path / "custody" / "shared_state.json",
        repo_root=tmp_path / "repo",
        repo_input=str(tmp_path / "repo"),
        target="target",
        research_ref="c" * 40,
        breadth_profile="standard",
        execution_profile={"agent": "codex", "model": None},
        owned_names=("target.backlog", "target.backlog.json", "target.backlog.md"),
    )
    _prepare_or_validate_qualification_cycle_namespace(
        contract=contract,
        resume=True,
        score=False,
    )
    _write_json(stage_root / "completed" / "report.json", {"complete": True})
    _prepare_or_validate_qualification_cycle_namespace(
        contract=contract,
        resume=True,
        score=False,
    )
    assert (stage_root / "completed" / "report.json").is_file()

    different = dict(contract)
    different["qualification_input_bundle_sha256"] = "d" * 64
    different["content_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="cycle_identity_changed"):
        _prepare_or_validate_qualification_cycle_namespace(
            contract=different,
            resume=True,
            score=False,
        )

    different_profile = deepcopy(contract)
    different_profile["execution_profile"] = {
        "agent": "codex",
        "model": "different-model",
    }
    different_profile["content_sha256"] = staged_module._qualification_canonical_sha256(
        {
            key: value
            for key, value in different_profile.items()
            if key != "content_sha256"
        }
    )
    with pytest.raises(ValueError, match="cycle_identity_changed"):
        _prepare_or_validate_qualification_cycle_namespace(
            contract=different_profile,
            resume=True,
            score=False,
        )

    stale_cycle = tmp_path / "external-cycles" / "stale"
    _write_json(stale_cycle / "backlog_artifacts" / "old.json", {"stale": True})
    stale = {
        **contract,
        "cycle_root": str(stale_cycle.resolve()),
        "stage_runs_dir": str((tmp_path / "external-stages" / "stale").resolve()),
    }
    stale["content_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="foreign_preexisting_path"):
        _prepare_or_validate_qualification_cycle_namespace(
            contract=stale,
            resume=True,
            score=False,
        )


def test_force_cannot_replace_a_sealed_qualification_cycle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--shadow",
                "--qualification-input-bundle",
                str(tmp_path / "bundle.json"),
                "--qualification-cycle-root",
                str(tmp_path / "cycle"),
                "--shadow-state",
                str(tmp_path / "state.json"),
                "--qualification-manifest-sha256",
                "a" * 64,
                "--force",
            ]
        )

    assert exc.value.code == 2
    assert "--force cannot replace" in capsys.readouterr().err


def test_adjudication_template_and_finalize_are_retry_deterministic(
    tmp_path: Path,
) -> None:
    atom = {
        "atom_id": "atom:one",
        "source": "run_failure",
        "evidence_role": "observation",
        "evidence_class": "observed_failure",
        "text": "Observed failure.",
    }
    manifest = build_qualification_corpus_manifest(
        atoms=[atom],
        atom_labels=[
            {
                "label_id": "label:none",
                "classification": "non_actionable",
                "atom_ids": ["atom:one"],
                "rationale": "The retained event is expected noise.",
            }
        ],
        adjudicator="independent-reviewer",
        method="retained evidence review",
    )
    manifest_path = _write_json(tmp_path / "custody" / "manifest.json", manifest)
    atoms_path = _write_json(tmp_path / "cycle" / "atoms.json", [atom])
    stage_paths = {
        stage: _write_json(tmp_path / "cycle" / f"{stage}.json", {"items": []})
        for stage in (
            "stage1",
            "stage2",
            "stage3",
            "stage4",
            "stage5",
            "stage6",
            "problem_mining_evidence",
            "case_registry",
        )
    }
    backlog_path = tmp_path / "cycle" / "target.backlog.json"
    pending_path = tmp_path / "cycle" / "target.pending.json"
    backlog = {
        "tickets": [],
        "artifacts": {
            "atoms_jsonl": str(atoms_path),
            "six_stage_pipeline": {
                "problem_records_json": str(stage_paths["stage1"]),
                "prioritized_problems_json": str(stage_paths["stage2"]),
                "research_json": str(stage_paths["stage3"]),
                "solution_options_json": str(stage_paths["stage4"]),
                "solution_selection_json": str(stage_paths["stage5"]),
                "change_plans_json": str(stage_paths["stage6"]),
            },
            "shadow_qualification": {
                "pending_run_receipt_path": str(pending_path),
            },
        },
    }
    _write_json(backlog_path, backlog)
    artifact_paths = {
        "atoms": atoms_path,
        "problem_records": stage_paths["stage1"],
        "problem_mining_evidence": stage_paths["problem_mining_evidence"],
        "prioritized_problems": stage_paths["stage2"],
        "research": stage_paths["stage3"],
        "solution_options": stage_paths["stage4"],
        "solution_selection": stage_paths["stage5"],
        "change_plans": stage_paths["stage6"],
        "case_registry": stage_paths["case_registry"],
    }
    write_pending_shadow_run(
        pending_path=pending_path,
        backlog_path=backlog_path,
        artifact_paths=artifact_paths,
        qualification_manifest_sha256_expected=sha256(
            manifest_path.read_bytes()
        ).hexdigest(),
        output_adjudication_sha256_pre_run=None,
        generated_at="2026-07-11T12:00:00Z",
    )
    first = build_qualification_adjudication_template(
        backlog_path=backlog_path,
        manifest_path=manifest_path,
    )
    second = build_qualification_adjudication_template(
        backlog_path=backlog_path,
        manifest_path=manifest_path,
    )
    assert first == second
    assert first["content_sha256"] == second["content_sha256"]

    decisions = {"output_adjudications": [], "false_rejections": []}
    finalized_first = finalize_qualification_adjudication(
        template=first,
        decisions=decisions,
        adjudicator="independent-reviewer",
        method="post-run review",
    )
    finalized_second = finalize_qualification_adjudication(
        template=second,
        decisions=decisions,
        adjudicator="independent-reviewer",
        method="post-run review",
    )
    assert finalized_first == finalized_second


def test_materialized_repair_is_independently_readjudicated_and_records_final_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reuse the repository's fully productive semantic fixture, but keep the
    # transaction/materializer/template/finalizer/scorer/state path real.
    from test_positive_depth_acceptance import (
        _option as _productive_option,
    )
    from test_positive_depth_acceptance import (
        _plan as _productive_plan,
    )
    from test_positive_depth_acceptance import (
        _selection as _productive_selection,
    )
    from test_positive_depth_acceptance import (
        _source_problem_record,
        _verified_research_proof,
    )
    from test_shadow_validation import (
        _accept_productive_fixture_contracts,
        _mixed_productive_inputs,
        _target_contract,
    )
    repo_root = find_repo_root(Path(__file__).resolve())
    fixture = _mixed_productive_inputs(tmp_path / "semantic-fixture")
    _accept_productive_fixture_contracts(monkeypatch)
    productive_research = _verified_research_proof(
        case_id="case:positive",
        problem_id="problem:positive",
        atom_id="atom:positive",
        symbol="core.run",
        path="src/core.py",
    )
    attempted_research = deepcopy(productive_research)
    stage_research = deepcopy(productive_research)
    stage_research["research_attempts"] = [
        {
            "attempt_number": 1,
            "outcome": "output_contract_valid",
            "attempted_dossier": attempted_research,
            "attempted_dossier_sha256": shadow_module._canonical_hash(
                attempted_research
            ),
        }
    ]
    productive_option = _productive_option(productive_research)
    productive_selection = _productive_selection(
        productive_research,
        productive_option,
    )
    productive_plan = _productive_plan(
        productive_research,
        productive_option,
        productive_selection,
    )
    productive_plan["target_contract"] = _target_contract(productive_plan)
    productive_plan = assign_plan_revision_id(productive_plan)
    productive_problem = _source_problem_record(
        productive_research,
        title="Correct the verified core failure boundary",
        problem="The verified core boundary produces the wrong result.",
        user_impact="Automated work cannot complete with the intended result.",
    )
    productive_priority = {
        "case_id": "case:positive",
        "problem_id": "problem:positive",
        "priority_bucket": "p1",
        "selected_for_research": True,
        "priority_rationale": "The observed failure is actionable.",
        "priority_status": "prioritized",
    }
    productive_ticket = {
        "case_id": "case:positive",
        "problem_id": "problem:positive",
        "plan_revision_id": productive_plan["plan_revision_id"],
        "stage": "ready_for_ticket",
        "severity": "high",
        "evidence_atom_ids": ["atom:positive"],
        "problem_record": productive_problem,
        "priority": productive_priority,
        "research": productive_research,
        "solution_options": [productive_option],
        "selected_solution": productive_selection,
        "change_plan": productive_plan,
    }
    fixture["stage1"]["items"][-1] = productive_problem
    fixture["stage2"]["items"][-1] = productive_priority
    fixture["stage3"]["items"][-1] = stage_research
    fixture["stage4"]["items"][-1] = productive_option
    fixture["stage5"]["items"][-1] = productive_selection
    fixture["stage6"]["items"][-1] = productive_plan
    fixture["backlog"]["tickets"][-1] = productive_ticket
    target_repo, research_ref = _repo(tmp_path, "target-repo")
    frozen_runs = tmp_path / "frozen" / "usertest"
    _write_json(frozen_runs / "source" / "report.json", {"status": "failed"})
    _write_json(
        frozen_runs.parent / "usertest_implement" / "source" / "report.json",
        {"status": "failed"},
    )
    ledger = tmp_path / "custody" / "atom_actions.yaml"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("{}\n", encoding="utf-8")
    registry_seed = _write_json(
        tmp_path / "custody" / "case_registry_seed.json",
        {"cases": {}},
    )
    bundle = build_qualification_input_bundle(
        atoms=fixture["atoms"],  # type: ignore[arg-type]
        repo_root=repo_root,
        repo_input=target_repo,
        research_ref=research_ref,
        source_runs_dir=frozen_runs,
        atom_actions_path=ledger,
        case_registry_seed_path=registry_seed,
        target="target",
        breadth_profile="standard",
        owner_roots=[repo_root, target_repo],
    )
    bundle_path = write_qualification_input_bundle(
        bundle,
        output_root=tmp_path / "custody" / "bundles",
    )

    cycle_root = tmp_path / "cycles" / "cycle-1"
    stage_root = tmp_path / "stage-runs" / "cycle-1"
    shared_state = tmp_path / "custody" / "shared_state.json"
    source_backlog_path = cycle_root / "target.backlog.json"
    source_backlog_md = cycle_root / "target.backlog.md"
    manifest_path = _write_json(
        tmp_path / "custody" / "manifest.json",
        fixture["qualification_manifest"],
    )
    source_adjudication = deepcopy(fixture["qualification_output_adjudication"])
    source_ticket_finding = next(
        item
        for item in source_adjudication["output_adjudications"]
        if item["output_kind"] == "ticket"
    )
    source_ticket_finding.update(
        {
            "quality": "bad",
            "repair_status": "not_repaired",
            "bad_severity": "noncritical",
            "bad_categories": ["limited_causal_coverage"],
            "rationale": "The original ticket did not fully address the verified cause.",
        }
    )
    source_adjudication["content_sha256"] = shadow_module._canonical_hash(
        {
            key: item
            for key, item in source_adjudication.items()
            if key != "content_sha256"
        }
    )
    source_routes = qualification_module._qualification_correction_routes(
        [source_ticket_finding],
        output_author_provenance=None,
    )
    source_adjudication_path = _write_json(
        tmp_path / "custody" / "source_adjudication.json",
        source_adjudication,
    )
    manifest_sha256 = sha256(manifest_path.read_bytes()).hexdigest()
    cycle_contract = _qualification_cycle_contract(
        bundle_path=bundle_path,
        bundle_sha256=str(bundle["content_sha256"]),
        manifest_sha256=manifest_sha256,
        cycle_root=cycle_root,
        source_runs_dir=frozen_runs,
        stage_runs_dir=stage_root,
        out_json=source_backlog_path,
        out_md=source_backlog_md,
        state_path=shared_state,
        repo_root=repo_root,
        repo_input=str(target_repo),
        target="target",
        research_ref=research_ref,
        breadth_profile="standard",
        execution_profile={"agent": "codex", "model": None},
        owned_names=(
            source_backlog_path.name,
            source_backlog_md.name,
            source_backlog_path.stem,
            "target.backlog_artifacts",
            "target.case_registry.json",
        ),
    )
    _prepare_or_validate_qualification_cycle_namespace(
        contract=cycle_contract,
        resume=True,
        score=False,
    )
    parent_contract_path = cycle_root / ".qualification_transaction.json"

    stage_keys = {
        "problem_mining": "stage1",
        "problem_prioritization": "stage2",
        "repro_research": "stage3",
        "solution_optioning": "stage4",
        "solution_selection": "stage5",
        "implementation_planning": "stage6",
    }
    stage_documents = {
        stage: deepcopy(fixture[key])
        for stage, key in stage_keys.items()
    }
    stage_paths: dict[str, Path] = {}
    stage_receipts: list[dict[str, object]] = []
    for stage, document in stage_documents.items():
        path = _write_json(stage_root / "outputs" / f"{stage}.json", document)
        stage_paths[stage] = path
        stage_receipts.append(
            {
                "stage": stage,
                "path": str(path.resolve()),
                "sha256": sha256(path.read_bytes()).hexdigest(),
                "content_sha256": staged_module._qualification_canonical_sha256(
                    document
                ),
            }
        )
    evidence_path = _write_json(
        stage_root / "outputs" / "problem_mining_evidence.json",
        {"items": []},
    )
    case_registry_path = _write_json(
        stage_root / "outputs" / "case_registry.json",
        fixture["case_registry"],
    )
    for stage, path in (
        ("problem_mining_evidence", evidence_path),
        ("case_registry", case_registry_path),
    ):
        stage_receipts.append(
            {
                "stage": stage,
                "path": str(path.resolve()),
                "sha256": sha256(path.read_bytes()).hexdigest(),
                "content_sha256": staged_module._qualification_canonical_sha256(
                    json.loads(path.read_text(encoding="utf-8"))
                ),
            }
        )
    atoms_path = _write_json(cycle_root / "atoms.json", fixture["atoms"])
    raw_report_body = {
        "schema_version": 1,
        "contract_kind": "qualification_raw_first_pass_report",
        "pending_run_sha256": "8" * 64,
        "report": {
            "passed": False,
            "qualification": {"correction_routes": source_routes},
        },
    }
    raw_report = _write_json(
        cycle_root / "raw_first_pass.json",
        {
            **raw_report_body,
            "content_sha256": staged_module._qualification_canonical_sha256(
                raw_report_body
            ),
        },
    )
    policy_path = repo_root / "configs" / "backlog_policy.yaml"
    policy_root = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    policy = BacklogPolicyConfig.from_dict(policy_root["backlog_policy"])
    source_backlog = deepcopy(fixture["backlog"])
    source_backlog.update(
        {
            "input": {"breadth_profile": "standard"},
            "scope": {"target": "target", "repo_input": str(target_repo)},
            "artifacts": {
                "atoms_jsonl": str(atoms_path),
                "case_registry_json": str(case_registry_path),
                "prompts_dir": str(repo_root / "configs" / "backlog_prompts"),
                "six_stage_pipeline": {
                    "problem_records_json": str(stage_paths["problem_mining"]),
                    "problem_mining_evidence_json": str(evidence_path),
                    "prioritized_problems_json": str(
                        stage_paths["problem_prioritization"]
                    ),
                    "research_json": str(stage_paths["repro_research"]),
                    "solution_options_json": str(stage_paths["solution_optioning"]),
                    "solution_selection_json": str(
                        stage_paths["solution_selection"]
                    ),
                    "change_plans_json": str(
                        stage_paths["implementation_planning"]
                    ),
                    "case_registry_json": str(case_registry_path),
                },
                "export_contract": {
                    "policy_config_path": str(policy_path),
                    "shadow_state_path": str(shared_state),
                },
                "shadow_qualification": {
                    "pending_adjudication": False,
                    "qualification_corpus_manifest_path": str(manifest_path),
                    "qualification_manifest_sha256_expected": manifest_sha256,
                    "qualification_manifest_sha256_observed": manifest_sha256,
                    "qualification_output_adjudication_path": str(
                        source_adjudication_path
                    ),
                    "qualification_input_bundle_path": str(bundle_path),
                    "qualification_input_bundle_sha256": bundle["content_sha256"],
                    "qualification_cycle_contract_path": str(parent_contract_path),
                    "qualification_cycle_contract_sha256": cycle_contract[
                        "content_sha256"
                    ],
                    "shadow_state_path": str(shared_state),
                    "raw_first_pass_report_path": str(raw_report),
                    "raw_first_pass_report_sha256": sha256(
                        raw_report.read_bytes()
                    ).hexdigest(),
                    "qualification_passed": False,
                    "model_readable_roots": [str(repo_root), str(target_repo)],
                },
            },
        }
    )
    _write_json(source_backlog_path, source_backlog)

    downstream_result = {
        "affected_problem_ids": ["problem:positive"],
        "requested_downstream_stages": list(stage_keys),
        "materialized_stage_receipts": stage_receipts,
    }
    route_sha256 = source_routes[0]["route_sha256"]
    route_receipts = [
        {
            "route_sha256": route_sha256,
            "status": "corrected",
            "attempts": [{"attempt": 1}],
            "assessments": [{"status": "improved"}],
        }
    ]
    consumption_body = {
        "schema_version": 1,
        "contract_kind": "qualification_correction_consumption",
        "source_pending_run_sha256": "8" * 64,
        "source_adjudication_sha256": sha256(
            source_adjudication_path.read_bytes()
        ).hexdigest(),
        "route_set_sha256": staged_module._qualification_canonical_sha256(
            [route_sha256]
        ),
        "route_receipts": route_receipts,
        "accepted_repair_count": 1,
        "accepted_repair_group_count": 1,
        "unresolved_route_count": 0,
        "pending_not_invoked_route_count": 0,
        "rerun_downstream_stages": list(stage_keys),
        "downstream_result": downstream_result,
        "downstream_result_sha256": staged_module._qualification_canonical_sha256(
            downstream_result
        ),
        "same_corpus_feedback_exposed": True,
        "release_qualification_eligible": False,
        "fresh_independent_readjudication_required": True,
    }
    consumption = {
        **consumption_body,
        "content_sha256": staged_module._qualification_canonical_sha256(
            consumption_body
        ),
    }
    runtime = QualificationRepairRuntimeResult(
        consumption=consumption,
        stage_documents=stage_documents,
        tickets=deepcopy(fixture["backlog"]["tickets"]),
        affected_problem_ids=["problem:positive"],
        atoms=deepcopy(fixture["atoms"]),
        case_registry=deepcopy(fixture["case_registry"]),
    )
    result = materialize_repaired_shadow_run(
        source_backlog=source_backlog,
        source_backlog_path=source_backlog_path,
        atoms=deepcopy(fixture["atoms"]),
        runtime=runtime,
        repo_root=repo_root,
        repo_input=str(target_repo),
        policy_config=policy,
        policy_config_path=policy_path,
        export_gate_config_path=repo_root / "configs" / "backlog_export_gate.yaml",
        qualification_manifest_path=manifest_path,
        qualification_manifest_sha256=manifest_sha256,
        qualification_output_adjudication_path=source_adjudication_path,
        qualification_output_adjudication_sha256=sha256(
            source_adjudication_path.read_bytes()
        ).hexdigest(),
    )
    assert result is not None
    repaired_backlog_path = Path(result["repaired_backlog_path"])
    repaired_backlog = json.loads(repaired_backlog_path.read_text(encoding="utf-8"))
    repaired_qualification = repaired_backlog["artifacts"]["shadow_qualification"]
    repaired_manifest_path = Path(
        repaired_qualification["qualification_corpus_manifest_path"]
    )
    template = build_qualification_adjudication_template(
        backlog_path=repaired_backlog_path,
        manifest_path=repaired_manifest_path,
    )
    assert len(template["source_correction_findings"]) == 1
    source_finding_id = template["source_correction_findings"][0]["finding_id"]
    decisions = {
        "output_adjudications": [
            {
                "output_kind": output_kind,
                "output_sha256": shadow_module._canonical_hash(output),
                "quality": "good",
                "repair_status": "repaired",
                "actionable_label_ids": ["label:positive"],
                "rationale": "Fresh independent review verified the corrected output.",
            }
            for output_kind, outputs in template["accepted_outputs_by_kind"].items()
            for output in outputs
        ],
        "false_rejections": [],
        "source_correction_resolutions": [
            {
                "finding_id": source_finding_id,
                "status": "resolved",
                "rationale": (
                    "Fresh review confirmed the repaired ticket now covers the verified cause."
                ),
                "repaired_output_refs": [
                    {
                        "output_kind": "ticket",
                        "output_sha256": shadow_module._canonical_hash(
                            template["accepted_outputs_by_kind"]["ticket"][-1]
                        ),
                    }
                ],
            }
        ],
    }
    final_adjudication = finalize_qualification_adjudication(
        template=template,
        decisions=decisions,
        adjudicator="fresh-independent-reviewer",
        method="post-repair retained evidence review",
    )
    repaired_adjudication_path = _write_json(
        Path(repaired_qualification["qualification_output_adjudication_path"]),
        final_adjudication,
    )
    gate = shadow_module.normalize_shadow_gate_config(
        {"required_consecutive_shadow_cycles": 2}
    )
    # Exercise the public repaired-child score path. The child contract already
    # binds and verifies its parent bundle/cycle, so operators must not resupply
    # parent --qualification-input-bundle, --qualification-cycle-root,
    # --stage-runs-dir, or --shadow-state arguments.
    score_argv = [
        "reports",
        "backlog",
        "--repo-root",
        str(repo_root),
        "--runs-dir",
        str(frozen_runs),
        "--target",
        "target",
        "--repo-input",
        str(target_repo),
        "--research-ref",
        research_ref,
        "--out-json",
        str(repaired_backlog_path),
        "--out-md",
        str(repaired_backlog_path.with_suffix(".md")),
        "--shadow",
        "--score-shadow",
        "--qualification-corpus-manifest",
        str(repaired_manifest_path),
        "--qualification-output-adjudication",
        str(repaired_adjudication_path),
    ]
    stale_module = tmp_path / "stale-install" / "backlog_core" / "outside.py"
    stale_module.parent.mkdir(parents=True)
    stale_module.write_text("VALUE = 'stale'\n", encoding="utf-8")
    monkeypatch.setitem(
        sys.modules,
        "backlog_core.repaired_child_stale_install",
        SimpleNamespace(__file__=str(stale_module)),
    )
    with pytest.raises(SystemExit) as stale_score_exit:
        main(score_argv)
    assert stale_score_exit.value.code == 2
    assert not shared_state.exists()
    monkeypatch.delitem(
        sys.modules,
        "backlog_core.repaired_child_stale_install",
    )

    with pytest.raises(SystemExit) as score_exit:
        main(score_argv)
    score_result = score_exit.value.code

    assert score_result == 0
    state = json.loads(shared_state.read_text(encoding="utf-8"))
    assert state["consecutive_stable_passes"] == 1
    assert state["ready_for_export"] is False
    final_cycle = state["cycles"][-1]
    assert final_cycle["cycle_mode"] == "release"
    assert final_cycle["qualification"]["status"] == "verified"
    assert final_cycle["qualification"]["clean_first_pass"] is False
    assert final_cycle["qualification"]["correction_required"] is True
    assert final_cycle["qualification"]["independent_release_evidence"] is False
    assert final_cycle["qualification"]["useful_output_verified"] is True

    second_cycle_root = tmp_path / "cycles" / "cycle-2"
    second_stage_root = tmp_path / "stage-runs" / "cycle-2"
    second_source_backlog_path = second_cycle_root / "target.backlog.json"
    second_source_backlog_md = second_cycle_root / "target.backlog.md"
    second_cycle_contract = _qualification_cycle_contract(
        bundle_path=bundle_path,
        bundle_sha256=str(bundle["content_sha256"]),
        manifest_sha256=manifest_sha256,
        cycle_root=second_cycle_root,
        source_runs_dir=frozen_runs,
        stage_runs_dir=second_stage_root,
        out_json=second_source_backlog_path,
        out_md=second_source_backlog_md,
        state_path=shared_state,
        repo_root=repo_root,
        repo_input=str(target_repo),
        target="target",
        research_ref=research_ref,
        breadth_profile="standard",
        execution_profile={"agent": "codex", "model": None},
        owned_names=(
            second_source_backlog_path.name,
            second_source_backlog_md.name,
            second_source_backlog_path.stem,
            "target.backlog_artifacts",
            "target.case_registry.json",
        ),
    )
    _prepare_or_validate_qualification_cycle_namespace(
        contract=second_cycle_contract,
        resume=True,
        score=False,
    )
    second_parent_contract_path = (
        second_cycle_root / ".qualification_transaction.json"
    )
    second_stage_paths: dict[str, Path] = {}
    second_stage_receipts: list[dict[str, object]] = []
    for stage, document in stage_documents.items():
        path = _write_json(
            second_stage_root / "outputs" / f"{stage}.json",
            document,
        )
        second_stage_paths[stage] = path
        second_stage_receipts.append(
            {
                "stage": stage,
                "path": str(path.resolve()),
                "sha256": sha256(path.read_bytes()).hexdigest(),
                "content_sha256": staged_module._qualification_canonical_sha256(
                    document
                ),
            }
        )
    second_evidence_path = _write_json(
        second_stage_root / "outputs" / "problem_mining_evidence.json",
        {"items": []},
    )
    second_case_registry_path = _write_json(
        second_stage_root / "outputs" / "case_registry.json",
        fixture["case_registry"],
    )
    for stage, path in (
        ("problem_mining_evidence", second_evidence_path),
        ("case_registry", second_case_registry_path),
    ):
        second_stage_receipts.append(
            {
                "stage": stage,
                "path": str(path.resolve()),
                "sha256": sha256(path.read_bytes()).hexdigest(),
                "content_sha256": staged_module._qualification_canonical_sha256(
                    json.loads(path.read_text(encoding="utf-8"))
                ),
            }
        )
    second_atoms_path = _write_json(
        second_cycle_root / "atoms.json",
        fixture["atoms"],
    )
    second_raw_report = _write_json(
        second_cycle_root / "raw_first_pass.json",
        {
            **raw_report_body,
            "content_sha256": staged_module._qualification_canonical_sha256(
                raw_report_body
            ),
        },
    )
    second_source_backlog = deepcopy(fixture["backlog"])
    second_source_backlog.update(
        {
            "input": {"breadth_profile": "standard"},
            "scope": {"target": "target", "repo_input": str(target_repo)},
            "artifacts": {
                "atoms_jsonl": str(second_atoms_path),
                "case_registry_json": str(second_case_registry_path),
                "prompts_dir": str(repo_root / "configs" / "backlog_prompts"),
                "six_stage_pipeline": {
                    "problem_records_json": str(
                        second_stage_paths["problem_mining"]
                    ),
                    "problem_mining_evidence_json": str(second_evidence_path),
                    "prioritized_problems_json": str(
                        second_stage_paths["problem_prioritization"]
                    ),
                    "research_json": str(second_stage_paths["repro_research"]),
                    "solution_options_json": str(
                        second_stage_paths["solution_optioning"]
                    ),
                    "solution_selection_json": str(
                        second_stage_paths["solution_selection"]
                    ),
                    "change_plans_json": str(
                        second_stage_paths["implementation_planning"]
                    ),
                    "case_registry_json": str(second_case_registry_path),
                },
                "export_contract": {
                    "policy_config_path": str(policy_path),
                    "shadow_state_path": str(shared_state),
                },
                "shadow_qualification": {
                    "pending_adjudication": False,
                    "qualification_corpus_manifest_path": str(manifest_path),
                    "qualification_manifest_sha256_expected": manifest_sha256,
                    "qualification_manifest_sha256_observed": manifest_sha256,
                    "qualification_output_adjudication_path": str(
                        source_adjudication_path
                    ),
                    "qualification_input_bundle_path": str(bundle_path),
                    "qualification_input_bundle_sha256": bundle[
                        "content_sha256"
                    ],
                    "qualification_cycle_contract_path": str(
                        second_parent_contract_path
                    ),
                    "qualification_cycle_contract_sha256": second_cycle_contract[
                        "content_sha256"
                    ],
                    "shadow_state_path": str(shared_state),
                    "raw_first_pass_report_path": str(second_raw_report),
                    "raw_first_pass_report_sha256": sha256(
                        second_raw_report.read_bytes()
                    ).hexdigest(),
                    "qualification_passed": False,
                    "model_readable_roots": [str(repo_root), str(target_repo)],
                },
            },
        }
    )
    _write_json(second_source_backlog_path, second_source_backlog)

    second_downstream_result = {
        "affected_problem_ids": ["problem:positive"],
        "requested_downstream_stages": list(stage_keys),
        "materialized_stage_receipts": second_stage_receipts,
    }
    second_route_sha256 = source_routes[0]["route_sha256"]
    second_route_receipts = [
        {
            "route_sha256": second_route_sha256,
            "status": "corrected",
            "attempts": [{"attempt": 1}],
            "assessments": [{"status": "improved"}],
        }
    ]
    second_consumption_body = {
        "schema_version": 1,
        "contract_kind": "qualification_correction_consumption",
        "source_pending_run_sha256": "9" * 64,
        "source_adjudication_sha256": sha256(
            source_adjudication_path.read_bytes()
        ).hexdigest(),
        "route_set_sha256": staged_module._qualification_canonical_sha256(
            [second_route_sha256]
        ),
        "route_receipts": second_route_receipts,
        "accepted_repair_count": 1,
        "accepted_repair_group_count": 1,
        "unresolved_route_count": 0,
        "pending_not_invoked_route_count": 0,
        "rerun_downstream_stages": list(stage_keys),
        "downstream_result": second_downstream_result,
        "downstream_result_sha256": (
            staged_module._qualification_canonical_sha256(
                second_downstream_result
            )
        ),
        "same_corpus_feedback_exposed": True,
        "release_qualification_eligible": False,
        "fresh_independent_readjudication_required": True,
    }
    second_consumption = {
        **second_consumption_body,
        "content_sha256": staged_module._qualification_canonical_sha256(
            second_consumption_body
        ),
    }
    second_runtime = QualificationRepairRuntimeResult(
        consumption=second_consumption,
        stage_documents=deepcopy(stage_documents),
        tickets=deepcopy(fixture["backlog"]["tickets"]),
        affected_problem_ids=["problem:positive"],
        atoms=deepcopy(fixture["atoms"]),
        case_registry=deepcopy(fixture["case_registry"]),
    )
    second_result = materialize_repaired_shadow_run(
        source_backlog=second_source_backlog,
        source_backlog_path=second_source_backlog_path,
        atoms=deepcopy(fixture["atoms"]),
        runtime=second_runtime,
        repo_root=repo_root,
        repo_input=str(target_repo),
        policy_config=policy,
        policy_config_path=policy_path,
        export_gate_config_path=repo_root / "configs" / "backlog_export_gate.yaml",
        qualification_manifest_path=manifest_path,
        qualification_manifest_sha256=manifest_sha256,
        qualification_output_adjudication_path=source_adjudication_path,
        qualification_output_adjudication_sha256=sha256(
            source_adjudication_path.read_bytes()
        ).hexdigest(),
    )
    assert second_result is not None
    second_repaired_backlog_path = Path(second_result["repaired_backlog_path"])
    second_repaired_backlog = json.loads(
        second_repaired_backlog_path.read_text(encoding="utf-8")
    )
    second_repaired_qualification = second_repaired_backlog["artifacts"][
        "shadow_qualification"
    ]
    second_repaired_manifest_path = Path(
        second_repaired_qualification["qualification_corpus_manifest_path"]
    )
    second_template = build_qualification_adjudication_template(
        backlog_path=second_repaired_backlog_path,
        manifest_path=second_repaired_manifest_path,
    )
    second_decisions = {
        "output_adjudications": [
            {
                "output_kind": output_kind,
                "output_sha256": shadow_module._canonical_hash(output),
                "quality": "good",
                "repair_status": "repaired",
                "actionable_label_ids": ["label:positive"],
                "rationale": (
                    "Fresh independent review verified the corrected output."
                ),
            }
            for output_kind, outputs in second_template[
                "accepted_outputs_by_kind"
            ].items()
            for output in outputs
        ],
        "false_rejections": [],
        "source_correction_resolutions": [
            {
                "finding_id": second_template["source_correction_findings"][0][
                    "finding_id"
                ],
                "status": "resolved",
                "rationale": (
                    "Fresh review confirmed the repaired ticket now covers the verified cause."
                ),
                "repaired_output_refs": [
                    {
                        "output_kind": "ticket",
                        "output_sha256": shadow_module._canonical_hash(
                            second_template["accepted_outputs_by_kind"]["ticket"][-1]
                        ),
                    }
                ],
            }
        ],
    }
    second_final_adjudication = finalize_qualification_adjudication(
        template=second_template,
        decisions=second_decisions,
        adjudicator="fresh-independent-reviewer-cycle-2",
        method="post-repair retained evidence review",
    )
    second_repaired_adjudication_path = _write_json(
        Path(
            second_repaired_qualification[
                "qualification_output_adjudication_path"
            ]
        ),
        second_final_adjudication,
    )
    second_score_result = staged_module._score_materialized_shadow_run(
        repo_root=repo_root,
        runs_dir=frozen_runs,
        out_json=second_repaired_backlog_path,
        out_md=second_repaired_backlog_path.with_suffix(".md"),
        repo_input=str(target_repo),
        shadow_gate_config=gate,
        qualification_manifest_path=second_repaired_manifest_path,
        qualification_output_adjudication_path=(
            second_repaired_adjudication_path
        ),
        no_actionable_evidence_receipt_path=None,
        agent="codex",
        model=None,
        cfg=type("Config", (), {"runs_dir": second_stage_root})(),
        research_config={},
        research_ref=research_ref,
        replay_timeout_seconds=10800.0,
        state_path=shared_state,
    )

    assert second_score_result == 0
    second_state = json.loads(shared_state.read_text(encoding="utf-8"))
    assert second_state["consecutive_stable_passes"] == 2
    assert second_state["ready_for_export"] is True
    assert len(second_state["cycles"]) == 2
    assert second_state["cycles"][-1]["qualification"]["status"] == "verified"
    assert (
        second_state["cycles"][-1]["qualification"]["clean_first_pass"]
        is False
    )
