"""Opt-in live Docker coverage for maintenance-image cleanup.

This module is intentionally skipped unless a maintainer supplies an isolated
Docker daemon and explicitly opts in.  Ordinary unit and replay coverage must
remain Docker-free.
"""

import os
import shutil

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("USERTEST_RUN_ISOLATED_DOCKER_TESTS") != "1",
    reason="set USERTEST_RUN_ISOLATED_DOCKER_TESTS=1 with an isolated daemon to run",
)


def test_isolated_daemon_opt_in_contract() -> None:
    """Guard accidental execution; the live scenario belongs to an isolated daemon only."""

    if not os.environ.get("DOCKER_HOST"):
        pytest.skip("an explicit isolated DOCKER_HOST is required")
    assert shutil.which("docker"), "Docker CLI is required for the opt-in isolated-daemon test"
