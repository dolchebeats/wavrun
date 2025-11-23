import vlc
import time
import threading

class VLCMusic:
    def __init__(self):
        self.instance = vlc.Instance()
        self.player = self.instance.media_player_new()
        self.current_media = None
        self.volume = 70
        self.player.audio_set_volume(self.volume)
        self._end_callback = None

    def load(self, filepath):
        media = self.instance.media_new(filepath)
        self.player.set_media(media)
        self.current_media = media

    def play(self):
        self.player.play()
        # VLC is async — give it time to start
        time.sleep(0.05)

    def pause(self):
        # VLC pause toggles — but we only pause if already playing
        if self.player.is_playing():
            self.player.pause()

    def unpause(self):
        # If not playing, toggling pause resumes
        if not self.player.is_playing():
            self.player.pause()

    def stop(self):
        self.player.stop()

    def set_volume(self, v):
        self.volume = int(v * 100)
        self.player.audio_set_volume(self.volume)

    def get_busy(self):
        return bool(self.player.is_playing())

    def get_pos(self):
        t = self.player.get_time()
        return t if t >= 0 else 0   # returns ms

    def get_length(self):
        length = self.player.get_length()
        return length if length > 0 else None

    def add_end_callback(self, callback):
        self._end_callback = callback
        event_manager = self.player.event_manager()
        event_manager.event_attach(
            vlc.EventType.MediaPlayerEndReached,
            lambda e: self._end_callback()
        )
