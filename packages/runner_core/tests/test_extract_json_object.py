from __future__ import annotations

import pytest

from runner_core.runner import _extract_json_object


def test_extract_json_object_accepts_fenced_json() -> None:
    text = "```json\n{\"ok\": \"yes\"}\n```"
    assert _extract_json_object(text) == {"ok": "yes"}


def test_extract_json_object_accepts_preamble_and_trailing_noise() -> None:
    text = "WARNING: something happened\n{\"ok\": \"yes\"}\n(extra)"
    assert _extract_json_object(text) == {"ok": "yes"}


def test_extract_json_object_errors_on_missing_object() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        _extract_json_object("no json here")
