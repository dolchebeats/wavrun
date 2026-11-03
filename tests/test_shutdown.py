import asyncio
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.background import background_read
from core.playlist import ScanCancelled, scan_folder
from core.storage import APP_DIR


class BackgroundTests(unittest.IsolatedAsyncioTestCase):
    async def test_results_and_errors_reach_caller(self):
        self.assertEqual(await background_read(lambda: 42), 42)
        def fail():
            raise OSError("unavailable")
        with self.assertRaises(OSError):
            await background_read(fail)

    async def test_cancellation_signals_reader_and_ignores_late_result(self):
        release = threading.Event()
        cancelled = threading.Event()
        finished = threading.Event()
        def read():
            release.wait(2)
            finished.set()
            return 42
        task = asyncio.create_task(background_read(read, cancelled))
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(cancelled.is_set())
        finally:
            release.set()
        while not finished.is_set():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)


class ShutdownTests(unittest.TestCase):
    def test_process_exits_after_quit_with_scan_blocked(self):
        # A subprocess detects threads left behind after asyncio.run returns.
        script = '''
import asyncio, threading
from unittest.mock import Mock, patch
from core import cli_playback
started = threading.Event()
def blocked_scan(folder, cancel_event=None):
    started.set()
    threading.Event().wait(3600)
async def main():
    player = Mock()
    with patch.object(cli_playback, 'AudioPlayer', return_value=player), \\
         patch.object(cli_playback, 'load_config', return_value={'music_dir': '.'}), \\
         patch.object(cli_playback, 'load_playlist_file', return_value=[]), \\
         patch.object(cli_playback, 'save_config'), \\
         patch.object(cli_playback, 'save_playlist_file'), \\
         patch.object(cli_playback, 'scan_folder', side_effect=blocked_scan):
        app = cli_playback.wavrun()
        async with app.run_test() as pilot:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
            await app.action_quit()
        player.close.assert_called_once()
asyncio.run(main())
print('clean exit')
'''
        result = subprocess.run([sys.executable, "-c", script], cwd=APP_DIR,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("clean exit", result.stdout)

    def test_scan_stops_between_metadata_reads(self):
        cancelled = threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "first.mp3").touch()
            (Path(folder) / "second.mp3").touch()
            def metadata(path):
                cancelled.set()
                return {"title": "Song", "artist": "Artist", "length": 10}
            with patch("core.playlist.get_metadata", side_effect=metadata) as read:
                with self.assertRaises(ScanCancelled):
                    scan_folder(folder, cancel_event=cancelled)
            self.assertEqual(read.call_count, 1)

if __name__ == "__main__":
    unittest.main()
