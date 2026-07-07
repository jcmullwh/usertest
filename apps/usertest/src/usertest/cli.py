from __future__ import annotations

import argparse

from usertest.commands.shared import _configure_console_output

_configure_console_output()


def build_parser() -> argparse.ArgumentParser:
    """Build the usertest CLI argument parser."""
    from usertest.parser import build_parser as _build_parser

    return _build_parser()


def main(argv: list[str] | None = None) -> None:
    """Run the CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if callable(func):
        raise SystemExit(func(args))
    raise SystemExit(2)


if __name__ == "__main__":
    main()
