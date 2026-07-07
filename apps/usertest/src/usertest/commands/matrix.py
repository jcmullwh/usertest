# ruff: noqa: E501
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from runner_core import RunnerConfig, RunRequest, run_once
from runner_core.catalog import discover_missions, discover_personas, load_catalog_config
from runner_core.pathing import slugify

from usertest.commands.batch import _prevalidate_batch_requests
from usertest.commands.shared import (
    _EXEC_CACHE_DIR_HELP,
    _EXEC_CACHE_HELP,
    _EXEC_NETWORK_HELP,
    _coerce_string,
    _load_runner_config,
    _load_yaml,
    _resolve_local_repo_root,
    _resolve_optional_path,
    _resolve_repo_root,
)


def add_matrix_command(sub: argparse._SubParsersAction) -> None:
    matrix_p = sub.add_parser(
        "matrix",
        help=(
            "Generate and (optionally) run a matrix of persona/mission x agent/model combinations."
        ),
    )
    matrix_sub = matrix_p.add_subparsers(dest="matrix_cmd", required=True)

    matrix_plan_p = matrix_sub.add_parser(
        "plan",
        help="Expand a matrix spec into batch targets and validate (no execution).",
    )
    matrix_run_p = matrix_sub.add_parser(
        "run",
        help="Validate a matrix spec then execute all combinations.",
    )

    for p in (matrix_plan_p, matrix_run_p):
        p.add_argument(
            "--repo-root",
            type=Path,
            default=Path("."),
            help="Monorepo root (auto-detected when omitted).",
        )
        p.add_argument(
            "--spec",
            type=Path,
            required=True,
            help="Path to a YAML matrix spec.",
        )
        p.add_argument(
            "--out-targets",
            type=Path,
            help=(
                "Write expanded batch targets YAML here "
                "(default: runs/usertest/<target>/_compiled/<ts>.matrix.targets.yaml)."
            ),
        )
        p.add_argument(
            "--out-report",
            type=Path,
            help=("Write a JSON validation report (capabilities + requirements per combination)."),
        )
        p.add_argument(
            "--exec-backend",
            choices=["local", "docker"],
            default="docker",
            help=(
                "Execution backend (default: docker; affects tool availability, "
                "especially for gemini shell access)."
            ),
        )
        p.add_argument("--exec-docker-context", type=Path)
        p.add_argument("--exec-dockerfile", type=Path)
        p.add_argument("--exec-docker-python", default="auto")
        p.add_argument("--exec-docker-timeout-seconds", type=float, default=None)
        p.add_argument("--exec-use-target-sandbox-cli-install", action="store_true")
        p.add_argument("--exec-use-host-agent-login", action="store_true")
        p.add_argument(
            "--exec-network",
            choices=["open", "none"],
            default="open",
            help=_EXEC_NETWORK_HELP,
        )
        p.add_argument(
            "--exec-cache",
            choices=["cold", "warm"],
            default="cold",
            help=_EXEC_CACHE_HELP,
        )
        p.add_argument("--exec-cache-dir", type=Path, help=_EXEC_CACHE_DIR_HELP)
        p.add_argument(
            "--exec-env",
            action="append",
            default=[],
            help=(
                "Extra environment variable assignment(s) for sandbox execution "
                "(repeatable KEY=VALUE)."
            ),
        )
        p.add_argument("--exec-keep-container", action="store_true")
        p.add_argument("--exec-rebuild-image", action="store_true")

        p.add_argument(
            "--skip-command-probes",
            action="store_true",
            help="Skip local command responsiveness probes (faster, less validation).",
        )
        p.add_argument(
            "--command-probe-timeout-seconds",
            type=float,
            default=0.25,
            help="Timeout for each command responsiveness probe.",
        )

    matrix_plan_p.set_defaults(func=_cmd_matrix_plan)
    matrix_run_p.set_defaults(func=_cmd_matrix_run)

