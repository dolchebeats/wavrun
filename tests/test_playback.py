import logging
import tempfile
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

# Importing the UI normally opens the application's debug log.
with patch.object(logging, "basicConfig"):
    from core import cli_playback
from core.player import VLCMusic
from core.playlist import scan_folder
from core.metadata import get_metadata


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.songs = []
        for name in ("first", "second"):
            path = folder / f"{name}.mp3"
            path.touch()
            self.songs.append({"path": str(path), "title": name,
                               "artist": "Artist", "length": 10})
        self.player = Mock()
        self.player.get_pos.return_value = 0
        self.player.get_length.return_value = 10000
        self.stack.enter_context(patch.object(cli_playback, "VLCMusic", return_value=self.player))
        self.stack.enter_context(patch.object(cli_playback, "load_config",
                                             return_value={"last_index": 1}))
        self.stack.enter_context(patch.object(cli_playback, "load_playlist_file",
                                             return_value=self.songs.copy()))
        self.save_config = self.stack.enter_context(patch.object(cli_playback, "save_config"))
        self.save_playlist = self.stack.enter_context(patch.object(cli_playback, "save_playlist_file"))
        self.app = cli_playback.wavrun()
        # Keep audio and background polling out of these UI transition tests.
        self.app.progress_updater = Mock()
        self.app.progress_updater.is_alive.return_value = True
        self.session = self.app.run_test()
        self.pilot = await self.session.__aenter__()
        await self.pilot.pause()

    async def asyncTearDown(self):
        await self.session.__aexit__(None, None, None)

    async def test_play_loads_saved_track_and_only_unpauses_when_paused(self):
        await self.app.action_play_pause()
        self.player.load.assert_called_once_with(self.songs[1]["path"])
        self.player.play.assert_called_once()
        self.player.unpause.assert_not_called()
        self.assertTrue(self.app.playing)
        await self.app.action_play_pause()
        self.assertTrue(self.app.paused)
        await self.app.action_play_pause()
        self.player.unpause.assert_called_once()
        self.player.load.assert_called_once()

    async def test_saved_track_can_start_while_hidden_by_search(self):
        self.app.search.value = "first"
        await self.pilot.pause()
        self.assertEqual(len(self.app.playlist), 1)
        await self.app.action_play_pause()
        self.player.load.assert_called_once_with(self.songs[1]["path"])

    async def test_play_with_empty_library_does_not_claim_playback(self):
        self.app.playlist = []
        self.app.full_playlist = []
        self.app.current_song_path = None
        await self.app.action_play_pause()
        self.player.play.assert_not_called()
        self.player.unpause.assert_not_called()
        self.assertFalse(self.app.playing)

    async def test_folder_change_stops_and_resets_before_saving(self):
        await self.app.action_play_pause()
        old_end_callback = self.player.add_end_callback.call_args.args[0]
        self.app.progress.progress = 75
        with tempfile.TemporaryDirectory() as empty_folder:
            await self.app.apply_folder(empty_folder)
        self.player.stop.assert_called()
        self.assertFalse(self.app.playing)
        self.assertFalse(self.app.paused)
        self.assertIsNone(self.app.current_song_path)
        self.assertIsNone(self.app.current_song_length)
        old_end_callback()
        await self.pilot.pause()
        self.assertFalse(self.app.playing)
        self.assertEqual(self.app.progress.progress, 0)
        self.assertEqual(self.app.full_playlist, [])
        self.save_playlist.assert_called_once_with([])
        self.assertEqual(self.save_config.call_args.args[0]["last_index"], 0)

    async def test_invalid_folder_preserves_library_and_playback(self):
        await self.app.action_play_pause()
        self.player.reset_mock()
        old_path = self.app.current_song_path
        old_folder = self.app.music_dir
        await self.app.apply_folder(str(Path(self.songs[0]["path"]).parent / "missing"))
        self.assertEqual(self.app.full_playlist, self.songs)
        self.assertEqual(self.app.music_dir, old_folder)
        self.assertEqual(self.app.current_song_path, old_path)
        self.assertTrue(self.app.playing)
        self.player.stop.assert_not_called()
        self.save_config.assert_not_called()
        self.save_playlist.assert_not_called()

    async def test_metadata_is_displayed_literally(self):
        self.songs[1].update(title="A[/title]", artist="[red]Artist[/red]")
        self.app._render_playlist()
        await self.app.action_play_pause()
        await self.pilot.pause()
        self.assertEqual(str(self.app.lbl_title.render()), "A[/title]")
        self.assertEqual(str(self.app.lbl_artist.render()), "[red]Artist[/red]")
        labels = self.app.list_view.query("Label")
        self.assertIn("A[/title]", str(labels.last().render()))

    async def test_immediate_failure_stops_and_allows_retry(self):
        self.player.play.side_effect = RuntimeError("VLC rejected the file")
        await self.app.action_play_pause()
        self.assertFalse(self.app.playing)
        self.assertFalse(self.app.paused)
        self.assertIn("Playback failed", str(self.app.status.render()))
        self.player.play.side_effect = None
        await self.app.action_play_pause()
        self.assertTrue(self.app.playing)
        self.assertEqual(self.player.load.call_count, 2)
        self.assertEqual(str(self.app.status.render()), "Playing")

    async def test_async_failure_stops_without_repeat_loop(self):
        self.app.repeat_mode = "all"
        await self.app.action_play_pause()
        error_callback = self.player.add_error_callback.call_args.args[0]
        error_callback()
        await self.pilot.pause()
        self.assertFalse(self.app.playing)
        self.assertFalse(self.app.paused)
        self.assertEqual(self.player.play.call_count, 1)

    async def test_old_error_notification_does_not_stop_new_track(self):
        await self.app.action_play_pause()
        old_callback = self.player.add_error_callback.call_args.args[0]
        await self.app.action_play_index(0)
        old_callback()
        await self.pilot.pause()
        self.assertTrue(self.app.playing)
        self.assertEqual(self.app.current_song_path, self.songs[0]["path"])

    async def test_filtered_auto_advance_keeps_view_and_clears_highlight(self):
        self.app.search.value = "first"
        await self.pilot.pause()
        await self.app.action_play_index(0)
        self.player.add_end_callback.call_args.args[0]()
        await self.pilot.pause()
        self.assertEqual(self.app.playlist, [self.songs[0]])
        self.assertEqual(self.app.current_song_path, self.songs[1]["path"])
        self.assertIsNone(self.app.list_view.index)
        self.assertIn("not in filter", str(self.app.lbl_title.render()))
        await self.app.action_clear_search()
        await self.pilot.pause()
        self.assertEqual(self.app.list_view.index, 1)
        self.assertEqual(str(self.app.lbl_title.render()), "second")

    async def test_repeat_hidden_track_preserves_filter(self):
        self.app.search.value = "first"
        await self.pilot.pause()
        await self.app.action_play_pause()
        self.app.repeat_mode = "one"
        self.player.add_end_callback.call_args.args[0]()
        await self.pilot.pause()
        self.assertEqual(self.app.playlist, [self.songs[0]])
        self.assertEqual(self.app.current_song_path, self.songs[1]["path"])
        self.assertIsNone(self.app.list_view.index)
        self.assertEqual(self.player.play.call_count, 2)

    async def test_filtering_while_paused_keeps_play_control(self):
        await self.app.action_play_pause()
        await self.app.action_play_pause()
        self.app.search.value = "first"
        await self.pilot.pause()
        self.assertTrue(self.app.paused)
        self.assertEqual(str(self.app.btn_play.label), "\u25b6")

    async def test_missing_files_stop_with_repeat_and_shuffle(self):
        for song in self.songs:
            Path(song["path"]).unlink()
        for shuffle in (False, True):
            with self.subTest(shuffle=shuffle):
                self.app.shuffle = shuffle
                self.app.repeat_mode = "all"
                await self.app.action_play_index(0)
                await self.pilot.pause()
                self.assertFalse(self.app.playing)
                self.assertIn("No playable files", str(self.app.status.render()))
        self.player.play.assert_not_called()

    async def test_missing_files_skip_to_existing_track_even_with_shuffle(self):
        Path(self.songs[0]["path"]).unlink()
        self.app.shuffle = True
        self.app.repeat_mode = "all"
        await self.app.action_play_index(0)
        self.player.load.assert_called_once_with(self.songs[1]["path"])
        self.assertTrue(self.app.playing)

    async def test_auto_advance_skips_missing_hidden_song_in_full_library(self):
        third = dict(self.songs[1], path=self.songs[1]["path"] + ".missing", title="missing")
        self.app.full_playlist.insert(1, third)
        self.app.search.value = "first"
        await self.pilot.pause()
        await self.app.action_play_index(0)
        self.player.add_end_callback.call_args.args[0]()
        await self.pilot.pause()
        self.assertEqual(self.app.current_song_path, self.songs[1]["path"])
        self.assertEqual(self.app.playlist, [self.songs[0]])

    async def test_pending_end_event_does_not_skip_new_selection(self):
        await self.app.action_play_pause()
        end_callback = self.player.add_end_callback.call_args.args[0]
        end_callback()  # Queued before the user selects a new song.
        await self.app.action_play_index(0)
        end_callback()  # A delayed callback from the same old playback.
        await self.pilot.pause()
        self.assertEqual(self.app.current_song_path, self.songs[0]["path"])
        self.assertEqual(self.player.play.call_count, 2)

    async def test_duplicate_end_event_advances_only_once(self):
        await self.app.action_play_index(0)
        end_callback = self.player.add_end_callback.call_args.args[0]
        end_callback()
        end_callback()
        await self.pilot.pause()
        self.assertTrue(self.app.playing)
        self.assertEqual(self.app.current_song_path, self.songs[1]["path"])
        self.assertEqual(self.player.play.call_count, 2)

    async def test_folder_change_preserves_and_reapplies_search(self):
        self.app.search.value = "second"
        await self.pilot.pause()
        replacement = [dict(song, title="new " + song["title"]) for song in self.songs]
        with patch.object(cli_playback, "scan_folder", return_value=replacement):
            await self.app.apply_folder("new folder")
        await self.pilot.pause()
        self.assertEqual(self.app.search.value, "second")
        self.assertEqual(self.app.playlist, [replacement[1]])
        self.assertEqual(self.app.full_playlist, replacement)
        self.save_playlist.assert_called_once_with(replacement)

    async def test_folder_search_with_no_matches_shows_empty_view(self):
        self.app.search.value = "not present"
        await self.pilot.pause()
        with patch.object(cli_playback, "scan_folder", return_value=self.songs.copy()):
            await self.app.apply_folder("new folder")
        await self.pilot.pause()
        self.assertEqual(self.app.playlist, [])
        self.assertEqual(self.app.full_playlist, self.songs)


