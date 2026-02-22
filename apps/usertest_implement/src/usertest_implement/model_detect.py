from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_MODEL_PATTERNS = (
    re.compile(r"\bmodel=([A-Za-z0-9_.:-]+)\b"),
    re.compile(r"\bmodel:\s*([A-Za-z0-9_.:-]+)\b"),
)


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None


def _extract_model_from_text(text: str) -> str | None:
    for pat in _MODEL_PATTERNS:
        match = pat.search(text)
        if match is not None:
            model = match.group(1).strip()
            if model:
                return model
    return None


def infer_observed_model(*, run_dir: Path) -> str | None:
    """
    Best-effort model inference from run artifacts.

    Preference order:
    1) `target_ref.json["model"]` (explicit, user-provided via `--model`)
    2) `agent_attempts.json` warnings (some agent CLIs log `model=...` to stderr)
    3) `agent_stderr.txt` (fallback)
    """

    target_ref = _read_json(run_dir / "target_ref.json")
    if isinstance(target_ref, dict):
        model = target_ref.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()

    attempts = _read_json(run_dir / "agent_attempts.json")
    if isinstance(attempts, dict):
        attempts_list = attempts.get("attempts")
        if isinstance(attempts_list, list):
            for attempt in attempts_list:
                if not isinstance(attempt, dict):
                    continue
                warnings = attempt.get("warnings")
                if not isinstance(warnings, list):
                    continue
                for warning in warnings:
                    if not isinstance(warning, str) or not warning.strip():
                        continue
                    model = _extract_model_from_text(warning)
                    if model is not None:
                        return model

    try:
        stderr_text = (run_dir / "agent_stderr.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        stderr_text = ""

    return _extract_model_from_text(stderr_text) if stderr_text else None
