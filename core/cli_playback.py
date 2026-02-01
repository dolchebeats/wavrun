from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, Button, Input, ListView, ListItem, Label, ProgressBar
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.message import Message
from core.dialogs import FolderDialog
from textual import events
from textual import worker
from pathlib import Path
import asyncio


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
        self.last_index = self.cfg.get("last_index", 0)
        
        self.playing = False
        self.paused = False

        self.music_dir = self.cfg.get("music_dir")
        self.playlist = load_playlist_file()
        self.full_playlist = self.playlist.copy()  # Keep original playlist
        self.shuffle = False
        self.repeat_mode = "off"  # off|one|all
        self.song_end_flag = threading.Event()
        self.progress_updater = None
        self.stop_threads = False
        self._lock = threading.Lock()  # Thread safety



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
                self.lbl_title = Label("No song selected", id="title")
                yield self.lbl_title
                self.lbl_artist = Label("", id="artist")
                yield self.lbl_artist
                self.progress = ProgressBar(total=100)
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
            node = ListItem(Label(label))
            node.song_index = i
            node.song = item
            self.list_view.append(node)

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

    def _play_index(self, idx:int, from_thread=False):
        """Play song at given index. Thread-safe.
        
        Args:
            idx: Index of song to play IN CURRENT PLAYLIST (may be filtered)
            from_thread: True if called from background thread, False if from UI thread
        """
        if idx < 0 or idx >= len(self.playlist):
            return
            
        path = self.playlist[idx]["path"]
        # Update currently playing song path
        self.current_song_path = path
        
        logging.debug(f"_play_index called for idx={idx}, path={path}, from_thread={from_thread}")
        
        if not os.path.exists(path):
            if from_thread:
                self.call_from_thread(lambda: self.status.update("File missing"))
                self.call_from_thread(self._skip_to_next)
            else:
                self.status.update("File missing")
                asyncio.create_task(self.action_next())
            return
            
        with self._lock:
            self.player.stop()
            time.sleep(0.02)
            self.player.load(path)
            
            # Register callback BEFORE playing
            def on_end():
                logging.debug("End callback triggered by VLC!")
                self.song_end_flag.set()
            
            self.player.add_end_callback(on_end)
            logging.debug(f"End callback registered")
            
            self.player.play()
            
            # Update state
            self.playing = True
            self.paused = False
            
        # Update UI - use call_from_thread only if we're in a background thread
        if from_thread:
            self.call_from_thread(self._update_ui_playing)
        else:
            self._update_ui_playing()
        
        # start background updater if not running
        if not self.progress_updater or not self.progress_updater.is_alive():
            self.progress_updater = threading.Thread(target=self._progress_loop, daemon=True)
            self.progress_updater.start()
            logging.debug("Progress updater thread started")

    def _skip_to_next(self):
        """Called when a file is missing - skip to next song"""
        asyncio.create_task(self.action_next())

    def _on_song_end(self):
        self.call_from_thread(self._handle_song_end)

    def _handle_song_end(self):
        self.song_end_flag.set()

    async def action_play_pause(self):
        if self.playing:
            self.player.pause()
            self.playing = False
            self.paused = True
            self.btn_play.label = "▶"
        else:
            if not self.current_song_path and self.playlist:
                # No song playing - start with first song in current playlist
                self.current_song_path = self.playlist[0]["path"]
                self._play_index(0)
            else:
                self.player.unpause()
                self.playing = True
                self.paused = False
                self.btn_play.label = "⏸"

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
                    self.player.stop()
                    self.playing = False
                    self.btn_play.label = "▶"
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
        self.playlist = self.full_playlist.copy()
        self._render_playlist()
        if self.list_view.index is not None:
            self._highlight_current()

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
        self.music_dir = path
        try:
            self.playlist = scan_folder(self.music_dir)
            self.full_playlist = self.playlist.copy()
        except Exception as e:
            return
        await self.action_save()
        self.current_song_path = None  # Reset currently playing song
        self._render_playlist()
        return

    async def action_quit(self):
        await self.action_save_and_exit()

    async def action_save_and_exit(self):
        # Stop threads first
        self.stop_threads = True
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
        
        # Find the song in current playlist by path
        current_idx = self._get_current_index()
        logging.debug(f"Current song index in playlist: {current_idx}")
        
        if current_idx >= 0:
            item = self.playlist[current_idx]
            logging.debug(f"Updating UI with song from current playlist: {item.get('title')}")
            self.lbl_title.update(item.get("title"))
            self.lbl_artist.update(item.get("artist"))
            self.btn_play.label = "⏸"
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
                self.btn_play.label = "⏸"
            else:
                logging.warning(f"Song not found in either playlist!")

    def _highlight_current(self):
        # set list index focus to current song if it's in the filtered playlist
        try:
            current_idx = self._get_current_index()
            if current_idx >= 0:
                self.list_view.index = current_idx
        except Exception:
            pass

    def _progress_loop(self):
        """Background thread for updating progress and handling song end events"""
        logging.debug("Progress loop started")
        while not self.stop_threads:
            try:
                # Update progress if playing
                if self.playing:
                    pos_ms = self.player.get_pos()
                    len_ms = self.player.get_length()
                    pos_s = (pos_ms/1000.0) if pos_ms else 0.0
                    len_s = (len_ms/1000.0) if len_ms else None
                    pct = int((pos_s/len_s)*100) if len_s and len_s>0 else 0
                    # update UI
                    self.call_from_thread(lambda: self._update_progress_ui(pos_s, len_s, pct))
                
                # FIXED: Check end event and handle song advancement
                if self.song_end_flag.is_set():
                    logging.debug("Song end flag detected!")
                    self.song_end_flag.clear()
                    
                    # Handle repeat/shuffle logic
                    if self.repeat_mode == "one":
                        logging.debug("Repeat mode ONE - replaying same song")
                        # Repeat current song - find it in current playlist
                        current_idx = self._get_current_index()
                        if current_idx >= 0:
                            self._play_index(current_idx, from_thread=True)
                        else:
                            # Not in filtered playlist, use full playlist
                            idx_full = self._find_song_in_full_playlist(self.current_song_path)
                            if idx_full >= 0:
                                def replay():
                                    old_pl = self.playlist
                                    self.playlist = self.full_playlist
                                    self._play_index(idx_full, from_thread=True)
                                    self.playlist = old_pl
                                self.call_from_thread(replay)
                    else:
                        logging.debug(f"Advancing to next (shuffle={self.shuffle}, repeat={self.repeat_mode})")
                        # Advance to next song
                        self.call_from_thread(self._advance_to_next)
                        
                time.sleep(0.2)
            except Exception as e:
                # Log errors but keep thread alive
                logging.exception(f"Progress loop error: {e}")
                time.sleep(0.5)
        logging.debug("Progress loop exited")

    def _advance_to_next(self):
        """Advance to next song based on shuffle/repeat settings. Called from thread.
        
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
            next_song_path = self.full_playlist[next_idx_full]["path"]
            
        else:
            # Sequential in FULL playlist
            next_idx_full = current_idx_full + 1
            logging.debug(f"Sequential mode: next_idx_full={next_idx_full}")
            
            if next_idx_full >= len(self.full_playlist):
                if self.repeat_mode == "all":
                    # Loop back to start of FULL playlist
                    logging.debug("End of full playlist - looping to start (repeat all)")
                    next_idx_full = 0
                    next_song_path = self.full_playlist[0]["path"]
                else:
                    # Stop playback
                    logging.debug("End of full playlist - stopping playback")
                    self.player.stop()
                    with self._lock:
                        self.playing = False
                    self.btn_play.label = "▶"  # Already on main thread
                    return
            else:
                next_song_path = self.full_playlist[next_idx_full]["path"]
        
        # Now find this song in the CURRENT (possibly filtered) playlist
        next_idx_current = -1
        for i, song in enumerate(self.playlist):
            if song["path"] == next_song_path:
                next_idx_current = i
                break
        
        if next_idx_current >= 0:
            # Song is in current filtered playlist - play it
            logging.debug(f"Next song found in current playlist at index {next_idx_current}")
            self.current_song_path = next_song_path
            # We're already on main thread (via call_from_thread), so from_thread=False
            self._play_index(next_idx_current, from_thread=False)
        else:
            # Song not in filtered playlist - need to play from full playlist
            logging.debug(f"Next song NOT in filtered playlist - playing from full")
            self.current_song_path = next_song_path
            
            # Temporarily switch to full playlist
            old_playlist = self.playlist
            self.playlist = self.full_playlist
            # We're already on main thread, so from_thread=False
            self._play_index(next_idx_full, from_thread=False)
            self.playlist = old_playlist

    def _update_progress_ui(self, pos_s, len_s, pct):
        self.lbl_pos.update(format_time(pos_s))
        if len_s:
            self.lbl_len.update(format_time(len_s))
            self.progress.progress = pct  # FIXED: Set attribute instead of calling update()
        else:
            self.lbl_len.update("??:??")
            self.progress.progress = 0  # FIXED: Set attribute instead of calling update()

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
            
        term = message.value.strip().lower()
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