def _matrix__coerce_bool(value: Any) -> bool | None:
    """Coerce matrix spec values into optional booleans."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "y", "1", "on"}:
            return True
        if lowered in {"false", "no", "n", "0", "off"}:
            return False
    return None


def _matrix__parse_mission_entries(
    raw: Any,
    *,
    spec_path: Path,
) -> tuple[list[str | None], dict[str, dict[str, bool]]]:
    """
    Returns (mission_ids, overrides_by_mission_id).

    Missions can be specified as:
      - "mission_id" (string)
      - {id: "mission_id", requires_shell: true, requires_edits: false}

    If missions is missing/empty, returns [None] meaning "use catalog default".
    """

    if raw is None:
        return ([None], {})

    if isinstance(raw, str):
        raw = [raw]

    if not isinstance(raw, list):
        raise ValueError(
            f"matrix spec missions must be a list (or string); got {type(raw).__name__} in {spec_path}"
        )

    mission_ids: list[str | None] = []
    overrides: dict[str, dict[str, bool]] = {}

    for idx, item in enumerate(raw):
        if isinstance(item, str):
            mid = item.strip()
            if not mid:
                raise ValueError(f"matrix spec missions[{idx}] is empty in {spec_path}")
            mission_ids.append(mid)
            continue

        if not isinstance(item, dict):
            raise ValueError(
                f"matrix spec missions[{idx}] must be a string or mapping; got {type(item).__name__} in {spec_path}"
            )

        mid = _coerce_string(item.get("id")) or _coerce_string(item.get("mission_id"))
        if mid is None:
            raise ValueError(f"matrix spec missions[{idx}] missing id in {spec_path}")
        mission_ids.append(mid)

        rs = _matrix__coerce_bool(item.get("requires_shell"))
        re_ = _matrix__coerce_bool(item.get("requires_edits"))
        if rs is not None or re_ is not None:
            overrides[mid] = {
                **({"requires_shell": rs} if rs is not None else {}),
                **({"requires_edits": re_} if re_ is not None else {}),
            }

    if not mission_ids:
        mission_ids = [None]

    return (mission_ids, overrides)


def _matrix__parse_persona_ids(raw: Any, *, spec_path: Path) -> list[str | None]:
    """Parse and validate persona identifiers from matrix spec input."""
    if raw is None:
        return [None]

    if isinstance(raw, str):
        raw = [raw]

    if not isinstance(raw, list):
        raise ValueError(
            f"matrix spec personas must be a list (or string); got {type(raw).__name__} in {spec_path}"
        )

    persona_ids: list[str | None] = []
    for idx, item in enumerate(raw):
        if item is None:
            persona_ids.append(None)
            continue
        if not isinstance(item, str):
            raise ValueError(
                f"matrix spec personas[{idx}] must be a string (or null for default); got {type(item).__name__} in {spec_path}"
            )
        pid = item.strip()
        if not pid:
            raise ValueError(f"matrix spec personas[{idx}] is empty in {spec_path}")
        persona_ids.append(pid)

    return persona_ids or [None]


def _matrix__parse_seeds(raw: Any, *, spec_path: Path) -> list[int]:
    """Parse and validate seed values from matrix spec input."""
    if raw is None:
        return [0]

    if isinstance(raw, int):
        return [int(raw)]

    if isinstance(raw, str) and raw.strip().isdigit():
        return [int(raw.strip())]

    if not isinstance(raw, list):
        raise ValueError(
            f"matrix spec seeds must be a list (or int); got {type(raw).__name__} in {spec_path}"
        )

    seeds: list[int] = []
    for idx, item in enumerate(raw):
        if isinstance(item, bool):
            raise ValueError(f"matrix spec seeds[{idx}] must be int; got bool in {spec_path}")
        if isinstance(item, int):
            seeds.append(int(item))
            continue
        if isinstance(item, str) and item.strip().isdigit():
            seeds.append(int(item.strip()))
            continue
        raise ValueError(
            f"matrix spec seeds[{idx}] must be int; got {item!r} ({type(item).__name__}) in {spec_path}"
        )

    return seeds or [0]


def _matrix__parse_agent_entries(raw: Any, *, spec_path: Path) -> list[dict[str, Any]]:
    """Parse the providers/models axis.

    Agents can be specified as:
      - "codex"
      - {agent: "codex", models: ["gpt-5.5"], policy: "inspect", agent_config: ["k=v"]}

    Returns a list of normalized dicts with keys: agent, models, policy, agent_config.
    """

    if raw is None:
        return [{"agent": "codex", "models": [None], "policy": "auto", "agent_config": []}]

    if isinstance(raw, str):
        raw = [raw]

    if not isinstance(raw, list):
        raise ValueError(
            f"matrix spec agents must be a list (or string); got {type(raw).__name__} in {spec_path}"
        )

    entries: list[dict[str, Any]] = []

    for idx, item in enumerate(raw):
        if isinstance(item, str):
            agent = item.strip()
            if not agent:
                raise ValueError(f"matrix spec agents[{idx}] is empty in {spec_path}")
            entries.append({"agent": agent, "models": [None], "policy": "auto", "agent_config": []})
            continue

        if not isinstance(item, dict):
            raise ValueError(
                f"matrix spec agents[{idx}] must be a string or mapping; got {type(item).__name__} in {spec_path}"
            )

        agent = _coerce_string(item.get("agent")) or _coerce_string(item.get("id"))
        if agent is None:
            raise ValueError(f"matrix spec agents[{idx}] missing agent in {spec_path}")

        policy = (_coerce_string(item.get("policy")) or "auto").strip()
        if not policy:
            policy = "auto"

        models_raw = item.get("models")
        if models_raw is None:
            models_raw = item.get("model")
        models: list[str | None] = []
        if models_raw is None:
            models = [None]
        elif isinstance(models_raw, str):
            models = [models_raw.strip()]
        elif isinstance(models_raw, list):
            for jdx, m in enumerate(models_raw):
                if m is None:
                    models.append(None)
                    continue
                if not isinstance(m, str) or not m.strip():
                    raise ValueError(
                        f"matrix spec agents[{idx}].models[{jdx}] must be a non-empty string or null; got {m!r}"
                    )
                models.append(m.strip())
        else:
            raise ValueError(
                f"matrix spec agents[{idx}].models must be a list (or string); got {type(models_raw).__name__}"
            )
        if not models:
            models = [None]

        agent_config_raw = item.get("agent_config")
        if agent_config_raw is None:
            agent_config_raw = item.get("agent_config_overrides")
        agent_config: list[str] = []
        if agent_config_raw is None:
            agent_config = []
        elif isinstance(agent_config_raw, list):
            for jdx, ov in enumerate(agent_config_raw):
                if not isinstance(ov, str) or not ov.strip():
                    raise ValueError(
                        f"matrix spec agents[{idx}].agent_config[{jdx}] must be a non-empty string; got {ov!r}"
                    )
                agent_config.append(ov.strip())
        else:
            raise ValueError(
                f"matrix spec agents[{idx}].agent_config must be a list; got {type(agent_config_raw).__name__}"
            )

        entries.append(
            {"agent": agent, "models": models, "policy": policy, "agent_config": agent_config}
        )

    if not entries:
        entries = [{"agent": "codex", "models": [None], "policy": "auto", "agent_config": []}]

    return entries


def _matrix__infer_allow_edits_and_shell_status(
    *,
    cfg: RunnerConfig,
    request: RunRequest,
) -> tuple[bool, str]:
    """Infer (allow_edits, shell_status) for request's agent/policy/backend.

    shell_status is one of: allowed | blocked | unknown
    """

    policy_cfg = cfg.policies.get(request.policy, {})
    policy_cfg = policy_cfg if isinstance(policy_cfg, dict) else {}

    codex_policy = policy_cfg.get("codex", {})
    codex_policy = codex_policy if isinstance(codex_policy, dict) else {}
    claude_policy = policy_cfg.get("claude", {})
    claude_policy = claude_policy if isinstance(claude_policy, dict) else {}
    gemini_policy = policy_cfg.get("gemini", {})
    gemini_policy = gemini_policy if isinstance(gemini_policy, dict) else {}

    allow_edits = False
    if request.agent == "codex":
        allow_edits = bool(codex_policy.get("allow_edits", False))
    elif request.agent == "claude":
        allow_edits = bool(claude_policy.get("allow_edits", False))
    elif request.agent == "gemini":
        allow_edits = bool(gemini_policy.get("allow_edits", False))

    shell_status = "unknown"
    if request.agent == "claude":
        allowed_tools = claude_policy.get("allowed_tools")
        allowed_tools = allowed_tools if isinstance(allowed_tools, list) else []
        shell_status = "allowed" if "Bash" in allowed_tools else "blocked"
    elif request.agent == "gemini":
        allowed_tools = gemini_policy.get("allowed_tools")
        allowed_tools = allowed_tools if isinstance(allowed_tools, list) else []
        shell_enabled = "run_shell_command" in allowed_tools
        has_outer_sandbox = str(request.exec_backend) == "docker"
        gemini_sandbox_enabled = (
            bool(gemini_policy.get("sandbox", True))
            if isinstance(gemini_policy.get("sandbox", True), bool)
            else True
        )
        if has_outer_sandbox:
            gemini_sandbox_enabled = False
        if os.name == "nt":
            gemini_sandbox_enabled = False
        shell_available = has_outer_sandbox or gemini_sandbox_enabled
        if shell_enabled and not shell_available:
            shell_status = "blocked"
        else:
            shell_status = "allowed" if shell_enabled else "blocked"

    return (allow_edits, shell_status)


def _matrix__choose_policy_auto(
    *,
    cfg: RunnerConfig,
    agent: str,
    exec_backend: str,
    requires_shell: bool,
    requires_edits: bool,
) -> str:
    """Choose the least-permissive policy that satisfies requirements.

    Uses the conventional ordering: safe < inspect < write.
    """

    candidates = ["safe", "inspect", "write"]
    for policy in candidates:
        if policy not in cfg.policies:
            continue
        req = RunRequest(repo=".", agent=agent, policy=policy, exec_backend=exec_backend)
        allow_edits, shell_status = _matrix__infer_allow_edits_and_shell_status(
            cfg=cfg, request=req
        )
        if requires_edits and not allow_edits:
            continue
        if requires_shell and shell_status == "blocked":
            continue
        return policy

    # Fall back to any available policy if nothing matches (will be caught by validation).
    for policy in candidates:
        if policy in cfg.policies:
            return policy
    return next(iter(cfg.policies.keys()))


def _cmd_matrix_plan(args: argparse.Namespace) -> int:
    """Execute matrix planning without launching runs."""
    return _cmd_matrix(args, execute=False)


def _cmd_matrix_run(args: argparse.Namespace) -> int:
    """Execute matrix planning and run generated targets."""
    return _cmd_matrix(args, execute=True)


def _cmd_matrix(args: argparse.Namespace, *, execute: bool) -> int:
    """Execute matrix command flow for planning or execution."""
    repo_root = _resolve_repo_root(args.repo_root)
    cfg = _load_runner_config(repo_root)

    spec_path = Path(args.spec)
    if not spec_path.is_absolute():
        spec_path = (repo_root / spec_path).resolve()

    spec_raw = _load_yaml(spec_path)
    if not isinstance(spec_raw, dict):
        raise ValueError(f"Matrix spec must be a mapping (YAML object): {spec_path}")

    repo_input = _coerce_string(spec_raw.get("repo"))
    if repo_input is None:
        raise ValueError(f"Matrix spec missing required field 'repo': {spec_path}")

    ref = _coerce_string(spec_raw.get("ref"))
    default_policy = (_coerce_string(spec_raw.get("policy")) or "auto").strip() or "auto"

    persona_ids = _matrix__parse_persona_ids(
        spec_raw.get("personas") if "personas" in spec_raw else spec_raw.get("persona_ids"),
        spec_path=spec_path,
    )

    mission_ids, mission_overrides = _matrix__parse_mission_entries(
        spec_raw.get("missions") if "missions" in spec_raw else spec_raw.get("mission_ids"),
        spec_path=spec_path,
    )

    seeds = _matrix__parse_seeds(
        spec_raw.get("seeds") if "seeds" in spec_raw else spec_raw.get("seed"), spec_path=spec_path
    )

    agent_entries = _matrix__parse_agent_entries(
        spec_raw.get("agents") if "agents" in spec_raw else spec_raw.get("providers"),
        spec_path=spec_path,
    )

    # Load catalog once (best-effort: local repo if available).
    target_repo_root = _resolve_local_repo_root(repo_root, repo_input)
    catalog_config = load_catalog_config(repo_root, target_repo_root)
    persona_by_id = discover_personas(catalog_config)
    mission_by_id = discover_missions(catalog_config)

    # Resolve defaults if caller used null persona/mission.
    resolved_persona_ids: list[str] = []
    for pid in persona_ids:
        if pid is None:
            if catalog_config.defaults_persona_id is None:
                raise ValueError("No default persona_id configured (matrix spec used null).")
            resolved_persona_ids.append(catalog_config.defaults_persona_id)
        else:
            resolved_persona_ids.append(pid)

    resolved_mission_ids: list[str] = []
    for mid in mission_ids:
        if mid is None:
            if catalog_config.defaults_mission_id is None:
                raise ValueError("No default mission_id configured (matrix spec used null).")
            resolved_mission_ids.append(catalog_config.defaults_mission_id)
        else:
            resolved_mission_ids.append(mid)

    # Expand cartesian product.
    run_targets: list[dict[str, Any]] = []
    requests: list[tuple[int, RunRequest]] = []
    validation_report: list[dict[str, Any]] = []

    exec_backend = str(getattr(args, "exec_backend", "local"))

    # Prepare execution backend args shared across requests.
    exec_docker_context = _resolve_optional_path(
        repo_root, getattr(args, "exec_docker_context", None)
    )
    exec_cache_dir = _resolve_optional_path(repo_root, getattr(args, "exec_cache_dir", None))
    if exec_cache_dir is None and str(getattr(args, "exec_cache", "cold")) == "warm":
        exec_cache_dir = repo_root / "runs" / "_cache" / "usertest"
        if not bool(getattr(args, "skip_command_probes", False)):
            print(
                f"No --exec-cache-dir provided; using default: {exec_cache_dir}",
                file=sys.stderr,
            )

    exec_docker_timeout_seconds = getattr(args, "exec_docker_timeout_seconds", None)
    if exec_docker_timeout_seconds is not None and float(exec_docker_timeout_seconds) <= 0:
        exec_docker_timeout_seconds = None

    base_exec_env = tuple(
        str(x) for x in (getattr(args, "exec_env", None) or []) if isinstance(x, str) and x.strip()
    )

    # Expand runs.
    idx_counter = 0
    for pid in resolved_persona_ids:
        for mid in resolved_mission_ids:
            mission_spec = mission_by_id.get(mid)
            if mission_spec is None:
                # We still include it so the user sees the error in validation.
                base_requires_shell = False
                base_requires_edits = False
            else:
                base_requires_shell = bool(getattr(mission_spec, "requires_shell", False))
                base_requires_edits = bool(getattr(mission_spec, "requires_edits", False))

            overrides = mission_overrides.get(mid, {})
            requires_shell = bool(overrides.get("requires_shell", base_requires_shell))
            requires_edits = bool(overrides.get("requires_edits", base_requires_edits))

            for agent_entry in agent_entries:
                agent = str(agent_entry.get("agent"))
                policy_raw = str(agent_entry.get("policy") or default_policy or "auto")
                agent_config = [
                    x for x in (agent_entry.get("agent_config") or []) if isinstance(x, str)
                ]

                for model in agent_entry.get("models") or [None]:
                    for seed in seeds:
                        policy = policy_raw
                        if policy == "auto":
                            policy = _matrix__choose_policy_auto(
                                cfg=cfg,
                                agent=agent,
                                exec_backend=exec_backend,
                                requires_shell=requires_shell,
                                requires_edits=requires_edits,
                            )

                        req = RunRequest(
                            repo=repo_input,
                            ref=ref,
                            agent=agent,
                            policy=policy,
                            persona_id=pid,
                            mission_id=mid,
                            seed=int(seed),
                            model=(
                                str(model) if isinstance(model, str) and model.strip() else None
                            ),
                            agent_config_overrides=tuple(agent_config),
                            exec_backend=exec_backend,
                            exec_docker_context=exec_docker_context,
                            exec_dockerfile=getattr(args, "exec_dockerfile", None),
                            exec_docker_python=str(getattr(args, "exec_docker_python", "auto")),
                            exec_docker_timeout_seconds=(
                                float(exec_docker_timeout_seconds)
                                if exec_docker_timeout_seconds is not None
                                else None
                            ),
                            exec_use_target_sandbox_cli_install=bool(
                                getattr(args, "exec_use_target_sandbox_cli_install", False)
                            ),
                            exec_use_host_agent_login=bool(
                                getattr(args, "exec_use_host_agent_login", False)
                            ),
                            exec_network=str(getattr(args, "exec_network", "open")),
                            exec_cache=str(getattr(args, "exec_cache", "cold")),
                            exec_cache_dir=exec_cache_dir,
                            exec_env=base_exec_env,
                            exec_keep_container=bool(getattr(args, "exec_keep_container", False)),
                            exec_rebuild_image=bool(getattr(args, "exec_rebuild_image", False)),
                        )

                        # Record plan entry.
                        run_targets.append(
                            {
                                "repo": repo_input,
                                **({"ref": ref} if ref is not None else {}),
                                "agent": agent,
                                "policy": policy,
                                **({"model": req.model} if req.model is not None else {}),
                                "persona_id": pid,
                                "mission_id": mid,
                                "seed": int(seed),
                                **({"agent_config": agent_config} if agent_config else {}),
                                **(
                                    {
                                        "mission_requirements_override": {
                                            "requires_shell": requires_shell,
                                            "requires_edits": requires_edits,
                                        }
                                    }
                                    if overrides
                                    else {}
                                ),
                            }
                        )

                        # Validation entry.
                        errors: list[str] = []
                        warnings: list[str] = []

                        if agent not in cfg.agents:
                            errors.append(
                                f"unknown agent {agent!r} (defined in configs/agents.yaml)."
                            )
                        else:
                            agent_cfg = cfg.agents.get(agent, {})
                            adapter = (
                                agent_cfg.get("adapter") if isinstance(agent_cfg, dict) else None
                            )
                            if isinstance(adapter, str) and adapter.endswith("_cli"):
                                binary = (
                                    agent_cfg.get("binary") if isinstance(agent_cfg, dict) else None
                                )
                                binary = str(binary).strip() if binary is not None else agent
                                # Best-effort: verify the CLI exists on PATH (or the configured absolute path exists).
                                if binary:
                                    p = Path(binary)
                                    is_pathish = (
                                        p.is_absolute()
                                        or any(sep in binary for sep in ("/", "\\"))
                                        or (os.name == "nt" and ":" in binary)
                                    )
                                    if is_pathish and not p.exists():
                                        errors.append(
                                            f"agent binary not found: {binary!r} for agent {agent!r}"
                                        )
                                    elif not is_pathish and shutil.which(binary) is None:
                                        errors.append(
                                            f"agent binary not on PATH: {binary!r} for agent {agent!r}"
                                        )
                        if policy not in cfg.policies:
                            errors.append(
                                f"unknown policy {policy!r} (defined in configs/policies.yaml)."
                            )
                        if pid not in persona_by_id:
                            errors.append(
                                f"unknown persona_id {pid!r} (available: {', '.join(sorted(persona_by_id.keys()))})."
                            )
                        if mid not in mission_by_id:
                            errors.append(
                                f"unknown mission_id {mid!r} (available: {', '.join(sorted(mission_by_id.keys()))})."
                            )

                        allow_edits = False
                        shell_status = "unknown"
                        if not errors:
                            allow_edits, shell_status = _matrix__infer_allow_edits_and_shell_status(
                                cfg=cfg, request=req
                            )
                            if requires_shell and shell_status == "blocked":
                                errors.append(
                                    "requires shell commands, but this agent/policy/backend blocks shell commands"
                                )
                            if requires_edits and not allow_edits:
                                errors.append(
                                    "requires edits, but this policy has allow_edits=false"
                                )
                            if (
                                (not requires_shell)
                                and policy in {"inspect", "write"}
                                and shell_status == "blocked"
                            ):
                                warnings.append(
                                    "policy suggests shell should be available, but backend blocks it (gemini on Windows typically needs --exec-backend docker)"
                                )

                        validation_report.append(
                            {
                                "index": idx_counter,
                                "repo": repo_input,
                                "ref": ref,
                                "agent": agent,
                                "model": req.model,
                                "policy": policy,
                                "persona_id": pid,
                                "mission_id": mid,
                                "seed": int(seed),
                                "requirements": {
                                    "requires_shell": requires_shell,
                                    "requires_edits": requires_edits,
                                    "overrides": overrides,
                                },
                                "capabilities": {
                                    "allow_edits": allow_edits,
                                    "shell_status": shell_status,
                                    "exec_backend": exec_backend,
                                },
                                "errors": errors,
                                "warnings": warnings,
                            }
                        )

                        requests.append((idx_counter, req))
                        idx_counter += 1

    # Determine default output paths.
    target_slug = slugify(repo_input)
    compiled_dir = repo_root / "runs" / "usertest" / target_slug / "_compiled"
    compiled_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    out_targets = getattr(args, "out_targets", None)
    if out_targets is None:
        out_targets = compiled_dir / f"{timestamp}.matrix.targets.yaml"
    if not Path(out_targets).is_absolute():
        out_targets = (repo_root / Path(out_targets)).resolve()

    out_report = getattr(args, "out_report", None)
    if out_report is None:
        out_report = compiled_dir / f"{timestamp}.matrix.validation.json"
    if not Path(out_report).is_absolute():
        out_report = (repo_root / Path(out_report)).resolve()

    # Write expanded targets YAML.
    targets_doc = {
        "meta": {
            "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "spec_path": str(spec_path),
            "repo": repo_input,
            "ref": ref,
            "exec_backend": exec_backend,
        },
        "targets": run_targets,
    }
    Path(out_targets).parent.mkdir(parents=True, exist_ok=True)
    Path(out_targets).write_text(yaml.safe_dump(targets_doc, sort_keys=False), encoding="utf-8")

    # Write validation report.
    Path(out_report).parent.mkdir(parents=True, exist_ok=True)
    Path(out_report).write_text(
        json.dumps(
            {
                "meta": targets_doc["meta"],
                "totals": {
                    "combinations": len(validation_report),
                    "errors": sum(1 for r in validation_report if r.get("errors")),
                    "warnings": sum(1 for r in validation_report if r.get("warnings")),
                },
                "results": validation_report,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # Surface validation errors.
    error_count = sum(1 for r in validation_report if r.get("errors"))
    warning_count = sum(1 for r in validation_report if r.get("warnings"))

    print(str(out_targets))
    print(str(out_report))
    print(f"matrix combinations: {len(validation_report)}")
    print(f"validation errors: {error_count}")
    print(f"validation warnings: {warning_count}")

    if error_count:
        # Print a short, grouped error summary.
        print("Matrix validation failed; no runs were executed.", file=sys.stderr)
        shown = 0
        for entry in validation_report:
            errs = entry.get("errors") or []
            if not errs:
                continue
            shown += 1
            if shown <= 25:
                ident = (
                    f"[{entry.get('index')}] agent={entry.get('agent')} model={entry.get('model')} "
                    f"policy={entry.get('policy')} persona={entry.get('persona_id')} mission={entry.get('mission_id')} seed={entry.get('seed')}"
                )
                print(f"- {ident}", file=sys.stderr)
                for err in errs:
                    print(f"    - {err}", file=sys.stderr)
        if shown > 25:
            print(f"... and {shown - 25} more", file=sys.stderr)
        return 2

    # Run additional environment probes via the existing batch validator (it also checks local repo paths).
    batch_errors = _prevalidate_batch_requests(
        cfg=cfg,
        repo_root=repo_root,
        targets_path=spec_path,
        requests=requests,
        probe_timeout_seconds=float(getattr(args, "command_probe_timeout_seconds", 0.25)),
        skip_command_responsiveness_probes=bool(getattr(args, "skip_command_probes", False)),
        validate_only=not execute,
    )
    if batch_errors:
        print("Matrix environment validation failed; no runs were executed.", file=sys.stderr)
        for e in batch_errors:
            print(f"- {e}", file=sys.stderr)
        return 2

    if not execute:
        return 0

    exit_code = 0
    for _idx, req in requests:
        result = run_once(cfg, req)
        print(str(result.run_dir))
        if result.exit_code != 0 or result.report_validation_errors:
            exit_code = 2

    return exit_code

__all__ = ['add_matrix_command', '_cmd_matrix', '_cmd_matrix_plan', '_cmd_matrix_run']