class BackendTests(unittest.TestCase):
    def test_untagged_wav_retains_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plain.wav"
            with wave.open(str(path), "wb") as audio:
                audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                audio.writeframes(b"\0\0" * 8000)
            metadata = get_metadata(path)
            self.assertEqual(metadata["title"], "plain.wav")
            self.assertEqual(metadata["artist"], "Unknown")
            self.assertAlmostEqual(metadata["length"], 1.0)

    def test_vlc_rejected_play_raises(self):
        with patch("core.player.vlc.Instance") as instance:
            backend = VLCMusic()
            instance.return_value.media_player_new.return_value.play.return_value = -1
            with self.assertRaises(RuntimeError):
                backend.play()

    def test_vlc_error_event_reaches_registered_callback(self):
        with patch("core.player.vlc.Instance") as instance:
            backend = VLCMusic()
            callback = Mock()
            backend.add_error_callback(callback)
            manager = instance.return_value.media_player_new.return_value.event_manager.return_value
            from core.player import vlc
            handlers = dict(call.args for call in manager.event_attach.call_args_list)
            handlers[vlc.EventType.MediaPlayerEncounteredError](None)
            callback.assert_called_once_with()

    def test_scan_rejects_files_and_missing_folders(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "file.mp3"
            path.touch()
            for invalid in (path, Path(folder) / "missing"):
                with self.subTest(path=invalid), self.assertRaises(NotADirectoryError):
                    scan_folder(invalid)

    def test_scan_propagates_walk_errors(self):
        def unreadable(folder, onerror):
            onerror(PermissionError("Access denied"))
        with tempfile.TemporaryDirectory() as folder:
            with patch("core.playlist.os.walk", side_effect=unreadable):
                with self.assertRaises(PermissionError):
                    scan_folder(folder)


if __name__ == "__main__":
    unittest.main()
