"""Public playback state and immutable, session-tagged notifications."""
from dataclasses import dataclass
from enum import Enum


class PlayerState(str, Enum):
    STOPPED = "stopped"
    LOADING = "loading"
    BUFFERING = "buffering"
    PLAYING = "playing"
    PAUSED = "paused"
    ERROR = "error"
    CLOSED = "closed"


@dataclass(frozen=True)
class PlaybackEvent:
    kind: str  # state, end, error
    session_id: int
    state: PlayerState
    path: str | None
    position_ms: int
    duration_ms: int | None
    error: str | None = None
