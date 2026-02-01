# core/player.py - DEBUG VERSION
import vlc
import time
import threading
import logging

class VLCMusic:
    """VLC wrapper for audio playback."""
    def __init__(self):
        self.instance = vlc.Instance('--no-xlib', '--no-video')  # no video
        self.player = self.instance.media_player_new()
        self.current_media = None
        self._end_callback = None
        self._events_attached = False
        self.set_volume(0.7)
        
        # CRITICAL: Attach event handler ONCE at initialization
        try:
            em = self.player.event_manager()
            em.event_attach(vlc.EventType.MediaPlayerEndReached, self._vlc_end_event)
            self._events_attached = True
            logging.debug("VLC: End event handler attached successfully")
        except Exception as e:
            self._events_attached = False
            logging.error(f"VLC: Failed to attach event handler: {e}")
    
    def load(self, filepath):
        media = self.instance.media_new(str(filepath))
        self.player.set_media(media)
        self.current_media = media
        logging.debug(f"VLC: Loaded media: {filepath}")
    
    def play(self):
        self.player.play()
        time.sleep(0.05)
        logging.debug("VLC: Started playback")
    
    def pause(self):
        if self.player.is_playing():
            self.player.pause()
            logging.debug("VLC: Paused")
    
    def unpause(self):
        self.player.set_pause(False)
        logging.debug("VLC: Unpaused")
    
    def stop(self):
        try:
            self.player.stop()
            logging.debug("VLC: Stopped")
        except Exception as e:
            logging.error(f"VLC: Error stopping: {e}")
    
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
        return max(0, self.player.get_time())
    
    def get_length(self):
        length = self.player.get_length()
        return length if (length and length > 0) else None
    
    def set_time(self, ms):
        try:
            self.player.set_time(int(ms))
        except Exception:
            pass
    
    def add_end_callback(self, callback):
        """Register a callback to be called when song ends"""
        self._end_callback = callback
        logging.debug(f"VLC: End callback registered")
    
    def _vlc_end_event(self, event):
        """Called by VLC event thread when media ends"""
        logging.debug("VLC: MediaPlayerEndReached event fired!")
        self._raise_end()
    
    def _raise_end(self):
        """Call the registered callback"""
        if callable(self._end_callback):
            try:
                logging.debug(f"VLC: Calling end callback")
                self._end_callback()
                logging.debug("VLC: End callback completed")
            except Exception as e:
                logging.error(f"VLC: Error in end callback: {e}")
        else:
            logging.warning("VLC: MediaPlayerEndReached fired but no callback registered!")