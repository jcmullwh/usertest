from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any


def _normalized_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def observed_read_attestation(
    *,
    path: Path,
    observed_text: str | None,
    source_exit_code: int,
    allow_partial: bool,
) -> dict[str, Any]:
    """Describe the exact file content proven visible to an agent.

    Shell output is never eligible for partial matching: search/list commands and
    unrelated chained stdout must not become source-read evidence. Tool reads may
    attest a uniquely located partial range, which is sufficient only for symbols
    whose complete definition is present in that observed range.
    """

    result: dict[str, Any] = {
        "content_observed": False,
        "whole_file_observed": False,
        "observed_content": None,
        "observed_content_sha256": None,
        "observed_bytes": None,
        "observed_start_line": None,
        "observed_end_line": None,
        "file_sha256": None,
        "file_size_bytes": None,
    }
    if source_exit_code != 0 or not path.is_file() or not isinstance(observed_text, str):
        return result

    try:
        file_bytes = path.read_bytes()
        file_text = _normalized_text(file_bytes.decode("utf-8", errors="replace"))
    except OSError:
        return result

    observed = _normalized_text(observed_text)
    result["file_sha256"] = sha256(file_bytes).hexdigest()
    result["file_size_bytes"] = len(file_bytes)
    if observed == file_text:
        start_line = 1
        end_line = max(1, file_text.count("\n") + (0 if file_text.endswith("\n") else 1))
        result.update(
            {
                "content_observed": True,
                "whole_file_observed": True,
                "observed_content": observed,
                "observed_content_sha256": sha256(observed.encode("utf-8")).hexdigest(),
                "observed_bytes": len(observed.encode("utf-8")),
                "observed_start_line": start_line,
                "observed_end_line": end_line,
            }
        )
        return result

    if not allow_partial or not observed:
        return result
    first = file_text.find(observed)
    if first < 0 or file_text.find(observed, first + 1) >= 0:
        return result
    start_line = file_text.count("\n", 0, first) + 1
    end_line = start_line + observed.count("\n")
    result.update(
        {
            "content_observed": True,
            "observed_content": observed,
            "observed_content_sha256": sha256(observed.encode("utf-8")).hexdigest(),
            "observed_bytes": len(observed.encode("utf-8")),
            "observed_start_line": start_line,
            "observed_end_line": max(start_line, end_line),
        }
    )
    return result


__all__ = ["observed_read_attestation"]
