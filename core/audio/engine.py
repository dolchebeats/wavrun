"""Buffered output sessions; the reusable AudioPlayer controller owns track changes."""
import math
import threading
import time

import numpy as np

from .buffer import PCMBuffer
from .decoder import DecoderError, DecoderSession, PCMFormat
from .clock import OutputClock
from .models import PlayerState


class PlaybackError(RuntimeError):
    pass


def output_format(backend, device=None):
    """Use the default device's preferred rate, with stereo/mono fallbacks."""
    try:
        info = backend.query_devices(device, kind="output")
        maximum = int(info["max_output_channels"])
        if maximum < 1:
            raise PlaybackError("The selected device has no audio outputs")
        preferred = float(info["default_samplerate"])
        rates = [int(round(preferred))] if math.isfinite(preferred) else []
        rates += [48000, 44100]
        for channels in ([2, 1] if maximum >= 2 else [1]):
            for rate in dict.fromkeys(rates):
                if not 8000 <= rate <= 384000:
                    continue
                try:
                    backend.check_output_settings(device=device, channels=channels,
                                                  dtype="float32", samplerate=rate)
                    return PCMFormat(rate, channels)
                except Exception:
                    continue
        raise PlaybackError("No supported mono/stereo floating-point output format")
    except PlaybackError:
        raise
    except Exception as error:
        raise PlaybackError(f"Cannot open an audio output device: {error}") from error


