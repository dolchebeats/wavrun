import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from core.audio import AudioPlayer, PlaybackEvent, PlayerState
from core.audio.decoder import DecoderSession
from core.audio.engine import PrototypePlayer
from core import cli_playback
from test_audio_controls import ManualAudio
from test_audio_prototype import blocked_decoder, make_wave


class TUIPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.songs = []
        for index in range(2):
            path = self.folder / f"song-{index}.wav"
            make_wave(path, rate=48000, seconds=1)
            self.songs.append(dict(path=str(path), title=f"Song {index}", artist="Artist", length=1))
        self.backend = ManualAudio()
        self.player = AudioPlayer(backend=self.backend, volume=0,
                                  prebuffer_seconds=0.05, buffer_seconds=0.1)
        self.app = cli_playback.wavrun(player=self.player, config={"last_index": 0, "volume": 0},
                                      playlist=self.songs)
        self.config_patch = patch.object(cli_playback, "save_config")
        self.playlist_patch = patch.object(cli_playback, "save_playlist_file")
        self.save_config = self.config_patch.start()
        self.save_playlist = self.playlist_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.addCleanup(self.playlist_patch.stop)
        self.context = self.app.run_test()
        self.pilot = await self.context.__aenter__()
        await self.pilot.pause()

    async def asyncTearDown(self):
        await self.context.__aexit__(None, None, None)

    async def until(self, condition, timeout=5):
        async def wait():
            while not condition():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout)

    async def play(self, index=0):
        await self.app.action_play_index(index)
        await self.until(lambda: self.backend.stream is not None and self.backend.stream.active
                         and self.app.current_song_path == self.app.playlist[index]["path"])

    async def drain(self):
        stream = self.backend.stream
        async def finish():
            while stream.active:
                stream.pump(1024)
                await asyncio.sleep(0.001)
        await asyncio.wait_for(finish(), 5)

    async def test_play_pause_seek_volume_and_quit(self):
        await self.play()
        await self.app.action_play_pause()
        await self.until(lambda: self.app.paused)
        old_id = self.player.session_id
        await self.app.action_seek_forward()
        await self.until(lambda: self.player.session_id != old_id and self.player.state == PlayerState.PAUSED
                         and self.player.get_pos() == 1000)
        await self.app.action_volume_up()
        await self.app.action_quit()
        self.assertEqual(self.player.state, PlayerState.CLOSED)
        self.assertEqual(self.save_config.call_args.args[0]["volume"], 0.05)

    async def test_keyboard_controls_and_visible_buttons(self):
        await self.play()
        self.app.list_view.focus()
        await self.pilot.press("space")
        await self.until(lambda: self.app.paused)
        previous = self.player.session_id
        await self.pilot.press("right")
        await self.until(lambda: self.player.session_id != previous and
                         self.player.state == PlayerState.PAUSED and self.player.get_pos() == 1000)
        await self.pilot.press("+")
        self.assertEqual(self.app.volume, 0.05)
        for button in (self.app.btn_prev, self.app.btn_play, self.app.btn_next,
                       self.app.btn_shuffle, self.app.btn_repeat):
            self.assertLessEqual(button.region.right, self.app.size.width)

    async def test_end_advances_full_library_when_filtered(self):
        self.app.search.value = "Song 0"
        await self.pilot.pause()
        await self.play()
        old_id = self.player.session_id
        await self.drain()
        await self.until(lambda: self.player.session_id != old_id and
                         self.app.current_song_path == self.songs[1]["path"] and self.backend.stream.active)
        self.assertEqual(self.app.playlist, [self.songs[0]])
        self.assertIsNone(self.app.list_view.index)
        self.assertIn("not in filter", str(self.app.lbl_title.render()))

    async def test_final_track_displays_completed_progress(self):
        await self.play(1)
        await self.drain()
        await self.until(lambda: not self.app.playing)
        self.assertEqual(str(self.app.lbl_pos.render()), "00:01")
        self.assertEqual(self.app.progress.progress, 100)

    async def test_repeat_hidden_track_and_ignore_old_end(self):
        await self.play(1)
        self.app.repeat_mode = "one"
        self.app.search.value = "Song 0"
        await self.pilot.pause()
        old_id = self.player.session_id
        await self.drain()
        await self.until(lambda: self.player.session_id != old_id and self.backend.stream.active)
        new_id = self.player.session_id
        old_event = PlaybackEvent("end", old_id, PlayerState.STOPPED, self.songs[1]["path"], 1000, 1000)
        await self.app.on_engine_event(cli_playback.EngineEvent(old_event))
        self.assertEqual(self.player.session_id, new_id)
        self.assertEqual(self.app.current_song_path, self.songs[1]["path"])

    async def test_folder_scan_and_old_playback_do_not_cancel_change(self):
        new_folder = self.folder / "new"
        new_folder.mkdir()
        new_path = new_folder / "new.wav"
        make_wave(new_path)
        songs = [dict(self.songs[0], path=str(new_path), title="New")]
        started, release = threading.Event(), threading.Event()
        def scan(*args, **kwargs):
            started.set()
            release.wait(10)
            return songs
        with patch.object(cli_playback, "scan_folder", side_effect=scan):
            change = asyncio.create_task(self.app.apply_folder(str(new_folder)))
            try:
                await self.until(started.is_set)
                await self.play()
                self.assertIn("Scanning", str(self.app.status.render()))
            finally:
                release.set()
                await change
        self.assertEqual(self.app.music_dir, str(new_folder))
        self.assertEqual(self.app.full_playlist, songs)
        self.assertFalse(self.app.playing)
        await self.play()
        self.assertEqual(self.app.current_song_path, str(new_path))

    async def test_rapid_selection_ignores_replaced_row(self):
        row = self.app.list_view.children[0]
        await self.app.action_play_index(0)
        await self.app.action_play_index(1)
        await self.until(lambda: self.app.current_song_path == self.songs[1]["path"] and
                         self.backend.stream is not None and self.backend.stream.active)
        self.app.search.value = "Song 1"
        await self.pilot.pause()
        await self.app.on_list_view_selected(cli_playback.ListView.Selected(self.app.list_view, row, 0))
        self.assertEqual(self.app.current_song_path, self.songs[1]["path"])

    async def test_corrupt_audio_shows_error_and_can_retry(self):
        path = self.folder / "bad.mp3"
        path.write_bytes(b"bad")
        self.app.playlist = [dict(self.songs[0], path=str(path))]
        self.app.full_playlist = self.app.playlist.copy()
        await self.app.action_play_index(0)
        await self.until(lambda: "Playback failed" in str(self.app.status.render()))
        self.assertFalse(self.app.playing)
        self.app.full_playlist = self.songs.copy()
        self.app.playlist = self.songs.copy()
        await self.play()

    async def test_quit_interrupts_startup_waiting_on_network_decoder(self):
        created = []
        def factory(*args, **kwargs):
            session = PrototypePlayer(*args, **kwargs)
            session._decoder.close()
            session._decoder = DecoderSession(self.songs[0]["path"], session.format, _worker=blocked_decoder)
            created.append(session)
            return session
        self.player._factory = factory
        await self.app.action_play_index(0)
        await self.until(lambda: created and created[0]._decoder.alive)
        await asyncio.wait_for(self.app.action_quit(), 3)
        self.assertEqual(self.player.state, PlayerState.CLOSED)
        self.assertFalse(created[0]._decoder.alive)

    async def test_invalid_folder_and_failed_save_preserve_app(self):
        await self.app.apply_folder(str(self.folder / "absent"))
        self.assertEqual(self.app.full_playlist, self.songs)
        self.save_playlist.side_effect = OSError("disk full")
        await self.app.action_quit()
        self.assertFalse(self.app.stop_threads)
        self.assertIn("Save failed", str(self.app.status.render()))
        self.assertNotEqual(self.player.state, PlayerState.CLOSED)
