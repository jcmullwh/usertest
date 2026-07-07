# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from usertest_implement.ci import _optional_timeout_seconds
from usertest_implement.shared import *


def _cmd_maintenance_images_list(args: argparse.Namespace) -> int:
    """Print the local maintenance-image inventory as JSON."""

    repo_root = _resolve_repo_root(getattr(args, "repo_root", None))
    payload = list_local_maintenance_images(
        repo_root=repo_root,
        timeout_seconds=_optional_timeout_seconds(args.timeout_seconds),
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _cmd_maintenance_images_cleanup(args: argparse.Namespace) -> int:
    """Prune old local maintenance-image tags using the configured retention policy."""

    repo_root = _resolve_repo_root(getattr(args, "repo_root", None))
    dry_run = args.dry_run
    if dry_run is None:
        dry_run = _load_maintenance_docker_config(repo_root=repo_root).cleanup_dry_run_default
    payload = cleanup_local_maintenance_images(
        repo_root=repo_root,
        timeout_seconds=_optional_timeout_seconds(args.timeout_seconds),
        dry_run=bool(dry_run),
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0




__all__ = [name for name in globals() if not name.startswith("__")]
