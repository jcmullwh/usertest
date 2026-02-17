from __future__ import annotations

import json


def toml_basic_string(value: str) -> str:
    """
    Return a TOML basic string literal (compatible with JSON string encoding).

    Codex CLI `--config key=value` parses values as TOML when possible; JSON string encoding
    is a compatible subset for TOML basic strings.
    """

    return json.dumps(value, ensure_ascii=False)


__all__ = ["toml_basic_string"]