class PrototypePlayer:
    """One output session, reusable across pauses. Seek starts a new session."""

    def __init__(self, path, *, device=None, volume=0.7, buffer_seconds=2.0,
                 prebuffer_seconds=0.25, startup_timeout=15.0, backend=None,
                 start_seconds=0, initially_paused=False):
        if not math.isfinite(volume) or not 0 <= volume <= 1:
            raise ValueError("Volume must be between 0 and 1")
        if (not all(math.isfinite(value) for value in
                    (buffer_seconds, prebuffer_seconds, startup_timeout)) or
                not 0.05 <= prebuffer_seconds <= buffer_seconds <= 30 or startup_timeout <= 0):
            raise ValueError("Invalid buffering or startup timeout settings")
        if backend is None:
            try:
                import sounddevice as backend
            except (ImportError, OSError) as error:
                raise PlaybackError(f"Audio output is unavailable: {error}") from error
        self._backend = backend
        self.format = output_format(backend, device)
        self._buffer = PCMBuffer(int(buffer_seconds * self.format.rate), self.format.channels)
        self._prefill = int(prebuffer_seconds * self.format.rate)
        self._timeout = startup_timeout
        self._volume = volume
        self._decoder = DecoderSession(path, self.format, start_seconds=start_seconds)
        self._offset = start_seconds
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._decoder_finished = threading.Event()
        self._close_lock = threading.RLock()
        self._feeder = None
        self._stream = None
        self._started = False
        self._closed = False
        self._paused = initially_paused
        self._output_started = False
        self._ready_to_play = False
        self._buffering = True
        self._natural_end = False
        self.info = None
        self.error = None
        self._clock = OutputClock(self.format.rate)
        self.underruns = 0
        self._device = device

    def _feed(self):
        try:
            while not self._stop.is_set():
                message = self._decoder.receive()
                if message is None:
                    continue
                kind, value = message
                if kind == "info":
                    self.info = value
                    if value["duration"] is not None:
                        self._offset = min(self._offset, value["duration"])
                elif kind == "pcm":
                    if not self.info or len(value) > 1024 * self.format.channels * 4:
                        raise DecoderError("Invalid PCM message")
                    samples = np.frombuffer(value, dtype=np.float32).reshape(-1, self.format.channels)
                    if not self._buffer.write(samples):
                        return
                elif kind == "eof":
                    self._buffer.finish()
                    return
                elif kind == "error":
                    raise DecoderError(value)
                else:
                    raise DecoderError(f"Unknown decoder message: {kind}")
        except Exception as error:
            if not self._stop.is_set():
                self.error = str(error)
                self._buffer.finish(self.error)
        finally:
            self._decoder_finished.set()

    def _callback(self, output, frames, timing, status):
        # No I/O, decoding, logging, IPC, or waits on the audio callback thread.
        try:
            if self._paused or self._stop.is_set():
                output.fill(0)
                return
            count, eof, error = self._buffer.read_into(output, self._volume)
            self._clock.record(count, timing, getattr(self._stream, "latency", 0))
            if error:
                self.error = error
                raise self._backend.CallbackAbort
            if eof:
                self._natural_end = True
                # CallbackStop drains this final output block before finished_callback.
                raise self._backend.CallbackStop
            if count < frames or status.output_underflow:
                self.underruns += 1
            self._buffering = count < frames
        except (self._backend.CallbackStop, self._backend.CallbackAbort):
            raise
        except Exception as error:
            output.fill(0)
            self.error = f"Audio callback failed: {error}"
            raise self._backend.CallbackAbort

    def _on_finished(self):
        if self._stop.is_set() or self._paused:
            return
        if self._natural_end:
            self._clock.freeze(self.frames_played)
        elif self.error is None:
            self.error = "Audio output stopped unexpectedly; check the output device"
        self._finished.set()

    @property
    def frames_played(self):
        return self._clock.submitted

    @property
    def state(self):
        if self.error:
            return PlayerState.ERROR
        if self._closed:
            return PlayerState.CLOSED
        if self._paused:
            return PlayerState.PAUSED
        if self._finished.is_set():
            return PlayerState.STOPPED
        if not self._output_started:
            return PlayerState.BUFFERING if self.info else PlayerState.LOADING
        return PlayerState.BUFFERING if self._buffering else PlayerState.PLAYING

    def get_pos(self):
        seconds = self._offset + self._clock.position() / self.format.rate
        if self.info and self.info["duration"] is not None:
            seconds = min(seconds, self.info["duration"])
        return max(0, round(seconds * 1000))

    def get_length(self):
        duration = self.info.get("duration") if self.info else None
        return round(duration * 1000) if duration is not None else None

    def set_volume(self, volume):
        if not math.isfinite(volume) or not 0 <= volume <= 1:
            raise ValueError("Volume must be between 0 and 1")
        self._volume = float(volume)

    def pause(self):
        with self._close_lock:
            if self._closed or self._finished.is_set() or self._paused:
                return False
            self._paused = True
            if self._output_started:
                self._stream.stop()
            self._clock.freeze(self.frames_played)
            return True

    def unpause(self):
        with self._close_lock:
            if self._closed or self._finished.is_set() or not self._paused:
                return False
            self._paused = False
            self._natural_end = False
            self._clock.resume()
            if self._ready_to_play:
                self._output_started = True
                self._stream.start()
            return True

    def start(self):
        if self._started or self._closed:
            raise PlaybackError("Create a new player session for each track")
        self._started = True
        try:
            with self._close_lock:
                if self._closed or self._stop.is_set():
                    raise PlaybackError("Playback session is closed")
                self._stream = self._backend.OutputStream(
                    device=self._device, samplerate=self.format.rate,
                    channels=self.format.channels, dtype="float32", blocksize=0,
                    latency="high", callback=self._callback, finished_callback=self._on_finished)
                self._decoder.start()
                self._feeder = threading.Thread(target=self._feed, name="wavrun-pcm-feeder", daemon=True)
                self._feeder.start()
            deadline = time.monotonic() + self._timeout
            while self._buffer.available < self._prefill and not self._decoder_finished.is_set():
                if self._stop.wait(0.01):
                    raise PlaybackError("Playback was stopped during startup")
                if time.monotonic() >= deadline:
                    raise PlaybackError("Timed out waiting for decoded audio")
            if self.error:
                raise PlaybackError(self.error)
            if self._stop.is_set():
                raise PlaybackError("Playback was stopped during startup")
            with self._close_lock:
                if self._closed:
                    raise PlaybackError("Playback session is closed")
                self._ready_to_play = True
                if not self._paused:
                    self._output_started = True
                    self._stream.start()
                else:
                    self._clock.freeze(0)
            return self
        except BaseException as error:
            if isinstance(error, Exception):
                self.error = str(error)
            self.close()
            if isinstance(error, Exception) and not isinstance(error, PlaybackError):
                raise PlaybackError(f"Cannot start playback: {error}") from error
            raise

    def wait(self, timeout=None):
        finished = self._finished.wait(timeout)
        if finished and self.error:
            raise PlaybackError(self.error)
        return finished

    def stop(self):
        self.close()

    def request_stop(self):
        """Interrupt startup/producer waits without waiting for native cleanup."""
        self._stop.set()
        self._buffer.cancel()

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._clock.freeze()
            self.request_stop()
            try:
                if self._stream is not None:
                    try:
                        self._stream.abort()
                    finally:
                        self._stream.close()
            finally:
                try:
                    self._decoder.close()
                finally:
                    if self._feeder is not None:
                        self._feeder.join(timeout=0.5)
                    self._finished.set()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
