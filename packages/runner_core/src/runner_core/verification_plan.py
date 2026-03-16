from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


from typing import Any


class VerificationTrack(str, Enum):
    CHANGE_VALIDATION = "change_validation"
    REPO_HEALTH = "repo_health"
    BOOTSTRAP = "bootstrap"


@dataclass(frozen=True)
class VerificationCommandSpec:
    command: str
    track: str = VerificationTrack.CHANGE_VALIDATION

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "track": str(self.track),
        }
