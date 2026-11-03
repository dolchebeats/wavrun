"""Textual music player backed by Wavrun's PyAV/PortAudio engine."""
import asyncio
import logging
import os
from pathlib import Path
import random
import threading

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, ProgressBar

from .background import background_read
from .common import format_time
from .config import load_config, save_config
from .dialogs import FolderDialog
from .player import AudioPlayer, PlaybackEvent, PlayerState
from .playlist import load_playlist_file, save_playlist_file, scan_folder, ScanCancelled
from .splash import show_splash
from .storage import APP_DIR, LOG_DIR


class EngineEvent(Message):
    def __init__(self, event: PlaybackEvent):
        self.event = event
        super().__init__()


class wavrun(App):
    CSS_PATH = APP_DIR / "core" / "wavrun.css"
    BINDINGS = [
        ("space", "play_pause", "Play/Pause"), ("n", "next", "Next"),
        ("p", "prev", "Previous"), ("s", "shuffle", "Shuffle"),
        ("r", "repeat", "Repeat"), ("/", "focus_search", "Search"),
        ("q", "quit", "Quit"), ("j", "down", "Down"), ("k", "up", "Up"),
        ("g", "change_music_folder", "Change Folder"),
        ("f5", "refresh_library", "Refresh Library"),
        ("escape", "clear_search", "Clear Search"),
        ("left", "seek_backward", "Seek -5s"), ("right", "seek_forward", "Seek +5s"),
        ("+", "volume_up", "Volume +"), ("=", "volume_up", "Volume +"),
        ("-", "volume_down", "Volume -"),
    ]

    def __init__(self, *, player=None, config=None, playlist=None):
        super().__init__()
        self.cfg = load_config() if config is None else config.copy()
        self.volume = self.cfg.get("volume", 0.7)
        self.player = player if player is not None else AudioPlayer(volume=self.volume)
        self.player.add_event_callback(self._receive_engine_event)
        self.music_dir = self.cfg.get("music_dir")
        self.playlist = load_playlist_file() if playlist is None else playlist.copy()
        self.full_playlist = self.playlist.copy()
        self.current_song_path = None
        self.current_song_length = None
        self.playing = self.paused = False
        self.shuffle = False
        self.repeat_mode = "off"
        self.stop_threads = False
        self._save_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._scan_generation = 0
        self._scan_cancellations = set()
        self._active_scan_generation = None
        self._playback_generation = 0
        self._playlist_generation = 0
        self._minimum_engine_id = None
        self._handled_terminal = None
        self._closed_player = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with VerticalScroll(id="playlist_panel"):
                self.list_view = ListView()
                yield self.list_view
            with Vertical(id="now_panel"):
                self.lbl_title = Label("No song selected", id="title", markup=False)
                self.lbl_artist = Label("", id="artist", markup=False)
                yield self.lbl_title
                yield self.lbl_artist
                self.progress = ProgressBar(total=100, show_percentage=False)
                yield self.progress
                with Horizontal(id="time_row"):
                    self.lbl_pos = Label("00:00")
                    self.lbl_len = Label("00:00")
                    self.lbl_volume = Label(f"Vol {round(self.volume * 100)}%")
                    yield self.lbl_pos
                    yield self.lbl_len
                    yield self.lbl_volume
                with Horizontal(id="controls"):
                    self.btn_prev = Button("⏮", id="prev")
                    self.btn_play = Button("▶", id="play")
                    self.btn_next = Button("⏭", id="next")
                    self.btn_shuffle = Button("Shuffle", id="shuffle")
                    self.btn_repeat = Button("Repeat: Off", id="repeat")
                    yield self.btn_prev
                    yield self.btn_play
                    yield self.btn_next
                    yield self.btn_shuffle
                    yield self.btn_repeat
        with Horizontal(id="bottom"):
            self.search = Input(placeholder="Search (Esc to clear)", id="search")
            self.status = Label("Ready", id="status", markup=False)
            yield self.search
            yield self.status
        yield Footer()

    async def on_mount(self):
        index = self.cfg.get("last_index", 0)
        if self.full_playlist and 0 <= index < len(self.full_playlist):
            self.current_song_path = self.full_playlist[index]["path"]
        self._render_playlist()
        self.set_interval(0.2, self._poll_progress)
        if self.music_dir:
            self.run_worker(self._refresh_library(), group="library")

    async def on_unmount(self):
        self.stop_threads = True
        self._cancel_scans()
        await self._close_player()

    async def _close_player(self):
        if not self._closed_player:
            self._closed_player = True
            await asyncio.to_thread(self.player.close)

    def _receive_engine_event(self, event):
        # AudioPlayer invokes this on its event thread. Widget work stays on Textual.
        if not self.stop_threads:
            self.post_message(EngineEvent(event))

    async def on_engine_event(self, message: EngineEvent):
        event = message.event
        if (self.stop_threads or self._minimum_engine_id is None or
                event.session_id < self._minimum_engine_id or
                event.session_id != self.player.session_id or
                event.path != self.current_song_path):
            return
        self._sync_playback_state(event.state)
        self._display_progress(event.position_ms, event.duration_ms)
        if event.kind in ("end", "error"):
            if self._handled_terminal == event.session_id:
                return
            self._handled_terminal = event.session_id
            if event.kind == "error":
                self._set_status(f"Playback failed: {event.error or 'unknown audio error'}")
                return
            if self.repeat_mode == "one":
                index = self._find_song_in_full_playlist(self.current_song_path)
                if index >= 0:
                    await self._play_index(index, playlist=self.full_playlist)
            else:
                await self._advance_to_next()

    def _sync_playback_state(self, state=None):
        state = self.player.state if state is None else state
        self.playing = state in (PlayerState.LOADING, PlayerState.BUFFERING, PlayerState.PLAYING)
        self.paused = state == PlayerState.PAUSED
        self.btn_play.label = "⏸" if self.playing else "▶"
        self._update_now_playing()
        labels = {PlayerState.LOADING: "Loading track...", PlayerState.BUFFERING: "Buffering...",
                  PlayerState.PLAYING: "Playing", PlayerState.PAUSED: "Paused",
                  PlayerState.STOPPED: "Stopped", PlayerState.CLOSED: "Closed"}
        if state in labels:
            self._set_status(labels[state])

    def _set_status(self, message):
        if self._active_scan_generation is not None:
            message = "Scanning music folder..." + (f" ({message})" if message else "")
        self.status.update(message)

    def _get_current_index(self):
        return next((i for i, song in enumerate(self.playlist)
                     if song["path"] == self.current_song_path), -1)

    def _find_song_in_full_playlist(self, path):
        return next((i for i, song in enumerate(self.full_playlist) if song["path"] == path), -1)

    def _render_playlist(self):
        self._playlist_generation += 1
        self.list_view.clear()
        for index, song in enumerate(self.playlist):
            title = song.get("title") or os.path.basename(song["path"])
            artist = song.get("artist") or "Unknown"
            item = ListItem(Label(f"{index + 1:02d}. {title} — {artist}", markup=False))
            item.song = song
            item.playlist_generation = self._playlist_generation
            self.list_view.append(item)
        self.call_after_refresh(self._highlight_current)

    def _highlight_current(self):
        index = self._get_current_index()
        self.list_view.index = index if index >= 0 else None

    def _update_now_playing(self):
        index = self._find_song_in_full_playlist(self.current_song_path)
        if index >= 0:
            song = self.full_playlist[index]
            title = song.get("title") or os.path.basename(song["path"])
            if self._get_current_index() < 0:
                title += " (not in filter)"
            self.lbl_title.update(title)
            self.lbl_artist.update(song.get("artist") or "Unknown")
        self._highlight_current()

    async def on_list_view_selected(self, message: ListView.Selected):
        if (message.list_view is not self.list_view or
                getattr(message.item, "playlist_generation", None) != self._playlist_generation):
            return
        song = getattr(message.item, "song", None)
        if song:
            index = next((i for i, item in enumerate(self.playlist) if item["path"] == song["path"]), -1)
            if index >= 0:
                await self.action_play_index(index)

    async def action_play_index(self, index):
        await self._play_index(index)

    async def _play_index(self, index, playlist=None):
        source = self.playlist if playlist is None else playlist
        if self.stop_threads or not 0 <= index < len(source):
            return
        self._playback_generation += 1
        generation = self._playback_generation
        self._minimum_engine_id = None
        self.player.cancel_pending()
        source = source.copy()
        remaining = list(range(index + 1, len(source)))
        if self.shuffle:
            remaining = [i for i in range(len(source)) if i != index]
            random.shuffle(remaining)
        elif self.repeat_mode == "all":
            remaining += list(range(index))
        self._set_status("Loading track...")
        self.run_worker(self._start_track(generation, source, [index] + remaining), group="playback")

    async def _start_track(self, generation, source, candidates):
        index = await background_read(lambda: next(
            (i for i in candidates if os.path.isfile(source[i]["path"])), None))
        if generation != self._playback_generation or self.stop_threads:
            return
        if index is None:
            await self._stop_playback()
            self._set_status("No playable files found; check the music folder")
            return
        self.current_song_path = source[index]["path"]
        self.current_song_length = source[index].get("length")
        self._update_now_playing()
        try:
            async with self._start_lock:
                if generation != self._playback_generation or self.stop_threads:
                    return
                await asyncio.to_thread(self.player.load, self.current_song_path)
                if generation != self._playback_generation or self.stop_threads:
                    return
                self._minimum_engine_id = self.player.session_id + 1
                await asyncio.to_thread(self.player.play)
                if generation == self._playback_generation and not self.stop_threads:
                    self._sync_playback_state()
                    if self.player.state == PlayerState.ERROR:
                        self._set_status(f"Playback failed: {self.player.error}")
        except Exception as error:
            logging.exception("Unable to play track")
            if generation == self._playback_generation and not self.stop_threads:
                self.playing = self.paused = False
                self.btn_play.label = "▶"
                self._set_status(f"Playback failed: {error}")

    def _reset_playback_state(self):
        self._playback_generation += 1
        self._minimum_engine_id = None
        self.player.cancel_pending()
        self.playing = self.paused = False
        self.btn_play.label = "▶"

    async def _stop_playback(self):
        self._reset_playback_state()
        await asyncio.to_thread(self.player.stop)

    def _control(self, function, *args):
        generation = self._playback_generation
        def apply():
            if generation == self._playback_generation and not self.stop_threads:
                return function(*args)
        async def run():
            try:
                if generation != self._playback_generation or self.stop_threads:
                    return
                await asyncio.to_thread(apply)
                if generation == self._playback_generation and not self.stop_threads:
                    self._sync_playback_state()
            except Exception as error:
                logging.exception("Playback control failed")
                if generation == self._playback_generation and not self.stop_threads:
                    self._set_status(f"Playback failed: {error}")
        self.run_worker(run(), group="playback-controls")

    async def action_play_pause(self):
        if self.playing:
            self._control(self.player.pause)
        elif self.paused:
            self._control(self.player.unpause)
        else:
            index = self._find_song_in_full_playlist(self.current_song_path)
            if index >= 0:
                await self._play_index(index, self.full_playlist)
            elif self.playlist:
                await self._play_index(0)

    async def action_next(self):
        await self._advance_to_next()

    async def _advance_to_next(self):
        if not self.full_playlist:
            return
        index = self._find_song_in_full_playlist(self.current_song_path)
        if self.shuffle:
            index = random.randrange(len(self.full_playlist))
        else:
            index += 1
            if index >= len(self.full_playlist):
                if self.repeat_mode != "all":
                    await self._stop_playback()
                    self._set_status("Stopped")
                    return
                index = 0
        await self._play_index(index, self.full_playlist)

    async def action_prev(self):
        if not self.full_playlist:
            return
        if self.player.get_pos() > 3000:
            self._control(self.player.set_time, 0)
            return
        index = self._find_song_in_full_playlist(self.current_song_path)
        index = index - 1 if index > 0 else (len(self.full_playlist) - 1 if self.repeat_mode == "all" else 0)
        await self._play_index(index, self.full_playlist)

    async def action_seek_backward(self):
        if self.playing or self.paused:
            self._control(self.player.set_time, max(0, self.player.get_pos() - 5000))

    async def action_seek_forward(self):
        if self.playing or self.paused:
            self._control(self.player.set_time, self.player.get_pos() + 5000)

    async def _change_volume(self, delta):
        self.volume = min(1, max(0, round(self.volume + delta, 2)))
        self.player.set_volume(self.volume)
        self.lbl_volume.update(f"Vol {round(self.volume * 100)}%")

    async def action_volume_up(self):
        await self._change_volume(0.05)

    async def action_volume_down(self):
        await self._change_volume(-0.05)

    async def action_shuffle(self):
        self.shuffle = not self.shuffle
        self.btn_shuffle.label = "Shuffle ✓" if self.shuffle else "Shuffle"

    async def action_repeat(self):
        modes = ["off", "one", "all"]
        self.repeat_mode = modes[(modes.index(self.repeat_mode) + 1) % 3]
        self.btn_repeat.label = f"Repeat: {self.repeat_mode.title()}"

    async def _poll_progress(self):
        if self.stop_threads or not (self.playing or self.paused):
            return
        self._display_progress(self.player.get_pos(), self.player.get_length())

    def _display_progress(self, milliseconds, length=None):
        if length is not None:
            self.current_song_length = length / 1000
        position = milliseconds / 1000
        total = self.current_song_length
        self.lbl_pos.update(format_time(position))
        self.lbl_len.update(format_time(total))
        self.progress.progress = min(100, max(0, position / total * 100)) if total else 0

    async def action_focus_search(self):
        self.search.focus()

    async def action_clear_search(self):
        self.search.value = ""
        self._apply_search()

    def _apply_search(self):
        term = self.search.value.strip().lower()
        self.playlist = [song for song in self.full_playlist if not term or term in
                         f"{song.get('title') or ''} {song.get('artist') or ''} {os.path.basename(song['path'])}".lower()]
        self._render_playlist()
        if self.current_song_path:
            self._update_now_playing()
        self._set_status(f"Found {len(self.playlist)} songs")

    async def on_input_changed(self, message: Input.Changed):
        if message.input.id == "search":
            self._apply_search()

    async def on_input_submitted(self, message: Input.Submitted):
        if message.input.id == "search":
            self.list_view.focus()

    async def action_down(self):
        self.list_view.action_cursor_down()

    async def action_up(self):
        self.list_view.action_cursor_up()

    async def on_button_pressed(self, event: Button.Pressed):
        actions = {"play": self.action_play_pause, "next": self.action_next, "prev": self.action_prev,
                   "shuffle": self.action_shuffle, "repeat": self.action_repeat}
        if event.button.id in actions:
            await actions[event.button.id]()

    async def action_change_music_folder(self):
        self.run_worker(self._choose_folder(), group="folder-dialog", exclusive=True)

    async def _choose_folder(self):
        path = await self.push_screen(FolderDialog(self.music_dir), wait_for_dismiss=True)
        await self.apply_folder(path)

    async def apply_folder(self, path):
        if path:
            await self._refresh_library(path, change_folder=True)

    async def action_refresh_library(self):
        self.run_worker(self._refresh_library(), group="library")

    def _cancel_scans(self):
        self._scan_generation += 1
        self._active_scan_generation = None
        for cancellation in self._scan_cancellations:
            cancellation.set()

    async def _refresh_library(self, path=None, change_folder=False):
        path = path or self.music_dir
        if not path:
            self._set_status("Choose a music folder first")
            return
        self._cancel_scans()
        generation = self._scan_generation
        cancellation = threading.Event()
        self._scan_cancellations.add(cancellation)
        self._active_scan_generation = generation
        self._set_status("")
        def scan():
            folder = str(Path(path).expanduser().resolve())
            return folder, scan_folder(folder, cancel_event=cancellation)
        try:
            path, songs = await background_read(scan, cancellation)
        except (ScanCancelled, asyncio.CancelledError):
            if generation == self._scan_generation:
                self._active_scan_generation = None
            return
        except Exception:
            logging.exception("Unable to scan music folder")
            if generation == self._scan_generation and not self.stop_threads:
                self._active_scan_generation = None
                self._set_status("Unable to read music folder; library unchanged")
            return
        finally:
            self._scan_cancellations.discard(cancellation)
        if generation != self._scan_generation or self.stop_threads:
            return
        self._active_scan_generation = None
        previous = {song["path"]: song for song in self.full_playlist}
        for song in songs:
            song["rating"] = previous.get(song["path"], {}).get("rating", 0)
        stop = change_folder or (self.current_song_path and
                                 not any(song["path"] == self.current_song_path for song in songs))
        if stop:
            self._reset_playback_state()
            self.current_song_path = self.current_song_length = None
            self.lbl_title.update("No song selected")
            self.lbl_artist.update("")
            self.lbl_pos.update("00:00")
            self.lbl_len.update("00:00")
            self.progress.progress = 0
        self.music_dir = path
        self.full_playlist = songs
        self._apply_search()
        if stop:
            await asyncio.to_thread(self.player.stop)
        if generation == self._scan_generation and not self.stop_threads:
            await self.action_save()

    async def action_save(self):
        index = self._find_song_in_full_playlist(self.current_song_path)
        self.cfg.update(last_index=max(0, index), music_dir=self.music_dir, volume=self.volume)
        cfg = self.cfg.copy()
        playlist = [song.copy() for song in self.full_playlist]
        def save():
            save_playlist_file(playlist)
            save_config(cfg)
        try:
            async with self._save_lock:
                await asyncio.to_thread(save)
            return True
        except Exception:
            logging.exception("Unable to save settings/library")
            self._set_status("Save failed; check disk space and permissions, then retry")
            return False

    async def action_quit(self):
        await self.action_save_and_exit()

    async def action_save_and_exit(self):
        self.stop_threads = True
        self._cancel_scans()
        if not await self.action_save():
            self.stop_threads = False
            return
        self._reset_playback_state()
        await self._close_player()
        self.exit()


def run_tui(*, splash=True):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=LOG_DIR / "wavrun_debug.log", level=logging.INFO,
                            format="%(asctime)s [%(threadName)s] %(message)s")
    except OSError:
        logging.basicConfig(level=logging.ERROR)
    if splash:
        show_splash()
    app = wavrun()
    try:
        app.run()
    finally:
        app.player.close()
