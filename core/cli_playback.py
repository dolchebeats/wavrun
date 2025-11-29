# core/cli_playback.py
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, Button, Input, ListView, ListItem, Label, ProgressBar
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.message import Message
from textual import events
from core.dialogs import FolderDialog
from textual import events
from textual import worker
from pathlib import Path
import asyncio


import threading, time, random, os

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
    ]

    def __init__(self):
        super().__init__()
        self.player = VLCMusic()
        self.cfg = load_config()
        self.current_index = self.cfg.get("last_index",0)
        self.last_index = self.current_index
        self.playing = False
        self.paused = False

        self.music_dir = self.cfg.get("music_dir")
        self.last_index = self.cfg.get("last_index", 0)
        self.playlist = load_playlist_file()
        self.shuffle = False
        self.repeat_mode = "off"  # off|one|all
        self.song_end_flag = threading.Event()
        self.progress_updater = None
        self.stop_threads = False



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
        # In your compose() method, replace the old bottom row with this:
        with Horizontal(id="bottom"):
            # Search input takes most of the space
            self.search = Input(placeholder="Search (press Enter)", id="search")
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
                    self.status.update("Initial scan completed")
                except Exception as e:
                    self.status.update("Initial scan failed...")
                    return
        # populate playlist
        self._render_playlist()
        # resume last index if available
        if self.playlist and 0 <= self.last_index < len(self.playlist):
            self.current_index = self.last_index
            # don't auto-play; just highlight
            self.list_view.index = self.current_index
            self._highlight_current()

    def _render_playlist(self):
        self.list_view.clear()
        for i, item in enumerate(self.playlist):
            title = item.get("title") or os.path.basename(item["path"])
            artist = item.get("artist") or "Unknown"
            label = f"{i + 1:02d}. {title} — {artist}"
            node = ListItem(Label(label), id=f"song_{i}")
            self.list_view.append(node)

    async def on_list_view_selected(self, message: ListView.Selected):
        id_str = message.item.id
        idx = int(id_str.split("_")[1])
        await self.action_play_index(idx)

    async def action_play_index(self, idx:int):
        # load into VLC and play
        if idx < 0 or idx >= len(self.playlist):
            return
        self.current_index = idx
        self._play_index(idx)

    def _play_index(self, idx:int):
        path = self.playlist[idx]["path"]
        if not os.path.exists(path):
            self.status.update("File missing")
            return
        self.player.stop()
        time.sleep(0.02)
        self.player.load(path)
        self.player.add_end_callback(lambda: self.song_end_flag.set())
        self.player.play()
        # wait briefly then pause to mimic initial paused behavior? we will start playing
        # Update UI
        self.playing = True
        self.paused = False
        self.call_after_refresh(self._update_ui_playing)
        # start background updater if not running
        if not self.progress_updater or not self.progress_updater.is_alive():
            self.progress_updater = threading.Thread(target=self._progress_loop, daemon=True)
            self.progress_updater.start()


    async def action_play_pause(self):
        if self.playing:
            self.player.pause()
            self.playing = False
            self.paused = True
            self.btn_play.label = "▶"
        else:
            if self.current_index == -1 and self.playlist:
                self.current_index = 0
                self._play_index(0)
            else:
                self.player.unpause()
                self.playing = True
                self.paused = False
                self.btn_play.label = "⏸"

    async def action_next(self):
        if not self.playlist:
            return
        if self.shuffle:
            idx = random.randrange(len(self.playlist))
        else:
            idx = self.current_index + 1
            if idx >= len(self.playlist):
                if self.repeat_mode == "all":
                    idx = 0
                else:
                    # stop
                    self.player.stop()
                    self.playing = False
                    self.btn_play.label = "▶"
                    return
        self.current_index = idx
        self._play_index(idx)

    async def action_prev(self):
        if not self.playlist:
            return
        pos = self.player.get_pos()
        if pos and pos > 3000:
            self.player.set_time(0)
            return
        idx = self.current_index - 1 if self.current_index > 0 else (len(self.playlist)-1 if self.repeat_mode=="all" else 0)
        self.current_index = idx
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
        self.call_after_refresh(self.set_focus, self.search)

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
            #self.status.update("Folder change cancelled.")
            return
        #self.status.update(f"DEBUG: path repr={repr(path)}")
        self.music_dir = path
        try:
            self.playlist = scan_folder(self.music_dir)
        except Exception as e:
            #self.status.update(f"Error scanning folder: {e}")
            return
        await self.action_save()
        self.current_index = 0
        self._render_playlist()
        self._highlight_current()
        #self.status.update(f"Loaded: {self.music_dir}")
        return

    async def action_quit(self):
        await self.action_save_and_exit()

    async def action_save_and_exit(self):
        # Save both folder and last_index
        self.cfg["last_index"] = self.current_index
        self.cfg["music_dir"] = self.music_dir
        save_config(self.cfg)

        save_playlist_file(self.playlist)
        self.stop_threads = True
        self.player.stop()
        self.exit()

    async def action_save(self):
        # Save both folder and last_index
        self.cfg["last_index"] = self.current_index
        self.cfg["music_dir"] = self.music_dir
        save_config(self.cfg)

        save_playlist_file(self.playlist)

    def _update_ui_playing(self):
        item = self.playlist[self.current_index]
        self.lbl_title.update(item.get("title"))
        self.lbl_artist.update(item.get("artist"))
        self.btn_play.label = "⏸"
        self._highlight_current()

    def _highlight_current(self):
        # set list index focus
        try:
            self.list_view.index = self.current_index
        except Exception:
            pass

    def _progress_loop(self):
        while not self.stop_threads:
            try:
                if self.playing:
                    pos_ms = self.player.get_pos()
                    len_ms = self.player.get_length()
                    pos_s = (pos_ms/1000.0) if pos_ms else 0.0
                    len_s = (len_ms/1000.0) if len_ms else None
                    pct = int((pos_s/len_s)*100) if len_s and len_s>0 else 0
                    # update UI
                    self.call_from_thread(lambda: self._update_progress_ui(pos_s, len_s, pct))
                # check end event
                if self.song_end_flag.is_set():
                    self.song_end_flag.clear()
                    # handle repeat/shuffle
                    if self.repeat_mode == "one":
                        self._play_index(self.current_index)
                    else:
                        # next
                        if self.shuffle:
                            self.current_index = random.randrange(len(self.playlist))
                            self._play_index(self.current_index)
                        else:
                            next_idx = self.current_index + 1
                            if next_idx >= len(self.playlist):
                                if self.repeat_mode == "all":
                                    self.current_index = 0
                                    self._play_index(self.current_index)
                                else:
                                    # stop
                                    self.player.stop()
                                    self.playing = False
                                    self.call_from_thread(lambda: setattr(self.btn_play, "label", "▶"))
                            else:
                                self.current_index = next_idx
                                self._play_index(self.current_index)
                time.sleep(0.2)
            except Exception:
                time.sleep(0.5)

    def _update_progress_ui(self, pos_s, len_s, pct):
        self.lbl_pos.update(format_time(pos_s))
        if len_s:
            self.lbl_len.update(format_time(len_s))
            self.progress.update(pct)
        else:
            self.lbl_len.update("??:??")
            self.progress.update(0)

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

    async def on_input_submitted(self, message: Input.Submitted) -> None:
        term = message.value.strip().lower()
        if not term:
            # clear filter
            self.playlist = load_playlist_file()
        else:
            # filter by title or artist
            pl = load_playlist_file()
            self.playlist = [p for p in pl if term in (p.get("title","").lower() + " " + p.get("artist","").lower() + " " + p.get("path","").lower())]
        self._render_playlist()

    async def on_key(self, event: events.Key) -> None:
        # allow Enter on focused list to play
        if event.key == "enter":
            if self.list_view.index is not None:
                await self.action_play_index(self.list_view.index)


def run_tui():
    app = wavrun()
    app.run()
