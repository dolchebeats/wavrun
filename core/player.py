"""Application playback entry point; no VLC installation or bindings required."""
from .audio import AudioPlayer, PlaybackError, PlaybackEvent, PlayerState

__all__ = ["AudioPlayer", "PlaybackError", "PlaybackEvent", "PlayerState"]
