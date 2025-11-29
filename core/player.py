# core/player.py
import vlc
import time
import threading

class VLCMusic:
    """VLC wrapper for audio playback."""
    def __init__(self):
        self.instance = vlc.Instance('--no-xlib', '--no-video')  # no video
        self.player = self.instance.media_player_new()
        self.current_media = None
        self._end_callback = None
        self._events_attached = False
        self.set_volume(0.7)

    def load(self, filepath):
        media = self.instance.media_new(str(filepath))
        self.player.set_media(media)
        self.current_media = media
        # mark events so add_end_callback attaches once
        self._events_attached = False

    def play(self):
        self.player.play()
        time.sleep(0.05)

    def pause(self):
        if self.player.is_playing():
            self.player.pause()

    def unpause(self):
        if not self.player.is_playing():
            self.player.pause()

    def stop(self):
        try:
            self.player.stop()
        except Exception:
            pass

    def set_volume(self, v):
        try:
            vol = int(max(0.0, min(1.0, float(v))) * 100)
            self.player.audio_set_volume(vol)
            self._volume = vol
        except Exception:
            pass

    def get_busy(self):
        try:
            return bool(self.player.is_playing())
        except Exception:
            return False

    def get_pos(self):
        t = self.player.get_time()
        return t if (t and t >= 0) else 0

    def get_length(self):
        length = self.player.get_length()
        return length if (length and length > 0) else None

    def set_time(self, ms):
        try:
            self.player.set_time(int(ms))
        except Exception:
            pass

    def add_end_callback(self, callback):
        self._end_callback = callback
        try:
            em = self.player.event_manager()
            if not self._events_attached:
                em.event_attach(vlc.EventType.MediaPlayerEndReached, lambda e: self._raise_end())
                self._events_attached = True
        except Exception:
            pass

    def _raise_end(self):
        if callable(self._end_callback):
            try:
                self._end_callback()
            except Exception:
                pass
