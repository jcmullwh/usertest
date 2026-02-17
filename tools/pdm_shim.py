"""
PDM shim for Windows hosts with broken WMI.

Why this exists
---------------
On some Windows machines, `platform.system()` (and friends) can hang due to WMI queries.
PDM imports `pbs_installer`, which calls `platform.system()` at import time, so *any* `pdm`
invocation can hang indefinitely.

This shim patches `platform._wmi_query` to raise `OSError`, forcing the stdlib
`platform` module to fall back to non-WMI mechanisms.

This is intended for *development tooling* (e.g. `tools/scaffold/scaffold.py run ...`).
It should not affect runtime pipeline behavior.
"""

from __future__ import annotations

import platform
import sys
from typing import Callable


def _disable_platform_wmi() -> None:
    """
    Disable WMI-backed platform queries.

    Notes
    -----
    Python's stdlib `platform._win32_ver()` catches `OSError` from `_wmi_query()` and falls
    back to `sys.getwindowsversion()` + `ver`. Raising `OSError` avoids hangs while keeping
    behavior close to upstream.
    """

    wmi_query = getattr(platform, "_wmi_query", None)
    if not callable(wmi_query):
        return

    def _no_wmi(*_args: object, **_kwargs: object) -> tuple[()]:
        raise OSError("WMI disabled by tools/pdm_shim.py")

    platform._wmi_query = _no_wmi  # type: ignore[attr-defined]


def main(argv: list[str] | None = None) -> None:
    """
    Entrypoint that behaves like `python -m pdm`.

    Parameters
    ----------
    argv:
        Arguments to pass through to PDM. If omitted, uses `sys.argv[1:]`.
    """

    _disable_platform_wmi()

    from pdm.core import main as pdm_main

    pdm_main(list(argv) if argv is not None else sys.argv[1:])


if __name__ == "__main__":
    main(sys.argv[1:])
