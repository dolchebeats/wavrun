"""Decode in an isolated, explicitly spawned process on every platform."""
from dataclasses import dataclass
import multiprocessing
import math
import threading


class DecoderError(RuntimeError):
    pass


@dataclass(frozen=True)
class PCMFormat:
    rate: int = 48000
    channels: int = 2

    def __post_init__(self):
        if type(self.rate) is not int or not 8000 <= self.rate <= 384000:
            raise ValueError("Sample rate must be an integer between 8000 and 384000")
        if type(self.channels) is not int or self.channels not in (1, 2):
            raise ValueError("Output must be mono or stereo")


def _decode_worker(path, output, sender, cancelled, start_seconds=0):
    # Native decoder objects are created, used, and released in this process.
    try:
        import av
        import numpy as np

        with av.open(path, mode="r") as container:
            if not container.streams.audio:
                raise DecoderError("File contains no audio stream")
            stream = container.streams.audio[0]
            duration = None
            if stream.duration is not None and stream.time_base is not None:
                duration = float(stream.duration * stream.time_base)
            elif container.duration is not None:
                duration = container.duration / av.time_base
            sender.send(("info", {"duration": duration, "codec": stream.codec_context.name,
                                   "rate": output.rate, "channels": output.channels,
                                   "start_seconds": start_seconds}))
            origin = float(stream.start_time * stream.time_base) if stream.start_time is not None else 0
            target = origin + start_seconds
            if start_seconds > 0:
                if duration is not None and start_seconds >= duration:
                    sender.send(("eof", 0))
                    return
                container.seek(int(target / stream.time_base), stream=stream,
                               backward=True, any_frame=False)
            resampler = av.AudioResampler(format="fltp", rate=output.rate,
                                         layout="mono" if output.channels == 1 else "stereo")
            total = 0

            def send_frames(frames):
                nonlocal total
                for frame in frames:
                    samples = np.ascontiguousarray(frame.to_ndarray().T, dtype=np.float32)
                    if start_seconds > 0:
                        if frame.pts is None or frame.time_base is None:
                            raise DecoderError("Cannot seek accurately without audio timestamps")
                        frame_time = float(frame.pts * frame.time_base)
                        skip = max(0, math.ceil((target - frame_time) * output.rate - 1e-6))
                        samples = samples[min(skip, len(samples)):]
                    # Small, bounded messages keep pipe and buffer memory bounded.
                    for start in range(0, len(samples), 1024):
                        if cancelled.is_set():
                            return False
                        chunk = samples[start:start + 1024]
                        sender.send(("pcm", chunk.tobytes()))
                        total += len(chunk)
                return True

            for frame in container.decode(stream):
                if cancelled.is_set() or not send_frames(resampler.resample(frame)):
                    return
            if cancelled.is_set() or not send_frames(resampler.resample(None)):
                return
            if not total and start_seconds == 0:
                raise DecoderError("File produced no audio samples")
            sender.send(("eof", total))
    except (BrokenPipeError, EOFError, OSError) as error:
        if not cancelled.is_set():
            try:
                sender.send(("error", f"{type(error).__name__}: {error}"[:1024]))
            except (OSError, EOFError):
                pass
    except Exception as error:
        try:
            sender.send(("error", f"{type(error).__name__}: {error}"[:1024]))
        except (OSError, EOFError):
            pass
    finally:
        sender.close()


class DecoderSession:
    """One decoder per track; closing also handles blocked native/network reads."""

    def __init__(self, path, output=PCMFormat(), *, start_seconds=0, _worker=_decode_worker):
        if not math.isfinite(start_seconds) or start_seconds < 0:
            raise ValueError("Seek position must be a nonnegative finite number")
        self.path = str(path)
        self.output = output
        self._context = multiprocessing.get_context("spawn")
        self._cancelled = self._context.Event()
        self._receiver, self._sender = self._context.Pipe(duplex=False)
        self._process = self._context.Process(target=_worker, name="wavrun-decoder",
                                             args=(self.path, output, self._sender, self._cancelled, start_seconds),
                                             daemon=True)
        self._started = False
        self._closed = False
        self._close_lock = threading.RLock()

    def start(self):
        with self._close_lock:
            if self._closed or self._started:
                raise DecoderError("Decoder session cannot be started twice")
            try:
                self._process.start()
            except Exception:
                self.close()
                raise
            self._started = True
            self._sender.close()
        return self

    def receive(self, timeout=0.1):
        if not self._started or self._closed:
            raise DecoderError("Decoder is not running")
        try:
            if self._receiver.poll(timeout):
                return self._receiver.recv()
            if not self._process.is_alive():
                raise DecoderError(f"Decoder exited without completing (code {self._process.exitcode})")
            return None
        except (EOFError, OSError) as error:
            raise DecoderError("Decoder connection closed unexpectedly") from error

    @property
    def alive(self):
        return not self._closed and self._started and self._process.is_alive()

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._cancelled.set()
            if self._started:
                self._process.join(timeout=0.3)
                if self._process.is_alive():
                    self._process.terminate()
                    self._process.join(timeout=0.5)
                if self._process.is_alive():
                    self._process.kill()
                    self._process.join(timeout=0.5)
                if self._process.is_alive():
                    raise DecoderError("Decoder process did not shut down")
            self._closed = True
            self._receiver.close()
            self._sender.close()
            self._process.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()
