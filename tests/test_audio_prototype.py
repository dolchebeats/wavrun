import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np

from core.audio.__main__ import decode_only
from core.audio.buffer import PCMBuffer
from core.audio.decoder import DecoderError, DecoderSession, PCMFormat
from core.audio.engine import PlaybackError, PrototypePlayer, output_format

ROOT = Path(__file__).resolve().parents[1]


def blocked_decoder(path, output, sender, cancelled, start_seconds=0):
    # Deliberately ignores cancellation, like a blocked native network read.
    time.sleep(60)


def make_wave(path, rate=44100, channels=2, seconds=0.3):
    phase = np.arange(round(rate * seconds)) / rate
    samples = (np.sin(2 * np.pi * 440 * phase) * 8192).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
        output.writeframes(np.repeat(samples[:, None], channels, axis=1).tobytes())


def transcode(source, path, codec):
    with av.open(str(source)) as reader, av.open(str(path), "w") as writer:
        stream = writer.add_stream(codec, rate=44100)
        stream.layout = "stereo"
        stream.codec_context.format = stream.codec_context.codec.audio_formats[0]
        if codec == "vorbis":
            stream.codec_context.options = {"strict": "experimental"}
        resampler = av.AudioResampler(format=stream.codec_context.format.name,
                                     layout="stereo", rate=44100)
        for frame in reader.decode(audio=0):
            for converted in resampler.resample(frame):
                for packet in stream.encode(converted):
                    writer.mux(packet)
        for converted in resampler.resample(None):
            for packet in stream.encode(converted):
                writer.mux(packet)
        for packet in stream.encode(None):
            writer.mux(packet)


class FakeAudio:
    class CallbackStop(Exception):
        pass

    class CallbackAbort(Exception):
        pass

    def __init__(self):
        self.stream = None
        self.outputs = []
        self.finish_count = 0

    def query_devices(self, device=None, kind=None):
        return {"max_output_channels": 2, "default_samplerate": 48000}

    def check_output_settings(self, **kwargs):
        pass

    def OutputStream(self, **settings):
        backend = self
        class Stream:
            def __init__(self):
                self.stopped = threading.Event()
                self.thread = None

            def start(self):
                def run():
                    while not self.stopped.is_set():
                        block = np.empty((257, settings["channels"]), dtype=np.float32)
                        try:
                            settings["callback"](block, len(block), None,
                                                 SimpleNamespace(output_underflow=False))
                        except (backend.CallbackStop, backend.CallbackAbort):
                            backend.outputs.append(block.copy())
                            # Simulate the final samples draining before completion.
                            time.sleep(0.01)
                            backend.finish_count += 1
                            settings["finished_callback"]()
                            return
                        backend.outputs.append(block.copy())
                        time.sleep(0.001)
                self.thread = threading.Thread(target=run, daemon=True)
                self.thread.start()

            def abort(self):
                self.stopped.set()

            def close(self):
                if self.thread is not None:
                    self.thread.join(timeout=1)
        self.stream = Stream()
        return self.stream


class BufferTests(unittest.TestCase):
    def test_wraparound_gain_final_partial_block_and_silence(self):
        buffer = PCMBuffer(5, 1)
        buffer.write(np.arange(3, dtype=np.float32)[:, None])
        buffer.read_into(np.empty((2, 1), dtype=np.float32))
        buffer.write(np.arange(3, 7, dtype=np.float32)[:, None])
        buffer.finish()
        output = np.empty((8, 1), dtype=np.float32)
        count, eof, error = buffer.read_into(output, 0.5)
        self.assertEqual(count, 5)
        self.assertTrue(eof)
        self.assertIsNone(error)
        np.testing.assert_array_equal(output[:, 0], [1, 1.5, 2, 2.5, 3, 0, 0, 0])

    def test_reader_does_not_wait_for_busy_producer(self):
        buffer = PCMBuffer(5, 1)
        held, release = threading.Event(), threading.Event()
        def hold():
            with buffer._condition:
                held.set()
                release.wait(2)
        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(held.wait(1))
            output = np.ones((5, 1), dtype=np.float32)
            self.assertEqual(buffer.read_into(output), (0, False, None))
            self.assertFalse(output.any())
        finally:
            release.set()
            thread.join(1)

    def test_cancel_wakes_producer_with_full_buffer(self):
        buffer = PCMBuffer(4, 1)
        result = []
        thread = threading.Thread(target=lambda: result.append(
            buffer.write(np.ones((8, 1), dtype=np.float32))))
        thread.start()
        deadline = time.monotonic() + 1
        while buffer.available < 4 and time.monotonic() < deadline:
            time.sleep(0.001)
        buffer.cancel()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])


class DecoderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.folder = Path(cls.temporary.name)
        cls.wav = cls.folder / "tone.wav"
        make_wave(cls.wav)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_resamples_wave_and_preserves_audio_level(self):
        result = decode_only(self.wav)
        self.assertEqual(result["rate"], 48000)
        self.assertEqual(result["channels"], 2)
        self.assertAlmostEqual(result["frames"], 14400, delta=1)
        self.assertAlmostEqual(result["peak"], 0.25, delta=0.01)

    def test_mono_output(self):
        result = decode_only(self.wav, PCMFormat(44100, 1))
        self.assertEqual(result["channels"], 1)
        self.assertEqual(result["frames"], 13230)

    def test_real_decoding_of_all_existing_playlist_formats(self):
        for extension, codec in (("flac", "flac"), ("mp3", "libmp3lame"),
                                 ("ogg", "vorbis"), ("m4a", "aac")):
            with self.subTest(format=extension):
                path = self.folder / f"tone.{extension}"
                transcode(self.wav, path, codec)
                result = decode_only(path)
                self.assertAlmostEqual(result["decoded_seconds"], 0.3, delta=0.1)
                self.assertGreater(result["peak"], 0.1)

    def test_missing_corrupt_and_empty_audio_report_errors(self):
        corrupt = self.folder / "corrupt.mp3"
        corrupt.write_bytes(b"not audio")
        empty = self.folder / "empty.wav"
        make_wave(empty, seconds=0)
        for path in (self.folder / "missing.wav", corrupt, empty):
            with self.subTest(path=path), self.assertRaises(DecoderError):
                decode_only(path)

    def test_close_terminates_a_stuck_decoder_process(self):
        decoder = DecoderSession(self.wav, _worker=blocked_decoder).start()
        self.assertTrue(decoder.alive)
        started = time.monotonic()
        decoder.close()
        decoder.close()
        self.assertFalse(decoder.alive)
        self.assertLess(time.monotonic() - started, 3)

    def test_close_when_pipe_is_full(self):
        decoder = DecoderSession(self.wav).start()
        try:
            deadline = time.monotonic() + 5
            while decoder.receive() is None:
                self.assertLess(time.monotonic(), deadline)
            # Metadata arrives before PCM; leave the small pipe unread and full.
            time.sleep(0.1)
        finally:
            decoder.close()
        self.assertFalse(decoder.alive)

    def test_cli_decode_does_not_import_vlc(self):
        script = """
import sys
from core.audio.__main__ import main
result = main([sys.argv[1], '--decode-only'])
assert 'vlc' not in sys.modules
raise SystemExit(result)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.wav)],
                                cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["frames"], 14400)

    def test_fake_output_drains_all_samples_and_closes(self):
        backend = FakeAudio()
        with PrototypePlayer(self.wav, backend=backend, prebuffer_seconds=0.05,
                             buffer_seconds=0.1, volume=0.5) as player:
            self.assertTrue(player.wait(5))
            self.assertEqual(player.frames_played, 14400)
            self.assertEqual(backend.finish_count, 1)
            output = np.concatenate(backend.outputs)
            self.assertAlmostEqual(float(np.abs(output).max()), 0.125, delta=0.01)
        self.assertFalse(player._decoder.alive)
        self.assertFalse(backend.stream.thread.is_alive())

    def test_start_failure_cleans_up_decoder(self):
        backend = FakeAudio()
        backend.OutputStream = lambda **kwargs: (_ for _ in ()).throw(OSError("No output device"))
        player = PrototypePlayer(self.wav, backend=backend)
        with self.assertRaisesRegex(PlaybackError, "No output device"):
            player.start()
        self.assertFalse(player._decoder.alive)
        player.close()

    def test_short_track_starts_without_filling_prebuffer(self):
        backend = FakeAudio()
        with PrototypePlayer(self.wav, backend=backend, prebuffer_seconds=1,
                             buffer_seconds=2) as player:
            self.assertTrue(player.wait(5))
            self.assertEqual(player.frames_played, 14400)

    def test_underflow_outputs_silence_and_reports_it(self):
        backend = FakeAudio()
        player = PrototypePlayer(self.wav, backend=backend)
        try:
            output = np.ones((128, 2), dtype=np.float32)
            player._callback(output, 128, None, SimpleNamespace(output_underflow=False))
            self.assertFalse(output.any())
            self.assertEqual(player.underruns, 1)
            self.assertEqual(player.frames_played, 0)
        finally:
            player.close()

    def test_early_stop_releases_active_output_and_decoder(self):
        backend = FakeAudio()
        player = PrototypePlayer(self.wav, backend=backend, prebuffer_seconds=0.05)
        player.start()
        player.stop()
        self.assertFalse(player._decoder.alive)
        self.assertFalse(backend.stream.thread.is_alive())
        self.assertTrue(player.wait(0))

    def test_mono_source_and_surround_source_convert_to_stereo(self):
        for channels in (1, 6):
            with self.subTest(channels=channels):
                path = self.folder / f"channels-{channels}.wav"
                make_wave(path, channels=channels)
                result = decode_only(path)
                self.assertEqual(result["channels"], 2)
                self.assertEqual(result["frames"], 14400)
                self.assertGreater(result["peak"], 0.1)


class DeviceTests(unittest.TestCase):
    def test_negotiation_falls_back_to_mono_and_another_rate(self):
        backend = FakeAudio()
        def check(**settings):
            if settings["channels"] != 1 or settings["samplerate"] != 44100:
                raise ValueError("Unsupported")
        backend.check_output_settings = check
        self.assertEqual(output_format(backend), PCMFormat(44100, 1))

    def test_missing_output_is_a_clear_error(self):
        backend = FakeAudio()
        backend.query_devices = lambda *args, **kwargs: {"max_output_channels": 0}
        with self.assertRaisesRegex(PlaybackError, "no audio outputs"):
            output_format(backend)


if __name__ == "__main__":
    unittest.main()
