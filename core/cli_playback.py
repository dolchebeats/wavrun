from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, Button, Input, ListView, ListItem, Label, ProgressBar
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from core.dialogs import FolderDialog
from textual import events
from pathlib import Path


import asyncio
import threading
import random, os
import logging

from .player import VLCMusic
from .playlist import load_playlist_file, save_playlist_file, scan_folder, ScanCancelled
from .background import background_read
from .common import format_time
from .config import load_config, save_config
from .storage import APP_DIR
from .splash import show_splash

class SongSelected(Message):
    def __init__(self, index: int):
        self.index = index
        super().__init__()

class PlaybackFailed(Message):
    def __init__(self, generation: int):
        self.generation = generation
        super().__init__()

class PlaybackEnded(Message):
    def __init__(self, generation: int):
        self.generation = generation
        super().__init__()

class wavrun(App):

    CSS_PATH = "wavrun.css"
    BINDINGS = [
        ("space", "play_pause", "Play/Pause"),
        ("n", "next", "Next"),
        ("p", "prev", "Previous"),
        ("s", "shuffle", "Shuffle"),
        ("r", "repeat", "Repeat"),
        ("/", "focus_search", "Search"),
        ("q", "quit", "Quit"),
        ("j", "down", "Down"),
        ("k", "up", "Up"),
        ("g", "change_music_folder", "Change Folder"),
        ("f5", "refresh_library", "Refresh Library"),
        ("escape", "clear_search", "Clear Search"),  # NEW: Escape to clear search
    ]

    def __init__(self):
        super().__init__()
        self.player = VLCMusic()
        self.cfg = load_config()
        
        # NEW: Track currently playing song by path instead of index
        self.current_song_path = None
        self.current_song_length = None
        self.last_index = self.cfg.get("last_index", 0)
        
        self.playing = False
        self.paused = False

        self.music_dir = self.cfg.get("music_dir")
        self.playlist = load_playlist_file()
        self.full_playlist = self.playlist.copy()  # Keep original playlist
        self.shuffle = False
        self.repeat_mode = "off"  # off|one|all
        self.stop_threads = False
        self._lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._scan_generation = 0
        self._scan_cancellations = set()
        self._active_scan_generation = None
        self._playback_generation = 0
        self._playlist_generation = 0



    def _get_current_index(self):
        """Get the index of currently playing song in the current (possibly filtered) playlist.
        Returns -1 if not found."""
        if not self.current_song_path:
            return -1
        for i, song in enumerate(self.playlist):
            if song["path"] == self.current_song_path:
                return i
        return -1
    
    def _find_song_in_full_playlist(self, path):
        """Find a song by path in the full playlist. Returns index or -1."""
        for i, song in enumerate(self.full_playlist):
            if song["path"] == path:
                return i
        return -1

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            # left playlist
            with VerticalScroll(id="playlist_panel"):
                self.list_view = ListView()
                yield self.list_view
            # center now playing and controls
            with Vertical(id="now_panel"):
                self.lbl_title = Label("No song selected", id="title", markup=False)
                yield self.lbl_title
                self.lbl_artist = Label("", id="artist", markup=False)
                yield self.lbl_artist
                self.progress = ProgressBar(total=100, show_percentage=False)
                yield self.progress
                # time labels
                with Horizontal():
                    self.lbl_pos = Label("00:00")
                    yield self.lbl_pos
                    yield Static("")  # spacer
                    self.lbl_len = Label("00:00")
                    yield self.lbl_len
                # controls row
                with Horizontal():
                    self.btn_prev = Button("⏮", id="prev")
                    yield self.btn_prev
                    self.btn_play = Button("▶", id="play")
                    yield self.btn_play
                    self.btn_next = Button("⏭", id="next")
                    yield self.btn_next
                    self.btn_shuffle = Button("Shuffle", id="shuffle")
                    yield self.btn_shuffle
                    self.btn_repeat = Button("Repeat: Off", id="repeat")
                    yield self.btn_repeat
        # footer with search + status
        with Horizontal(id="bottom"):
            # Search input takes most of the space
            self.search = Input(placeholder="Search (type to filter, Esc to clear)", id="search")
            yield self.search

            # Fixed-width status label
            self.status = Label("Ready", id="status")
            yield self.status

        yield Footer()

    async def on_mount(self):
        # populate playlist
        self._render_playlist()
        
        # resume last index if available - convert to path-based tracking
        if self.playlist and 0 <= self.last_index < len(self.full_playlist):
            self.current_song_path = self.full_playlist[self.last_index]["path"]
            # Highlight it if it's in the current view
            current_idx = self._get_current_index()
            if current_idx >= 0:
                self.list_view.index = current_idx
        self.set_interval(0.2, self._poll_progress)
        if self.music_dir:
            self.run_worker(self._refresh_library(), group="library")

    async def _audio(self, function, *args):
        async with self._lock:
            return await asyncio.to_thread(function, *args)

    def _set_status(self, message):
        if self._active_scan_generation is not None:
            message = "Scanning music folder..." + (f" ({message})" if message else "")
        self.status.update(message)

    def _render_playlist(self):
        self._playlist_generation += 1
        self.list_view.clear()
        for i, item in enumerate(self.playlist):
            title = item.get("title") or os.path.basename(item["path"])
            artist = item.get("artist") or "Unknown"
            label = f"{i + 1:02d}. {title} — {artist}"
            node = ListItem(Label(label, markup=False))
            node.song_index = i
            node.song = item
            node.playlist_generation = self._playlist_generation
            self.list_view.append(node)
        self.call_after_refresh(self._highlight_current)

    async def on_list_view_selected(self, message: ListView.Selected):
        # A queued click may refer to a row removed by a folder/filter change.
        if (message.list_view is not self.list_view or
                getattr(message.item, "playlist_generation", None) != self._playlist_generation):
            return
        song = getattr(message.item, "song", None)
        if song is None:
            return
        for idx, entry in enumerate(self.playlist):
            if entry["path"] == song["path"]:
                await self.action_play_index(idx)
                return

    async def action_play_index(self, idx:int):
        # load into VLC and play
        if idx < 0 or idx >= len(self.playlist):
            return
        await self._play_index(idx)

    async def _play_index(self, idx:int, playlist=None):
        """Play song at given index. Thread-safe.
        
        Args:
            idx: Index of song to play in the supplied or current playlist
            playlist: Optional source for a saved track hidden by the current filter
        """
        source = self.playlist if playlist is None else playlist
        if idx < 0 or idx >= len(source):
            return

        # Try each candidate at most once, even with shuffle or repeat enabled.
        remaining = list(range(idx + 1, len(source)))
        if self.shuffle:
            remaining = [i for i in range(len(source)) if i != idx]
            random.shuffle(remaining)
        elif self.repeat_mode == "all":
            remaining += list(range(idx))
        generation = self._playback_generation = self._playback_generation + 1
        idx = await background_read(
            lambda: next((i for i in [idx] + remaining
                          if os.path.isfile(source[i]["path"])), None))
        if generation != self._playback_generation:
            return
        if idx is None:
            await self._handle_playback_failure("No playable files found; check the music folder")
            return

        path = source[idx]["path"]
        # Update currently playing song path
        self.current_song_path = path
        self.current_song_length = source[idx].get("length")
        
        def start():
            if generation != self._playback_generation:
                return
            self.player.stop()
            self.player.load(path)

            def on_end():
                self.post_message(PlaybackEnded(generation))

            self.player.add_end_callback(on_end)
            self.player.add_error_callback(
                lambda: self.post_message(PlaybackFailed(generation))
            )
            self.player.play()
        try:
            await self._audio(start)
        except Exception:
            logging.exception("Unable to play %s", path)
            if generation == self._playback_generation:
                await self._handle_playback_failure()
            return
        if generation != self._playback_generation:
            return
        self.playing = True
        self.paused = False
        self._update_ui_playing()

    async def on_playback_ended(self, message: PlaybackEnded):
        # Validate on the UI thread, after any intervening track/folder change.
        if message.generation != self._playback_generation or self.stop_threads:
            return
        if self.repeat_mode == "one":
            idx = self._find_song_in_full_playlist(self.current_song_path)
            if idx >= 0:
                await self._play_index(idx, playlist=self.full_playlist)
        else:
            await self._advance_to_next()

    def _reset_playback_state(self):
        """Invalidate old playback before yielding to other UI actions."""
        self._playback_generation += 1
        self.playing = False
        self.paused = False
        self.btn_play.label = "\u25b6"

    async def _stop_playback(self):
        self._reset_playback_state()
        await self._audio(self.player.stop)

    async def on_playback_failed(self, message: PlaybackFailed):
        if message.generation == self._playback_generation:
            await self._handle_playback_failure()

    async def _handle_playback_failure(self, message="Playback failed; select another track or retry"):
        # Stay stopped so repeat/shuffle cannot loop forever over broken files.
        await self._stop_playback()
        self._set_status(message)

    async def action_play_pause(self):
        if self.playing:
            try:
                await self._audio(self.player.pause)
            except Exception:
                logging.exception("Unable to pause playback")
                await self._handle_playback_failure()
                return
            self.playing = False
            self.paused = True
            self.btn_play.label = "▶"
        elif self.paused:
            try:
                await self._audio(self.player.unpause)
                self.playing = True
                self.paused = False
                self.btn_play.label = "⏸"
            except Exception:
                logging.exception("Unable to resume playback")
                await self._handle_playback_failure()
        else:
            idx = self._find_song_in_full_playlist(self.current_song_path)
            if idx >= 0:
                await self._play_index(idx, playlist=self.full_playlist)
            elif self.playlist:
                await self._play_index(0)

    async def action_next(self):
        await self._advance_to_next()

    async def action_prev(self):
        if not self.full_playlist:
            return
        pos = await self._audio(self.player.get_pos)
        if pos and pos > 3000:
            await self._audio(self.player.set_time, 0)
            return
            
        current_idx = self._find_song_in_full_playlist(self.current_song_path)
        if current_idx < 0:
            current_idx = 0
            
        idx = current_idx - 1 if current_idx > 0 else (len(self.full_playlist)-1 if self.repeat_mode=="all" else 0)
        await self._play_index(idx, playlist=self.full_playlist)

    async def action_shuffle(self):
        self.shuffle = not self.shuffle
        self.btn_shuffle.label = "Shuffle ✓" if self.shuffle else "Shuffle"

    async def action_repeat(self):
        if self.repeat_mode == "off":
            self.repeat_mode = "one"
            self.btn_repeat.label = "Repeat: One"
        elif self.repeat_mode == "one":
            self.repeat_mode = "all"
            self.btn_repeat.label = "Repeat: All"
        else:
            self.repeat_mode = "off"
            self.btn_repeat.label = "Repeat: Off"

    async def action_focus_search(self):
        self.search.focus()

    async def action_clear_search(self):
        """NEW: Clear search and restore full playlist"""
        self.search.value = ""
        self._apply_search()

    async def action_down(self):
        self.list_view.action_cursor_down()

    async def action_up(self):
        self.list_view.action_cursor_up()



    async def action_change_music_folder(self):
        self.run_worker(self._choose_folder(), exclusive=True)

    async def _choose_folder(self):
        dialog = FolderDialog(self.music_dir)
        new_path = await self.push_screen(dialog, wait_for_dismiss=True)
        await self.apply_folder(new_path)

    async def apply_folder(self, path):
        if not path:
            return
        await self._refresh_library(path, change_folder=True)

    async def action_refresh_library(self):
        self.run_worker(self._refresh_library(), group="library")

    async def _refresh_library(self, path=None, change_folder=False):
        path = path or self.music_dir
        if not path:
            self._set_status("Choose a music folder first")
            return
        self._scan_generation += 1
        scan_generation = self._scan_generation
        for pending in self._scan_cancellations:
            pending.set()
        cancel_scan = threading.Event()
        self._scan_cancellations.add(cancel_scan)
        self._active_scan_generation = scan_generation
        self._set_status("")
        def scan():
            folder = str(Path(path).expanduser().resolve())
            return folder, scan_folder(folder, cancel_event=cancel_scan)
        try:
            path, playlist = await background_read(scan, cancel_scan)
        except ScanCancelled:
            if self._active_scan_generation == scan_generation:
                self._active_scan_generation = None
                if not self.stop_threads:
                    self._set_status("Folder scan cancelled")
            return
        except asyncio.CancelledError:
            if self._active_scan_generation == scan_generation:
                self._active_scan_generation = None
                if not self.stop_threads:
                    self._set_status("Folder scan cancelled")
            raise
        except Exception:
            logging.exception("Unable to scan music folder %s", path)
            if scan_generation == self._scan_generation:
                self._active_scan_generation = None
                self._set_status("Unable to read music folder; library unchanged")
            return
        finally:
            self._scan_cancellations.discard(cancel_scan)
        if scan_generation != self._scan_generation or self.stop_threads:
            return
        self._active_scan_generation = None
        old_entries = {song["path"]: song for song in self.full_playlist}
        for song in playlist:
            if song["path"] in old_entries:
                song["rating"] = old_entries[song["path"]].get("rating", 0)
        stop_playback = change_folder or (self.current_song_path and
                          not any(song["path"] == self.current_song_path for song in playlist))
        if stop_playback:
            self._reset_playback_state()
            self.current_song_path = None
            self.current_song_length = None
            self.lbl_title.update("No song selected")
            self.lbl_artist.update("")
            self.lbl_pos.update("00:00")
            self.lbl_len.update("00:00")
            self.progress.progress = 0
        self.music_dir = path
        self.full_playlist = playlist
        self._apply_search()
        # Publish the new library and invalidate old requests before awaiting VLC.
        # Selections made during a slow stop now refer to the new rows, and their
        # playback is serialized after this stop by the audio lock.
        if stop_playback:
            await self._audio(self.player.stop)
        if scan_generation != self._scan_generation or self.stop_threads:
            return
        await self.action_save()
        return

    async def action_quit(self):
        await self.action_save_and_exit()

    async def action_save_and_exit(self):
        self.stop_threads = True
        for pending in self._scan_cancellations:
            pending.set()
        self._scan_generation += 1
        self._active_scan_generation = None
        if not await self.action_save():
            self.stop_threads = False
            return
        await self._stop_playback()
        await self._audio(self.player.close)
        self.exit()

    async def action_save(self):
        # Save current song position by finding it in full playlist
        if self.current_song_path:
            idx = self._find_song_in_full_playlist(self.current_song_path)
            self.cfg["last_index"] = idx if idx >= 0 else 0
        else:
            self.cfg["last_index"] = 0
            
        self.cfg["music_dir"] = self.music_dir
        cfg = self.cfg.copy()
        playlist = [song.copy() for song in self.full_playlist]
        def save():
            save_playlist_file(playlist)
            save_config(cfg)
        try:
            async with self._save_lock:
                await asyncio.to_thread(save)
        except Exception:
            logging.exception("Unable to save settings/library")
            self._set_status("Save failed; check disk space and permissions, then retry")
            return False
        return True

    def _update_ui_playing(self):
        logging.debug(f"_update_ui_playing called, current_song_path: {self.current_song_path}")
        self._set_status("Playing" if self.playing else "Paused")
        self.btn_play.label = "\u23f8" if self.playing else "\u25b6"
        
        # Find the song in current playlist by path
        current_idx = self._get_current_index()
        logging.debug(f"Current song index in playlist: {current_idx}")
        
        if current_idx >= 0:
            item = self.playlist[current_idx]
            logging.debug(f"Updating UI with song from current playlist: {item.get('title')}")
            self.lbl_title.update(item.get("title"))
            self.lbl_artist.update(item.get("artist"))
            self._highlight_current()
        else:
            # Song not in filtered playlist - show it anyway
            idx_in_full = self._find_song_in_full_playlist(self.current_song_path)
            logging.debug(f"Song not in current playlist, index in full playlist: {idx_in_full}")
            if idx_in_full >= 0:
                item = self.full_playlist[idx_in_full]
                logging.debug(f"Updating UI with song from full playlist: {item.get('title')}")
                self.lbl_title.update(item.get("title") + " (not in filter)")
                self.lbl_artist.update(item.get("artist"))
            else:
                logging.warning(f"Song not found in either playlist!")
        self._highlight_current()

    def _highlight_current(self):
        # set list index focus to current song if it's in the filtered playlist
        try:
            current_idx = self._get_current_index()
            self.list_view.index = current_idx if current_idx >= 0 else None
        except Exception:
            pass

    async def _poll_progress(self):
        if not self.playing or self.stop_threads:
            return
        generation = self._playback_generation
        try:
            def read_progress():
                if self.stop_threads or generation != self._playback_generation:
                    return 0, None
                return self.player.get_pos(), self.player.get_length()
            pos_ms, len_ms = await self._audio(
                read_progress)
            if generation != self._playback_generation or not self.playing:
                return
            if self.current_song_length is None and len_ms and len_ms > 0:
                self.current_song_length = len_ms / 1000.0
            pos_s = max(0, pos_ms or 0) / 1000.0
            total = self.current_song_length
            pct = min(100, max(0, int(pos_s / total * 100))) if total else 0
            self._update_progress_ui(pos_s, pct)
        except Exception:
            logging.exception("Unable to update playback progress")

    async def _advance_to_next(self):
        """Advance to the next song on the UI thread.
        
        IMPORTANT: When auto-advancing, we use the FULL playlist, not the filtered one.
        This ensures continuous playback even when a search filter is active.
        """
        if not self.full_playlist:
            return
            
        # Find current song in FULL playlist
        current_idx_full = self._find_song_in_full_playlist(self.current_song_path)
        logging.debug(f"_advance_to_next: current song at full_playlist[{current_idx_full}]")
        
        if current_idx_full < 0:
            await self._play_index(0, playlist=self.full_playlist)
            return
        
        if self.shuffle:
            # Random song from FULL playlist
            next_idx_full = random.randrange(len(self.full_playlist))
            logging.debug(f"Shuffle mode: selected random index {next_idx_full} from full playlist")
            
        else:
            # Sequential in FULL playlist
            next_idx_full = current_idx_full + 1
            logging.debug(f"Sequential mode: next_idx_full={next_idx_full}")
            
            if next_idx_full >= len(self.full_playlist):
                if self.repeat_mode == "all":
                    # Loop back to start of FULL playlist
                    logging.debug("End of full playlist - looping to start (repeat all)")
                    next_idx_full = 0
                else:
                    # Stop playback
                    logging.debug("End of full playlist - stopping playback")
                    await self._stop_playback()
                    return
        
        await self._play_index(next_idx_full, playlist=self.full_playlist)

    def _update_progress_ui(self, pos_s, pct):
        self.lbl_pos.update(format_time(pos_s))

        total_s = self.current_song_length if self.current_song_length is not None else None
        if total_s:
            self.lbl_len.update(format_time(total_s))
        else:
            self.lbl_len.update("??:??")

        self.progress.progress = pct

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        id = event.button.id
        if id == "play":
            await self.action_play_pause()
        elif id == "next":
            await self.action_next()
        elif id == "prev":
            await self.action_prev()
        elif id == "shuffle":
            await self.action_shuffle()
        elif id == "repeat":
            await self.action_repeat()

    # FIXED: Handle search as-you-type instead of on submit
    async def on_input_changed(self, message: Input.Changed) -> None:
        """NEW: Filter playlist as user types"""
        if message.input.id != "search":
            return
            
        self._apply_search()

    def _apply_search(self):
        """Apply the visible query after typing or replacing the library."""
        term = self.search.value.strip().lower()
        if not term:
            # Restore full playlist
            self.playlist = self.full_playlist.copy()
        else:
            # Filter by title or artist
            self.playlist = [
                p for p in self.full_playlist
                if term in (p.get("title","").lower() + " " +
                           p.get("artist","").lower() + " " +
                           os.path.basename(p.get("path","")).lower())
            ]
        self._render_playlist()
        if self.playing or self.paused:
            self._update_ui_playing()
        self._set_status(f"Found {len(self.playlist)} songs")

    async def on_input_submitted(self, message: Input.Submitted) -> None:
        """REMOVED: No longer plays on Enter - just move focus back to list"""
        if message.input.id == "search":
            # Move focus to list view
            self.list_view.focus()

    async def on_key(self, event: events.Key) -> None:
        # REMOVED: Enter on list no longer needed since search doesn't submit
        # Keep this for potential other key handling
        pass


def run_tui():
    logging.basicConfig(filename=APP_DIR / "wavrun_debug.log", level=logging.DEBUG,
                        format='%(asctime)s [%(threadName)s] %(message)s')
    show_splash()
    app = wavrun()
    app.run()
