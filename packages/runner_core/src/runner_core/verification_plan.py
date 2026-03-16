from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class VerificationTrack(StrEnum):
    CHANGE_VALIDATION = "change_validation"
    REPO_HEALTH = "repo_health"
    BOOTSTRAP = "bootstrap"


@dataclass(frozen=True)
class VerificationCommandSpec:
    command: str
    track: VerificationTrack | str = VerificationTrack.CHANGE_VALIDATION

    def to_dict(self) -> dict[str, Any]:
        track = self.track.value if isinstance(self.track, VerificationTrack) else str(self.track)
        return {
            "command": self.command,
            "track": track,
        }
