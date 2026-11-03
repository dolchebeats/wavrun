from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, Button, Input, ListView, ListItem, Label, ProgressBar
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.message import Message
from core.dialogs import FolderDialog
from textual import events
from textual import worker
from pathlib import Path


import threading, time, random, os
import logging

# Set up file logging
logging.basicConfig(
    filename='wavrun_debug.log',
    level=logging.DEBUG,
    format='%(asctime)s [%(threadName)s] %(message)s',
    filemode='w'  # Overwrite log each time
)

from .player import VLCMusic
from .playlist import load_playlist_file, save_playlist_file, scan_folder
from .common import format_time
from .metadata import get_metadata
from .config import load_config, save_config

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
        self.progress_updater = None
        self.stop_threads = False
        self._lock = threading.Lock()  # Thread safety
        self._playback_generation = 0



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
        if self.music_dir:
            self.status.update("Music folder found")
            if not self.playlist:
                self.status.update("No playlist")
                try:
                    self.playlist = scan_folder(self.music_dir)
                    self.full_playlist = self.playlist.copy()
                    self.status.update("Initial scan completed")
                except Exception as e:
                    self.status.update("Initial scan failed...")
                    return
        # populate playlist
        self._render_playlist()
        
        # resume last index if available - convert to path-based tracking
        if self.playlist and 0 <= self.last_index < len(self.full_playlist):
            self.current_song_path = self.full_playlist[self.last_index]["path"]
            # Highlight it if it's in the current view
            current_idx = self._get_current_index()
            if current_idx >= 0:
                self.list_view.index = current_idx

    def _render_playlist(self):
        self.list_view.clear()
        for i, item in enumerate(self.playlist):
            title = item.get("title") or os.path.basename(item["path"])
            artist = item.get("artist") or "Unknown"
            label = f"{i + 1:02d}. {title} — {artist}"
            node = ListItem(Label(label, markup=False))
            node.song_index = i
            node.song = item
            self.list_view.append(node)
        self.call_after_refresh(self._highlight_current)

    async def on_list_view_selected(self, message: ListView.Selected):
        idx = getattr(message.item, "song_index", None)
        if idx is None:
            id_str = getattr(message.item, "id", "")
            try:
                idx = int(id_str.split("_")[1])
            except Exception:
                return
        await self.action_play_index(idx)

    async def action_play_index(self, idx:int):
        # load into VLC and play
        if idx < 0 or idx >= len(self.playlist):
            return
        # Set current song by path, not index
        self.current_song_path = self.playlist[idx]["path"]
        self._play_index(idx)

    def _play_index(self, idx:int, from_thread=False, playlist=None):
        """Play song at given index. Thread-safe.
        
        Args:
            idx: Index of song to play in the supplied or current playlist
            from_thread: True if called from background thread, False if from UI thread
            playlist: Optional source for a saved track hidden by the current filter
        """
        source = self.playlist if playlist is None else playlist
        if from_thread:
            self.call_from_thread(lambda: self._play_index(idx, playlist=playlist))
            return
        if idx < 0 or idx >= len(source):
            return

        # Try each candidate at most once, even with shuffle or repeat enabled.
        remaining = list(range(idx + 1, len(source)))
        if self.shuffle:
            remaining = [i for i in range(len(source)) if i != idx]
            random.shuffle(remaining)
        elif self.repeat_mode == "all":
            remaining += list(range(idx))
        idx = next((i for i in [idx] + remaining
                    if os.path.isfile(source[i]["path"])), None)
        if idx is None:
            self._handle_playback_failure("No playable files found; check the music folder")
            return

        path = source[idx]["path"]
        # Update currently playing song path
        self.current_song_path = path
        self.current_song_length = source[idx].get("length")
        
        logging.debug(f"_play_index called for idx={idx}, path={path}, from_thread={from_thread}")
        
        try:
            with self._lock:
                self._playback_generation += 1
                generation = self._playback_generation
                self.player.stop()
                time.sleep(0.02)
                self.player.load(path)

                def on_end():
                    self.post_message(PlaybackEnded(generation))

                self.player.add_end_callback(on_end)
                self.player.add_error_callback(
                    lambda: self.post_message(PlaybackFailed(generation))
                )
                self.player.play()
                self.playing = True
                self.paused = False
        except Exception:
            logging.exception("Unable to play %s", path)
            self._handle_playback_failure()
            return
            
        self._update_ui_playing()
        
        # start background updater if not running
        if not self.progress_updater or not self.progress_updater.is_alive():
            self.progress_updater = threading.Thread(target=self._progress_loop, daemon=True)
            self.progress_updater.start()
            logging.debug("Progress updater thread started")

    def on_playback_ended(self, message: PlaybackEnded):
        # Validate on the UI thread, after any intervening track/folder change.
        if message.generation != self._playback_generation or self.stop_threads:
            return
        if self.repeat_mode == "one":
            idx = self._find_song_in_full_playlist(self.current_song_path)
            if idx >= 0:
                self._play_index(idx, playlist=self.full_playlist)
        else:
            self._advance_to_next()

    def _stop_playback(self):
        self._playback_generation += 1
        self.player.stop()
        self.playing = False
        self.paused = False
        self.btn_play.label = "\u25b6"

    def on_playback_failed(self, message: PlaybackFailed):
        if message.generation == self._playback_generation:
            self._handle_playback_failure()

    def _handle_playback_failure(self, message="Playback failed; select another track or retry"):
        # Stay stopped so repeat/shuffle cannot loop forever over broken files.
        self._stop_playback()
        self.status.update(message)

    async def action_play_pause(self):
        if self.playing:
            self.player.pause()
            self.playing = False
            self.paused = True
            self.btn_play.label = "▶"
        elif self.paused:
            try:
                self.player.unpause()
                self.playing = True
                self.paused = False
                self.btn_play.label = "⏸"
            except Exception:
                logging.exception("Unable to resume playback")
                self._handle_playback_failure()
        else:
            idx = self._find_song_in_full_playlist(self.current_song_path)
            if idx >= 0:
                self._play_index(idx, playlist=self.full_playlist)
            elif self.playlist:
                self._play_index(0)

    async def action_next(self):
        if not self.playlist:
            return
            
        current_idx = self._get_current_index()
        
        if self.shuffle:
            idx = random.randrange(len(self.playlist))
        else:
            idx = current_idx + 1 if current_idx >= 0 else 0
            if idx >= len(self.playlist):
                if self.repeat_mode == "all":
                    idx = 0
                else:
                    # stop
                    self._stop_playback()
                    return
        
        self.current_song_path = self.playlist[idx]["path"]
        self._play_index(idx)

    async def action_prev(self):
        if not self.playlist:
            return
        pos = self.player.get_pos()
        if pos and pos > 3000:
            self.player.set_time(0)
            return
            
        current_idx = self._get_current_index()
        if current_idx < 0:
            current_idx = 0
            
        idx = current_idx - 1 if current_idx > 0 else (len(self.playlist)-1 if self.repeat_mode=="all" else 0)
        self.current_song_path = self.playlist[idx]["path"]
        self._play_index(idx)

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
        try:
            playlist = scan_folder(path)
        except Exception as e:
            logging.exception("Unable to scan music folder %s", path)
            self.status.update("Unable to read music folder; library unchanged")
            return

        with self._lock:
            self._playback_generation += 1
            self.player.stop()
            self.playing = False
            self.paused = False
            self.current_song_path = None
            self.current_song_length = None
            self.music_dir = path
            self.playlist = playlist
            self.full_playlist = playlist.copy()
        self.btn_play.label = "\u25b6"
        self.lbl_title.update("No song selected")
        self.lbl_artist.update("")
        self.lbl_pos.update("00:00")
        self.lbl_len.update("00:00")
        self.progress.progress = 0
        self._apply_search()
        await self.action_save()
        return

    async def action_quit(self):
        await self.action_save_and_exit()

    async def action_save_and_exit(self):
        # Stop threads first
        self.stop_threads = True
        self._playback_generation += 1
        if self.progress_updater and self.progress_updater.is_alive():
            self.progress_updater.join(timeout=1.0)  # Wait for thread to finish
        
        # Save current song position by finding it in full playlist
        if self.current_song_path:
            idx = self._find_song_in_full_playlist(self.current_song_path)
            self.cfg["last_index"] = idx if idx >= 0 else 0
        else:
            self.cfg["last_index"] = 0
            
        self.cfg["music_dir"] = self.music_dir
        save_config(self.cfg)

        save_playlist_file(self.full_playlist)
        self.player.stop()
        self.exit()

    async def action_save(self):
        # Save current song position by finding it in full playlist
        if self.current_song_path:
            idx = self._find_song_in_full_playlist(self.current_song_path)
            self.cfg["last_index"] = idx if idx >= 0 else 0
        else:
            self.cfg["last_index"] = 0
            
        self.cfg["music_dir"] = self.music_dir
        save_config(self.cfg)

        save_playlist_file(self.full_playlist)

    def _update_ui_playing(self):
        logging.debug(f"_update_ui_playing called, current_song_path: {self.current_song_path}")
        self.status.update("Playing" if self.playing else "Paused")
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

    def _progress_loop(self):
        """Poll progress; playback transitions are handled by UI messages."""
        logging.debug("Progress loop started")
        while not self.stop_threads:
            try:
                # Update progress if playing
                if self.playing:
                    pos_ms = self.player.get_pos()
                    len_ms = self.player.get_length()
                    pos_s = (pos_ms/1000.0) if pos_ms else 0.0
                    if self.current_song_length is None and len_ms and len_ms > 0:
                        self.current_song_length = len_ms / 1000.0
                    total_s = self.current_song_length if self.current_song_length is not None else None
                    pct = int((pos_s / total_s) * 100) if total_s else 0
                    # update UI
                    self.call_from_thread(lambda: self._update_progress_ui(pos_s, pct))
                
                time.sleep(0.2)
            except Exception as e:
                # Log errors but keep thread alive
                logging.exception(f"Progress loop error: {e}")
                time.sleep(0.5)
        logging.debug("Progress loop exited")

    def _advance_to_next(self):
        """Advance to the next song on the UI thread.
        
        IMPORTANT: When auto-advancing, we use the FULL playlist, not the filtered one.
        This ensures continuous playback even when a search filter is active.
        """
        if not self.current_song_path:
            logging.debug("No current song to advance from")
            return
            
        # Find current song in FULL playlist
        current_idx_full = self._find_song_in_full_playlist(self.current_song_path)
        logging.debug(f"_advance_to_next: current song at full_playlist[{current_idx_full}]")
        
        if current_idx_full < 0:
            logging.warning("Current song not found in full playlist!")
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
                    self._stop_playback()
                    return
        
        self._play_index(next_idx_full, playlist=self.full_playlist)

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
        self.status.update(f"Found {len(self.playlist)} songs")

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
    app = wavrun()
    app.run()
