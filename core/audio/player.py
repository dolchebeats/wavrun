"""Reusable playback controls and events, independent of any user interface."""
from collections import deque
import logging
import math
import threading

from .engine import PlaybackError, PrototypePlayer
from .models import PlaybackEvent, PlayerState


class AudioPlayer:
    """Thread-safe track lifecycle with millisecond position/duration methods.

    load/play/seek may block while buffering; call them from a UI worker. stop and
    close invalidate in-flight startup immediately and perform bounded decoder cleanup.
    User callbacks run on the event thread, never on the audio callback thread.
    """

    def __init__(self, *, device=None, volume=0.7, buffer_seconds=2,
                 prebuffer_seconds=0.25, startup_timeout=15, backend=None,
                 _session_factory=PrototypePlayer):
        if not math.isfinite(volume) or not 0 <= volume <= 1:
            raise ValueError("Volume must be between 0 and 1")
        self._options = dict(device=device, buffer_seconds=buffer_seconds,
                             prebuffer_seconds=prebuffer_seconds,
                             startup_timeout=startup_timeout, backend=backend)
        self._factory = _session_factory
        self._lock = threading.RLock()
        self._operations = threading.RLock()
        self._events = deque()
        self._event_condition = threading.Condition()
        self._event_stop = False
        self._listeners = []
        self._end_callback = self._error_callback = None
        self._session = None
        self._session_id = 0
        self._terminal_id = None
        self._path = None
        self._position = 0
        self._duration = None
        self._volume = volume
        self._state = PlayerState.STOPPED
        self._error = None
        self._done = threading.Event()
        self._done.set()
        self._event_thread = threading.Thread(target=self._dispatch, name="wavrun-audio-events", daemon=True)
        self._event_thread.start()

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def session_id(self):
        with self._lock:
            return self._session_id

    @property
    def error(self):
        with self._lock:
            return self._error

    @property
    def volume(self):
        with self._lock:
            return self._volume

    def _ensure_open(self):
        if self._state == PlayerState.CLOSED:
            raise PlaybackError("Audio player is closed")

    def _publish(self, kind="state"):
        # Called with the controller lock. Stale notifications are also checked
        # when dispatched, so rapid switches cannot deliver an old queued end.
        event = PlaybackEvent(kind, self._session_id, self._state, self._path,
                              self._position, self._duration, self._error)
        with self._event_condition:
            if len(self._events) >= 128:
                self._events = deque(item for item in self._events
                                     if item.session_id == self._session_id and item.kind != "state")
            self._events.append(event)
            self._event_condition.notify()

    def _set_state(self, state):
        if self._state != state:
            self._state = state
            self._publish()

    def _dispatch(self):
        while True:
            with self._event_condition:
                self._event_condition.wait_for(lambda: self._events or self._event_stop)
                if self._event_stop and not self._events:
                    return
                event = self._events.popleft()
            with self._lock:
                if event.session_id != self._session_id:
                    continue
                callbacks = [(callback, True) for callback in self._listeners]
                legacy = self._end_callback if event.kind == "end" else (
                    self._error_callback if event.kind == "error" else None)
                if legacy is not None:
                    callbacks.append((legacy, False))
            for callback, with_event in callbacks:
                with self._lock:
                    if event.session_id != self._session_id:
                        break
                try:
                    callback(event) if with_event else callback()
                except Exception:
                    logging.exception("Playback event callback failed")

    def add_event_callback(self, callback):
        with self._lock:
            self._ensure_open()
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_event_callback(self, callback):
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def add_end_callback(self, callback):
        with self._lock:
            self._end_callback = callback

    def add_error_callback(self, callback):
        with self._lock:
            self._error_callback = callback

    def load(self, path):
        path = str(path)
        if not path:
            raise ValueError("Provide an audio file path")
        with self._lock:
            self._ensure_open()
            old = self._session
            self._session = None
            self._session_id += 1
            self._path = path
            self._position = 0
            self._duration = None
            self._error = None
            self._done.set()
            self._set_state(PlayerState.STOPPED)
        if old is not None:
            old.request_stop()
            with self._operations:
                old.close()

    def _replace_session(self, offset_ms=0, paused=False, expected_id=None):
        with self._lock:
            self._ensure_open()
            if expected_id is not None and expected_id != self._session_id:
                return False
            if self._path is None:
                raise PlaybackError("Load a track before starting playback")
            old = self._session
            self._session = None
            self._session_id += 1
            generation = self._session_id
            path = self._path
            self._position = offset_ms
            self._error = None
            self._done.clear()
            self._set_state(PlayerState.LOADING)
        if old is not None:
            old.request_stop()
        # Startup may wait for data, but newer commands can invalidate its ID
        # and signal request_stop before waiting here for device cleanup.
        with self._operations:
            if old is not None:
                old.close()
            return self._start_session(generation, path, offset_ms, paused)

    def _start_session(self, generation, path, offset_ms, paused):
        session = None
        try:
            with self._lock:
                if generation != self._session_id:
                    return False
                volume = self._volume
            session = self._factory(path, **self._options, volume=volume,
                                    start_seconds=offset_ms / 1000, initially_paused=paused)
            with self._lock:
                if generation != self._session_id:
                    session.close()
                    return False
                self._session = session
                session.set_volume(self._volume)
            monitor = threading.Thread(target=self._monitor, args=(generation, session),
                                       name="wavrun-audio-monitor", daemon=True)
            monitor.start()
            session.start()
            with self._lock:
                if generation != self._session_id:
                    return False
                self._position = session.get_pos()
                self._duration = session.get_length()
                if not session._finished.is_set():
                    self._set_state(session.state)
            return True
        except Exception as error:
            if session is not None:
                session.close()
            with self._lock:
                if generation != self._session_id:
                    return False
                self._fail(generation, str(error))
            if isinstance(error, PlaybackError):
                raise
            raise PlaybackError(str(error)) from error

    def _fail(self, generation, error):
        if self._terminal_id == generation:
            return
        self._terminal_id = generation
        self._error = error
        self._set_state(PlayerState.ERROR)
        self._publish("error")
        self._done.set()

    def _monitor(self, generation, session):
        while True:
            finished = session._finished.wait(0.02)
            with self._lock:
                if generation != self._session_id:
                    return
                self._position = session.get_pos()
                self._duration = session.get_length()
                if finished:
                    if self._terminal_id != generation:
                        if session.error:
                            self._fail(generation, session.error)
                        else:
                            self._terminal_id = generation
                            self._set_state(PlayerState.STOPPED)
                            self._publish("end")
                            self._done.set()
                else:
                    self._set_state(session.state)
            if finished:
                with self._operations:
                    session.close()
                return

    def play(self):
        with self._lock:
            self._ensure_open()
            generation = self._session_id
            if self._state == PlayerState.PAUSED:
                resume = True
            elif self._state in (PlayerState.PLAYING, PlayerState.BUFFERING, PlayerState.LOADING):
                return True
            else:
                resume = False
                offset = self._position if self._terminal_id != self._session_id else 0
        return self.unpause() if resume else self._replace_session(offset, expected_id=generation)

    def pause(self):
        with self._lock:
            self._ensure_open()
            session = self._session
            generation = self._session_id
        if session is None:
            return False
        try:
            changed = session.pause()
        except Exception as error:
            self._control_error(generation, session, error)
            return False
        with self._lock:
            if generation == self._session_id and changed:
                self._position = session.get_pos()
                self._set_state(PlayerState.PAUSED)
            return generation == self._session_id and changed

    def unpause(self):
        with self._lock:
            self._ensure_open()
            session = self._session
            generation = self._session_id
        if session is None:
            return False
        try:
            changed = session.unpause()
        except Exception as error:
            self._control_error(generation, session, error)
            return False
        with self._lock:
            if generation == self._session_id and changed:
                self._set_state(session.state)
            return generation == self._session_id and changed

    def _control_error(self, generation, session, error):
        session.error = str(error)
        session.request_stop()
        with self._operations:
            session.close()
        with self._lock:
            if generation == self._session_id:
                self._fail(generation, str(error))

    def set_time(self, milliseconds):
        if not math.isfinite(milliseconds):
            raise ValueError("Seek position must be finite")
        with self._lock:
            self._ensure_open()
            offset = max(0, round(milliseconds))
            if self._duration is not None:
                offset = min(offset, self._duration)
            paused = self._state == PlayerState.PAUSED
            generation = self._session_id
            active = self._session is not None and self._state not in (
                PlayerState.STOPPED, PlayerState.ERROR)
            if not active:
                self._position = offset
                self._session = None
                self._terminal_id = None
                return True
        return self._replace_session(offset, paused, expected_id=generation)

    def set_volume(self, volume):
        if not math.isfinite(volume) or not 0 <= volume <= 1:
            raise ValueError("Volume must be between 0 and 1")
        with self._lock:
            self._ensure_open()
            self._volume = float(volume)
            if self._session is not None:
                self._session.set_volume(self._volume)

    def get_pos(self):
        with self._lock:
            if self._session is not None:
                self._position = self._session.get_pos()
            return self._position

    def get_length(self):
        with self._lock:
            return self._duration

    def get_busy(self):
        return self.state in (PlayerState.LOADING, PlayerState.BUFFERING, PlayerState.PLAYING)

    @property
    def format(self):
        with self._lock:
            return self._session.format if self._session is not None else None

    @property
    def frames_played(self):
        with self._lock:
            return self._session.frames_played if self._session is not None else 0

    @property
    def underruns(self):
        with self._lock:
            return self._session.underruns if self._session is not None else 0

    def wait(self, timeout=None):
        done = self._done.wait(timeout)
        if done and self.error:
            raise PlaybackError(self.error)
        return done

    def stop(self):
        with self._lock:
            if self._state == PlayerState.CLOSED:
                return
            self._session_id += 1
            session = self._session
            self._session = None
            self._position = 0
            self._error = None
            self._done.set()
            self._set_state(PlayerState.STOPPED)
        if session is not None:
            session.request_stop()
            with self._operations:
                session.close()

    def cancel_pending(self):
        """Invalidate startup immediately; load/stop/close then retires resources.

        This performs no native device I/O and is safe for a UI event handler.
        It lets a newer request interrupt a play() waiting in another thread.
        """
        with self._lock:
            if self._state == PlayerState.CLOSED:
                return
            self._session_id += 1
            self._error = None
            self._done.set()
            self._set_state(PlayerState.STOPPED)
            if self._session is not None:
                self._session.request_stop()

    def close(self):
        with self._lock:
            if self._state == PlayerState.CLOSED:
                return
            self._session_id += 1
            session = self._session
            self._session = None
            self._done.set()
            self._set_state(PlayerState.CLOSED)
        if session is not None:
            session.request_stop()
            with self._operations:
                session.close()
        with self._event_condition:
            self._event_stop = True
            self._event_condition.notify_all()
        if threading.current_thread() is not self._event_thread:
            self._event_thread.join(timeout=0.5)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
