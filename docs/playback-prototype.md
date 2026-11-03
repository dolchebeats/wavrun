# Wavrun playback engine

The Textual player and standalone audio CLI use this PyAV / NumPy / sounddevice
playback engine. Neither imports VLC or requires VLC or the `ffmpeg` command installed.
PyAV binary wheels supply the FFmpeg libraries.

## Install and run

With your virtual environment activated, use Python 3.12 or newer with compatible
binary wheels:

```sh
python -m pip install --only-binary=:all: -r requirements.txt
python main.py
python -m core.audio --list-devices
python -m core.audio "path/to/song.flac"
```

Windows paths and mounted network-share paths work as file arguments. On Linux,
sounddevice may require the system PortAudio library; for example, Debian/Ubuntu
provide it in `libportaudio2`. Windows/macOS pip packages include PortAudio.

Optional commands:

```sh
python -m core.audio "path/to/song.mp3" --volume 0.5 --seconds 5
python -m core.audio "path/to/song.m4a" --device 3
python -m core.audio "path/to/song.ogg" --decode-only
python -m core.audio "path/to/song.flac" --interactive
python -m core.audio "path/to/song.mp3" --seek 30 --seconds 5
```

`--decode-only` opens no output device and reports codec, output format, frame
count, duration, and peak sample level. Ctrl+C stops normal playback and releases
the stream and decoder process. `--seconds` limits playback after startup.

Interactive mode accepts one command per line: `pause`, `resume`, `stop`,
`seek SECONDS`, `volume 0..1`, `load PATH`, `status`, and `quit`. It remains open
after a track ends, allowing replay or loading another track. `status` reports
state, position/duration in milliseconds, volume, and the last error. File paths
after `load` may contain spaces and Windows backslashes, optionally in double quotes.

## Public playback API

```python
from core.audio import AudioPlayer, PlayerState, PlaybackError

with AudioPlayer(volume=0.7) as player:
    player.add_event_callback(lambda event: print(event.kind, event.state, event.session_id))
    player.load("path/to/song.flac")
    player.play()
    player.pause()
    player.set_time(30_000)  # stays paused; seek position is in milliseconds
    player.set_volume(0.5)
    player.unpause()
    position_ms = player.get_pos()
    duration_ms = player.get_length()  # None if duration is unknown
    player.wait()  # raises PlaybackError if playback fails
```

`load`, `play`, seek, and native device cleanup are synchronous and belong in a
UI worker. Concurrent stop/load/seek invalidate older requests before waiting
for cleanup; a superseded play/seek returns `False`. A successful startup returns
`True`. The Textual adapter runs these operations in workers, marshals engine
events back through Textual messages, and cancels pending startup before track
replacement. `cancel_pending()` invalidates startup without native I/O; a
subsequent load/stop/close retires the cancelled session's resources.

States are `stopped`, `loading`, `buffering`, `playing`, `paused`, `error`, and
`closed`. Typed `PlaybackEvent` notifications have kind `state`, `end`, or `error`,
plus session ID, path, position, duration, and error text. Notifications are delivered
on a dedicated event thread; listeners should return promptly and marshal UI work
to their UI thread. Queued events from superseded sessions are discarded. An event
callback already executing cannot be undone; asynchronous consumers should retain
the event's session ID. State notifications may be coalesced under queue pressure.

Compatibility helpers `add_end_callback(callback)` and `add_error_callback(callback)`
invoke callbacks with no arguments. Natural completion produces one end event after
audio drains. Stop, close, seek cancellation, and track replacement do not produce
an end event. Callback exceptions are logged without stopping playback.

Pause stops consumption and drains audio already queued to the device before
returning; resume retains the decoder and PCM buffer. Seeking creates a new decoder
and buffer, seeks backward to a decodable location, and discards samples preceding
the requested timestamp. A paused seek remains paused. Seek values are clamped to
zero and the known duration; a seek on a stopped track sets its next start position.
Inputs that cannot seek or lack timestamps produce a clear error.

## Implementation

- Each track has a decoder process created with the `spawn` context on every OS.
  Native decoder state remains in that process.
- PyAV converts source audio to interleaved float32 mono/stereo PCM at a rate
  supported by the selected output device. Decoder and resampler tails are flushed.
- Small pipe messages and a fixed-size PCM ring limit buffered memory. The decoder
  naturally waits when the buffer and pipe are full.
- A feeder thread moves PCM from IPC into the ring. The audio callback only copies
  samples with gain applied; it never decodes, accesses files, logs, or waits for
  the producer. Missing data produces silence and increments an underrun counter.
- Output starts after prebuffering, or when a shorter track is fully decoded.
  Completion is signaled after the final output block drains.
- Stop/close abort output, wake blocked buffer writes, and close the decoder.
  A decoder stuck in native/network I/O is terminated after a bounded grace period.
  Each new track gets a fresh process and pipe.

The reusable `AudioPlayer` owns single-track `PrototypePlayer` output sessions.
PortAudio DAC timestamps and a bounded output history estimate the audible position,
including queued device latency. Progress freezes on pause and does not advance
through underrun silence. Accuracy depends on host/device timestamp reporting;
without timestamps the stream's reported latency is used. `frames_played` separately
counts samples submitted to output and is not the audible position clock.
Live volume changes apply to subsequently rendered blocks. Seeking flushes old
output and may briefly silence playback while the new decoder buffers.
Mid-track network reads can stall until stopped; silence and `buffering` state make
that visible. Device failures produce an error event; automatic device reconnection
and seamless crossfades are not implemented.

## Validation

```sh
python -m unittest discover -s tests -p "test_audio*.py" -v
```

Tests generate WAV, FLAC, MP3, OGG/Vorbis, and M4A/AAC fixtures and decode them in
real spawned processes. They also cover resampling, mono conversion, sample level,
bounded buffering, final partial blocks, short tracks, corrupt/missing files,
simulated output, and terminating a stuck decoder. No speakers are used by tests.
Stage 2 tests cover pause/resume continuity, precise PCM seeks, compressed-format
seeks, live volume, latency-aware clocks, error/end events, device disconnection,
rapid seek/track replacement, cancellation during startup, and interactive commands.

The GitHub Actions workflow runs these checks on Windows, macOS, and Linux with
Python 3.12 and 3.14. A configured matrix is not evidence that those runners passed;
real-device playback must also be checked separately on all three platforms.

Local validation completed on Windows with Python 3.15.2, PyAV 19.0.2, NumPy 2.5.4,
and sounddevice 0.5.6. The stage 1 native default-device probe played a 0.1-second
silent WAV at zero volume, delivered all 4,410 frames, and reported zero underruns.
A stage 2 native zero-volume check also passed pause, stable paused position,
seek-while-paused, resume, completion at the full track duration, and clean exit with
zero underruns. Audible quality and native macOS/Linux playback still require testing.

The TUI supports seek and volume shortcuts, background folder refresh, generation-
checked playback events, and per-user config/data/log paths with legacy settings
fallback. See [distribution.md](distribution.md) for bundle creation, collected
notices, and remaining public-release validation/source-material requirements.
