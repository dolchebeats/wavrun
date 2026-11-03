import tempfile
import io
import json
import threading
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import redirect_stdout

import numpy as np

from core.audio import AudioPlayer, PlayerState
from core.audio.__main__ import control_command, main
from core.audio.clock import OutputClock
from core.audio.decoder import DecoderSession, PCMFormat
from core.audio.engine import PlaybackError, PrototypePlayer
from test_audio_prototype import FakeAudio, make_wave, transcode, blocked_decoder


def eventually(condition, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError("Condition did not become true")


class ManualAudio(FakeAudio):
    """Explicit output ticks allow deterministic pause, seek, and end tests."""
    def OutputStream(self, **settings):
        backend = self
        class Stream:
            active = False
            latency = 0
            closed = False

            def start(self):
                self.active = True

            def stop(self):
                self.active = False
                settings["finished_callback"]()

            def abort(self):
                self.active = False
                settings["finished_callback"]()

            def close(self):
                self.closed = True
                self.active = False

            def pump(self, frames=480):
                if not self.active:
                    return None
                output = np.empty((frames, settings["channels"]), dtype=np.float32)
                try:
                    settings["callback"](output, frames, None,
                                         SimpleNamespace(output_underflow=False))
                except (backend.CallbackStop, backend.CallbackAbort):
                    self.active = False
                    backend.finish_count += 1
                    settings["finished_callback"]()
                backend.outputs.append(output.copy())
                return output

            def disconnect(self):
                self.active = False
                settings["finished_callback"]()
        self.stream = Stream()
        return self.stream


class ClockTests(unittest.TestCase):
    def test_clock_accounts_for_multiple_queued_buffers_and_silence(self):
        now = [0.0]
        clock = OutputClock(1000, monotonic=lambda: now[0])
        clock.record(100, SimpleNamespace(currentTime=10, outputBufferDacTime=10.2))
        now[0] = 0.1
        clock.record(100, SimpleNamespace(currentTime=20, outputBufferDacTime=20.2))
        self.assertEqual(clock.position(), 0)
        now[0] = 0.25
        self.assertAlmostEqual(clock.position(), 50, delta=1)
        now[0] = 0.35
        self.assertAlmostEqual(clock.position(), 150, delta=1)
        now[0] = 0.5
        self.assertEqual(clock.position(), 200)
        clock.record(0)
        now[0] = 0.8
        self.assertEqual(clock.position(), 200)

    def test_pause_freezes_and_resume_starts_at_drained_position(self):
        now = [0.0]
        clock = OutputClock(1000, monotonic=lambda: now[0])
        clock.record(100)
        clock.freeze(100)
        now[0] = 10
        self.assertEqual(clock.position(), 100)
        clock.resume()
        clock.record(100)
        now[0] += 0.05
        self.assertAlmostEqual(clock.position(), 150, delta=1)


class ControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.temporary.name)
        cls.wav = cls.folder / "tone.wav"
        make_wave(cls.wav, rate=48000, seconds=1)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.backend = ManualAudio()
        self.player = AudioPlayer(backend=self.backend, volume=1,
                                  buffer_seconds=0.1, prebuffer_seconds=0.05)
        self.addCleanup(self.player.close)
        self.player.load(self.wav)

    def drain(self):
        deadline = time.monotonic() + 5
        while self.backend.stream.active and time.monotonic() < deadline:
            self.backend.stream.pump(1024)
            time.sleep(0.001)
        self.assertTrue(self.player.wait(2))

    def test_pause_resume_preserves_decoder_and_buffered_samples(self):
        self.player.play()
        session = self.player._session
        self.backend.stream.pump(960)
        self.assertTrue(self.player.pause())
        self.assertEqual(self.player.state, PlayerState.PAUSED)
        position = self.player.get_pos()
        self.assertEqual(position, 20)
        time.sleep(0.03)
        self.assertEqual(self.player.get_pos(), position)
        self.assertFalse(self.player.wait(0))
        self.assertTrue(self.player.unpause())
        self.assertIs(self.player._session, session)
        output = self.backend.stream.pump(480)
        with wave.open(str(self.wav), "rb") as source:
            source.setpos(960)
            expected = np.frombuffer(source.readframes(480), dtype="<i2").reshape(-1, 2) / 32768
        np.testing.assert_allclose(output, expected, atol=1e-6)

    def test_live_volume_does_not_restart_playback(self):
        self.player.play()
        generation = self.player.session_id
        self.player.set_volume(0)
        self.assertFalse(self.backend.stream.pump().any())
        self.player.set_volume(0.5)
        output = self.backend.stream.pump()
        self.assertAlmostEqual(float(np.abs(output).max()), 0.125, delta=0.01)
        self.assertEqual(self.player.session_id, generation)

    def test_seek_flushes_old_pcm_and_preserves_paused_state(self):
        self.player.play()
        old_session = self.player._session
        self.player.set_time(250)
        self.assertFalse(old_session._decoder.alive)
        self.assertEqual(self.player.get_pos(), 250)
        self.backend.stream.pump(480)
        self.player.pause()
        self.player.set_time(600)
        self.assertEqual(self.player.state, PlayerState.PAUSED)
        self.assertEqual(self.player.get_pos(), 600)
        self.assertFalse(self.backend.stream.active)
        self.player.unpause()
        output = self.backend.stream.pump(480)
        with wave.open(str(self.wav), "rb") as source:
            source.setpos(28800)
            expected = np.frombuffer(source.readframes(480), dtype="<i2").reshape(-1, 2) / 32768
        np.testing.assert_allclose(output, expected, atol=1e-6)

    def test_end_fires_once_after_output_drains_on_event_thread(self):
        ends = []
        events = []
        self.player.add_end_callback(lambda: ends.append(threading.current_thread().name))
        self.player.add_event_callback(events.append)
        self.player.play()
        self.assertEqual(self.player.get_length(), 1000)
        self.assertFalse(self.player.wait(0))
        self.drain()
        eventually(lambda: len(ends) == 1)
        self.assertEqual(ends, ["wavrun-audio-events"])
        self.assertEqual(self.player.get_pos(), 1000)
        self.assertEqual(self.player.state, PlayerState.STOPPED)
        self.assertEqual(len([event for event in events if event.kind == "end"]), 1)

    def test_seek_to_end_clamps_and_completes_once(self):
        self.player.play()
        self.player.set_time(5000)
        self.drain()
        self.assertEqual(self.player.get_pos(), 1000)

    def test_seek_after_end_updates_position_and_replays_from_target(self):
        self.player.play()
        self.drain()
        self.player.set_time(250)
        self.assertEqual(self.player.get_pos(), 250)
        self.player.play()
        self.assertEqual(self.player.get_pos(), 250)

    def test_seeking_to_end_while_paused_waits_for_resume(self):
        ends = []
        self.player.add_end_callback(lambda: ends.append(True))
        self.player.play()
        self.player.pause()
        self.player.set_time(1000)
        self.assertFalse(self.player.wait(0.03))
        self.assertEqual(self.player.state, PlayerState.PAUSED)
        self.assertFalse(ends)
        self.player.unpause()
        self.drain()
        eventually(lambda: len(ends) == 1)

    def test_stop_and_close_do_not_signal_natural_end(self):
        ends = []
        self.player.add_end_callback(lambda: ends.append(True))
        self.player.play()
        self.player.stop()
        self.assertEqual(self.player.state, PlayerState.STOPPED)
        self.assertEqual(self.player.get_pos(), 0)
        self.player.play()
        self.player.close()
        self.player.close()
        self.assertEqual(self.player.state, PlayerState.CLOSED)
        self.assertFalse(ends)

    def test_output_device_failure_reports_one_error(self):
        errors = []
        self.player.add_error_callback(lambda: errors.append(True))
        self.player.play()
        self.backend.stream.disconnect()
        with self.assertRaisesRegex(PlaybackError, "stopped unexpectedly"):
            self.player.wait(2)
        eventually(lambda: len(errors) == 1)
        self.assertEqual(self.player.state, PlayerState.ERROR)

    def test_corrupt_file_error_and_retry(self):
        errors = []
        self.player.add_error_callback(lambda: errors.append(True))
        path = self.folder / "bad.mp3"
        path.write_bytes(b"invalid audio")
        self.player.load(path)
        with self.assertRaises(PlaybackError):
            self.player.play()
        eventually(lambda: len(errors) == 1)
        self.assertEqual(self.player.state, PlayerState.ERROR)
        self.player.load(self.wav)
        self.assertTrue(self.player.play())
        self.assertIsNone(self.player.error)

    def test_old_queued_end_cannot_advance_new_track(self):
        entered, release = threading.Event(), threading.Event()
        ends = []
        def listener(event):
            if event.state == PlayerState.LOADING and not entered.is_set():
                entered.set()
                release.wait(5)
        self.player.add_event_callback(listener)
        self.player.add_end_callback(lambda: ends.append(True))
        try:
            self.player.play()
            self.assertTrue(entered.wait(1))
            self.drain()
            self.player.load(self.wav)
            self.player.play()
        finally:
            release.set()
        time.sleep(0.03)
        self.assertFalse(ends)
        self.assertNotEqual(self.player.state, PlayerState.STOPPED)

    def test_stop_cancels_inflight_startup_without_late_error(self):
        entered, release = threading.Event(), threading.Event()
        sessions = []
        def factory(*args, **kwargs):
            session = PrototypePlayer(*args, **kwargs)
            sessions.append(session)
            start = session.start
            def delayed():
                entered.set()
                while not release.wait(0.01) and not session._stop.is_set():
                    pass
                return start()
            session.start = delayed
            return session
        results = []
        self.player._factory = factory
        worker = threading.Thread(target=lambda: results.append(self.player.play()))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.player.stop()
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [False])
        self.assertEqual(self.player.state, PlayerState.STOPPED)
        self.assertFalse(sessions[0]._decoder.alive)
        self.assertIsNone(self.player.error)

    def test_rapid_seeks_cancel_the_older_startup(self):
        self.player.play()
        entered = threading.Event()
        created = []
        def factory(*args, **kwargs):
            session = PrototypePlayer(*args, **kwargs)
            created.append(session)
            original = session.start
            if kwargs["start_seconds"] == 0.2:
                def delayed():
                    entered.set()
                    session._stop.wait(3)
                    return original()
                session.start = delayed
            return session
        self.player._factory = factory
        results = []
        worker = threading.Thread(target=lambda: results.append(self.player.set_time(200)))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.player.set_time(700))
        finally:
            if created:
                created[0].request_stop()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [False])
        self.assertEqual(self.player.get_pos(), 700)
        self.assertFalse(created[0]._decoder.alive)

    def test_event_callback_can_load_next_track_without_deadlock(self):
        loaded = threading.Event()
        def on_end():
            self.player.load(self.wav)
            loaded.set()
        self.player.add_end_callback(on_end)
        self.player.play()
        self.drain()
        self.assertTrue(loaded.wait(2))
        self.assertEqual(self.player.state, PlayerState.STOPPED)

    def test_buffering_state_recovers_when_pcm_arrives(self):
        self.player.play()
        session = self.player._session
        with patch.object(session._buffer, "read_into", side_effect=lambda output, gain: (
                output.fill(0) or 0, False, None)):
            self.backend.stream.pump()
            eventually(lambda: self.player.state == PlayerState.BUFFERING)
        self.backend.stream.pump()
        eventually(lambda: self.player.state == PlayerState.PLAYING)

    def test_stop_terminates_a_real_decoder_stuck_in_native_read(self):
        created = []
        def factory(*args, **kwargs):
            session = PrototypePlayer(*args, **kwargs)
            session._decoder.close()
            session._decoder = DecoderSession(self.wav, session.format, _worker=blocked_decoder)
            created.append(session)
            return session
        self.player._factory = factory
        results = []
        worker = threading.Thread(target=lambda: results.append(self.player.play()))
        worker.start()
        try:
            eventually(lambda: created and created[0]._decoder.alive)
            start = time.monotonic()
            self.player.stop()
            self.assertLess(time.monotonic() - start, 3)
        finally:
            self.player.stop()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [False])
        self.assertFalse(created[0]._decoder.alive)

    def test_concurrent_track_change_waits_for_previous_output_to_close(self):
        original_open = self.backend.OutputStream
        def exclusive_open(**settings):
            if self.backend.stream is not None and not self.backend.stream.closed:
                raise RuntimeError("Old output device is still open")
            return original_open(**settings)
        self.backend.OutputStream = exclusive_open
        self.player.play()
        old_stream = self.backend.stream
        original_close = old_stream.close
        entered, release = threading.Event(), threading.Event()
        def slow_close():
            entered.set()
            release.wait(3)
            original_close()
        old_stream.close = slow_close
        results, errors = [], []
        def seek():
            try:
                results.append(self.player.set_time(200))
            except Exception as error:
                errors.append(error)
        def replace():
            try:
                self.player.load(self.wav)
                results.append(self.player.play())
            except Exception as error:
                errors.append(error)
        older = threading.Thread(target=seek)
        newer = threading.Thread(target=replace)
        older.start()
        try:
            self.assertTrue(entered.wait(1))
            newer.start()
            time.sleep(0.02)
        finally:
            release.set()
            older.join(5)
            if newer.ident is not None:
                newer.join(5)
        self.assertFalse(older.is_alive() or newer.is_alive())
        self.assertFalse(errors)
        self.assertCountEqual(results, [False, True])

    def test_closed_state_event_and_rejecting_further_playback(self):
        events = []
        self.player.add_event_callback(events.append)
        self.player.close()
        self.assertTrue(any(event.state == PlayerState.CLOSED for event in events))
        with self.assertRaises(PlaybackError):
            self.player.play()

    def test_invalid_values_leave_valid_playback_unchanged(self):
        self.player.play()
        generation = self.player.session_id
        for value in (-1, 2, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.player.set_volume(value)
        for value in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.player.set_time(value)
        self.assertEqual(self.player.session_id, generation)
        self.assertEqual(self.player.volume, 1)

    def test_interactive_commands_control_the_engine(self):
        self.player.play()
        self.assertTrue(control_command(self.player, "pause"))
        self.assertEqual(self.player.state, PlayerState.PAUSED)
        control_command(self.player, "volume 0.25")
        control_command(self.player, "seek 0.5")
        self.assertEqual(self.player.get_pos(), 500)
        control_command(self.player, "resume")
        self.assertEqual(self.player.volume, 0.25)
        self.assertFalse(control_command(self.player, "quit"))

    def test_decode_only_cli_honors_initial_seek(self):
        output = io.StringIO()
        with redirect_stdout(output):
            result = main([str(self.wav), "--decode-only", "--seek", "0.25"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["frames"], 36000)

    def test_all_supported_codecs_can_seek_and_decode_forward(self):
        for extension, codec in (("wav", None), ("flac", "flac"), ("mp3", "libmp3lame"),
                                 ("ogg", "vorbis"), ("m4a", "aac")):
            with self.subTest(format=extension):
                path = self.wav if codec is None else self.folder / f"seek.{extension}"
                if codec:
                    transcode(self.wav, path, codec)
                count = 0
                with DecoderSession(path, PCMFormat(), start_seconds=0.4) as decoder:
                    deadline = time.monotonic() + 5
                    while True:
                        message = decoder.receive()
                        self.assertLess(time.monotonic(), deadline)
                        if message is None:
                            continue
                        kind, value = message
                        if kind == "pcm":
                            count += len(value) // 8
                        elif kind == "error":
                            self.fail(value)
                        elif kind == "eof":
                            break
                self.assertAlmostEqual(count / 48000, 0.6, delta=0.04)


if __name__ == "__main__":
    unittest.main()
