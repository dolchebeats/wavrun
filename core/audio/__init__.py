"""VLC-independent playback. Run with python -m core.audio."""

from .models import PlaybackEvent, PlayerState

__all__ = ["AudioPlayer", "PlaybackError", "PlaybackEvent", "PlayerState"]


def __getattr__(name):
    if name == "AudioPlayer":
        from .player import AudioPlayer
        return AudioPlayer
    if name == "PlaybackError":
        from .engine import PlaybackError
        return PlaybackError
    raise AttributeError(name)
