from __future__ import annotations

import pytest

from runner_core.artifacts import (
    _extract_json_object,
    _extract_json_object_with_receipt,
)


def test_extract_json_object_accepts_fenced_json() -> None:
    text = "```json\n{\"ok\": \"yes\"}\n```"
    assert _extract_json_object(text) == {"ok": "yes"}


def test_extract_json_object_accepts_preamble_and_trailing_noise() -> None:
    text = "WARNING: something happened\n{\"ok\": \"yes\"}\n(extra)"
    assert _extract_json_object(text) == {"ok": "yes"}


def test_extract_json_object_errors_on_missing_object() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        _extract_json_object("no json here")


def test_extract_json_object_repairs_unique_missing_eof_delimiter() -> None:
    parsed, receipt = _extract_json_object_with_receipt(
        '{"status":"success","evidence":{"observed":true}'
    )

    assert parsed == {"status": "success", "evidence": {"observed": True}}
    assert receipt is not None
    assert receipt["repair_kind"] == "append_missing_eof_delimiters"
    assert receipt["appended_delimiters"] == "}"
    assert receipt["appended_delimiter_count"] == 1


def test_extract_json_object_does_not_scavenge_nested_object_from_bad_root() -> None:
    with pytest.raises(ValueError, match="Top-level JSON object"):
        _extract_json_object('{"evidence":{"observed":true},"status":')


def test_extract_json_object_does_not_repair_unterminated_string() -> None:
    with pytest.raises(ValueError, match="Top-level JSON object"):
        _extract_json_object('{"status":"succ')
