#!/usr/bin/env python3
"""
wavrun_vlc_dark.py
Modern dark-themed music player using python-vlc backend.

Requirements:
    - python-vlc (pip install python-vlc)
    - mutagen (pip install mutagen)

Features:
    - Single-file self-contained player
    - VLC backend with reliable end-of-track detection
    - Dark, modern tkinter UI (no external GUI frameworks required)
    - Playlist add/save/load, metadata (title/artist/duration), star ratings
    - Play/Pause, Next, Prev, Shuffle, Repeat, Seek by clicking progress bar
    - Volume slider, persistent last-folder & last-index in wavrun_config.json
"""

import os
import sys
import json
import random
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

# Try mutagen for metadata
try:
    from mutagen import File as MutagenFile
    HAS_MUTAGEN = True
except Exception:
    HAS_MUTAGEN = False

# ---------- VLC wrapper ----------
try:
    import vlc
except Exception as e:
    print("python-vlc is required. Install with: pip install python-vlc")
    raise e

class VLCMusic:
    """Simple VLC wrapper compatible with the previous pygame-style API."""
    def __init__(self):
        self.instance = vlc.Instance('--no-video')
        self.player = self.instance.media_player_new()
        self.current_media = None
        self._end_callback = None
        # Keep track of whether we attached events for the current media
        self._events_attached = False
        self.set_volume(0.7)

    def load(self, filepath):
        media = self.instance.media_new(str(filepath))
        self.player.set_media(media)
        self.current_media = media
        self._events_attached = False

    def play(self):
        self.player.play()
        # let it spin up a little
        time.sleep(0.05)

    def pause(self):
        # toggle only if playing
        if self.player.is_playing():
            self.player.pause()

    def unpause(self):
        # If not playing, toggling pause will resume
        if not self.player.is_playing():
            self.player.pause()

    def stop(self):
        try:
            self.player.stop()
        except Exception:
            pass

    def set_volume(self, v):
        """v: 0.0 - 1.0"""
        try:
            vol = int(max(0.0, min(1.0, float(v))) * 100)
            self.player.audio_set_volume(vol)
            self._volume = vol
        except Exception:
            pass

    def get_busy(self):
        return bool(self.player.is_playing())

    def get_pos(self):
        """Return current time in milliseconds (>=0) or 0"""
        t = self.player.get_time()
        return t if (t and t >= 0) else 0

    def get_length(self):
        """Return length in milliseconds or None"""
        length = self.player.get_length()
        return length if (length and length > 0) else None

    def set_time(self, ms):
        """Seek to absolute time (ms)."""
        try:
            self.player.set_time(int(ms))
        except Exception:
            pass

    def add_end_callback(self, callback):
        """Attach MediaPlayerEndReached event to callback (zero-arg)."""
        self._end_callback = callback
        try:
            em = self.player.event_manager()
            # detach old events if any (no direct detach; attaching repeatedly duplicates; guard)
            # Attach callback only once per media
            if not self._events_attached:
                em.event_attach(vlc.EventType.MediaPlayerEndReached, lambda e: self._raise_end())
                self._events_attached = True
        except Exception:
            pass

    def _raise_end(self):
        if callable(self._end_callback):
            try:
                self._end_callback()
            except Exception:
                pass

# ---------- Config paths ----------
CONFIG_FILE = Path("wavrun_config.json")
PLAYLIST_FILE = Path("wavrun_playlist.json")

# ---------- Utilities ----------
def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

def load_playlist_file():
    if PLAYLIST_FILE.exists():
        try:
            with open(PLAYLIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_playlist_file(pl):
    try:
        with open(PLAYLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(pl, f, indent=2)
    except Exception:
        pass

def format_time_seconds(seconds):
    if seconds is None:
        return "??:??"
    try:
        s = int(seconds)
        m, s = divmod(s, 60)
        return f"{m:02d}:{s:02d}"
    except Exception:
        return "??:??"

def get_metadata(filepath):
    """Return dict with title, artist, length (seconds). Try mutagen, fallback to filename."""
    title = os.path.basename(filepath)
    artist = "Unknown"
    length = None
    if HAS_MUTAGEN:
        try:
            audio = MutagenFile(filepath, easy=True)
            if audio:
                # easy tags
                t = audio.get("title") or audio.get("TIT2") or audio.get("TITLE")
                a = audio.get("artist") or audio.get("TPE1") or audio.get("ARTIST")
                if t:
                    title = t[0] if isinstance(t, (list, tuple)) else str(t)
                if a:
                    artist = a[0] if isinstance(a, (list, tuple)) else str(a)
                if hasattr(audio, "info") and getattr(audio.info, "length", None):
                    length = float(audio.info.length)
        except Exception:
            pass
    return {"title": title, "artist": artist, "length": length}

# ---------- The App ----------
class WavRunApp:
    def __init__(self, root):
        self.root = root
        self.root.title("wavrun — Dark")
        self.root.geometry("960x640")
        # dark palette
        self.bg = "#0f1115"
        self.card = "#15171b"
        self.fg = "#e6eef6"
        self.sub = "#9fb3c8"
        self.accent = "#6BE4B4"  # mint-green accent
        self.gray = "#2a2e33"
        self.root.configure(bg=self.bg)

        self.player = VLCMusic()
        self.song_ended = threading.Event()

        # State
        self.playlist = load_playlist_file()  # list of dict {path,title,artist,rating}
        self.current_index = -1
        self.playing = False
        self.paused = False
        self.autoplay = True
        self.shuffle = False
        self.repeat_mode = "off"  # off|one|all

        # load saved config
        cfg = load_config()
        self.last_folder = cfg.get("last_folder", os.path.expanduser("~"))
        self.last_index = int(cfg.get("last_index", 0)) if cfg.get("last_index") is not None else 0

        # UI building
        self._build_ui()

        # Start progress updater thread
        self._stop_threads = False
        self.progress_thread = threading.Thread(target=self._progress_loop, daemon=True)
        self.progress_thread.start()

        # If we have a playlist and last_index valid, resume paused
        if self.playlist and 0 <= self.last_index < len(self.playlist):
            self.current_index = self.last_index
            self._load_metadata_labels(self.current_index)
            # Do not auto-play until user asks (consistent with earlier behavior)
        self._refresh_playlist_view()

    # ---------------- UI ----------------
    def _build_ui(self):
        # top bar
        top = tk.Frame(self.root, bg=self.bg)
        top.pack(fill="x", padx=12, pady=10)

        title = tk.Label(top, text="wavrun", bg=self.bg, fg=self.accent, font=("Inter", 20, "bold"))
        title.pack(side="left")

        spacer = tk.Frame(top, bg=self.bg)
        spacer.pack(side="left", expand=True)

        # Search
        self.search_var = tk.StringVar()
        search_entry = tk.Entry(top, textvariable=self.search_var, bg="#101214", fg=self.fg, insertbackground=self.fg,
                                relief="flat", width=30)
        search_entry.pack(side="right", padx=(0, 6))
        search_entry.bind("<Return>", lambda e: self._apply_filter())

        search_label = tk.Label(top, text="Search", bg=self.bg, fg=self.sub)
        search_label.pack(side="right", padx=(0,6))

        # main area
        main = tk.Frame(self.root, bg=self.bg)
        main.pack(fill="both", expand=True, padx=12, pady=(0,12))

        # left: playlist
        left = tk.Frame(main, bg=self.card)
        left.pack(side="left", fill="both", expand=True)

        left_top = tk.Frame(left, bg=self.card)
        left_top.pack(fill="x", pady=8, padx=8)

        add_btn = ttk.Style()
        add_btn.configure("Accent.TButton", foreground=self.fg, background=self.accent)
        self.add_button = ttk.Button(left_top, text="Add Files", command=self._add_files, style="Accent.TButton")
        self.add_button.pack(side="left", padx=(0,6))

        add_folder = ttk.Button(left_top, text="Add Folder", command=self._add_folder)
        add_folder.pack(side="left", padx=6)

        save_button = ttk.Button(left_top, text="Save Playlist", command=self._save_playlist)
        save_button.pack(side="left", padx=6)

        # Playlist canvas
        self.play_canvas = tk.Canvas(left, bg=self.card, bd=0, highlightthickness=0)
        self.play_scroll = ttk.Scrollbar(left, orient="vertical", command=self.play_canvas.yview)
        self.playlist_frame = tk.Frame(self.play_canvas, bg=self.card)

        self.playlist_frame.bind("<Configure>", lambda e: self.play_canvas.configure(scrollregion=self.play_canvas.bbox("all")))
        self.play_canvas.create_window((0,0), window=self.playlist_frame, anchor="nw")
        self.play_canvas.configure(yscrollcommand=self.play_scroll.set)

        self.play_canvas.pack(side="left", fill="both", expand=True, padx=(8,0), pady=8)
        self.play_scroll.pack(side="right", fill="y", pady=8, padx=(0,8))

        # right: player card
        right = tk.Frame(main, bg=self.card, width=360, height=420)
        right.pack(side="right", fill="y", padx=(12,0), pady=8)
        right.pack_propagate(False)

        # song title & artist
        self.lbl_title = tk.Label(right, text="No song selected", bg=self.card, fg=self.fg, font=("Inter", 16, "bold"), wraplength=320, justify="center")
        self.lbl_title.pack(pady=(18,2))

        self.lbl_artist = tk.Label(right, text="", bg=self.card, fg=self.sub, font=("Inter", 12), wraplength=320, justify="center")
        self.lbl_artist.pack(pady=(0,6))

        # progress bar + time
        progress_container = tk.Frame(right, bg=self.card)
        progress_container.pack(fill="x", padx=16, pady=(12,6))

        self.time_label = tk.Label(progress_container, text="00:00", bg=self.card, fg=self.sub, font=("Inter", 10))
        self.time_label.pack(side="left")

        self.progress_var = tk.DoubleVar(value=0.0)
        style = ttk.Style()
        style.theme_use('default')
        style.configure("dark.Horizontal.TProgressbar", troughcolor=self.gray, background=self.accent, thickness=12)
        self.progress = ttk.Progressbar(progress_container, orient="horizontal", style="dark.Horizontal.TProgressbar", variable=self.progress_var, maximum=100.0)
        self.progress.pack(side="left", fill="x", expand=True, padx=8)
        self.progress.bind("<Button-1>", self._on_progress_click)

        self.length_label = tk.Label(progress_container, text="00:00", bg=self.card, fg=self.sub, font=("Inter", 10))
        self.length_label.pack(side="right")

        # controls (prev, play/pause, next) and extra controls
        ctrl_frame = tk.Frame(right, bg=self.card)
        ctrl_frame.pack(pady=8)

        self.btn_prev = ttk.Button(ctrl_frame, text="⏮", command=self._prev)
        self.btn_prev.grid(row=0, column=0, padx=6)

        self.btn_play = ttk.Button(ctrl_frame, text="▶", command=self._play_pause)
        self.btn_play.grid(row=0, column=1, padx=6)

        self.btn_next = ttk.Button(ctrl_frame, text="⏭", command=self._next)
        self.btn_next.grid(row=0, column=2, padx=6)

        # bottom controls: shuffle, repeat, volume, rating
        bottom = tk.Frame(right, bg=self.card)
        bottom.pack(fill="x", padx=16, pady=(8,12))

        self.shuffle_btn = ttk.Button(bottom, text="Shuffle", command=self._toggle_shuffle)
        self.shuffle_btn.pack(side="left", padx=4)

        self.repeat_btn = ttk.Button(bottom, text="Repeat: Off", command=self._cycle_repeat)
        self.repeat_btn.pack(side="left", padx=4)

        vol_frame = tk.Frame(bottom, bg=self.card)
        vol_frame.pack(side="right")
        tk.Label(vol_frame, text="Vol", bg=self.card, fg=self.sub).pack(side="left", padx=(0,6))
        self.vol_var = tk.DoubleVar(value=0.7)
        vol_slider = ttk.Scale(vol_frame, from_=0.0, to=1.0, orient="horizontal", variable=self.vol_var, command=self._on_volume_change, length=120)
        vol_slider.pack(side="left")

        # rating area
        self.stars_frame = tk.Frame(right, bg=self.card)
        self.stars_frame.pack(pady=(6,12))
        self.star_widgets = []
        for i in range(5):
            lbl = tk.Label(self.stars_frame, text="☆", bg=self.card, fg=self.sub, font=("Inter", 16))
            lbl.pack(side="left", padx=4)
            lbl.bind("<Button-1>", lambda e, idx=i: self._set_rating(idx+1))
            self.star_widgets.append(lbl)

        # keyboard bindings
        self.root.bind("<space>", lambda e: self._play_pause())
        self.root.bind("<Left>", lambda e: self._prev())
        self.root.bind("<Right>", lambda e: self._next())

    # ---------------- Playlist UI helpers ----------------
    def _refresh_playlist_view(self, filter_term=None):
        # Clear frame
        for w in self.playlist_frame.winfo_children():
            w.destroy()

        filter_term = (filter_term or "").strip().lower()
        for idx, item in enumerate(self.playlist):
            title = item.get("title") or os.path.basename(item["path"])
            artist = item.get("artist") or "Unknown"
            if filter_term:
                if filter_term not in title.lower() and filter_term not in artist.lower() and filter_term not in item["path"].lower():
                    continue

            row = tk.Frame(self.playlist_frame, bg=self.card, pady=6)
            row.pack(fill="x", padx=6)

            txt = tk.Label(row, text=f"{title} — {artist}", bg=self.card, fg=self.fg, anchor="w", justify="left", wraplength=420)
            txt.pack(side="left", fill="x", expand=True)
            txt.bind("<Button-1>", lambda e, i=idx: self._select_and_play(i))

            # rating stars small
            r = item.get("rating", 0)
            star_lbl = tk.Label(row, text="".join("★" if s < r else "☆" for s in range(5)), bg=self.card, fg=self.accent)
            star_lbl.pack(side="right", padx=(6,0))
            star_lbl.bind("<Button-1>", lambda e, i=idx: self._cycle_item_rating(i))

            # highlight current
            if idx == self.current_index:
                row.configure(bg="#192026")
                txt.configure(bg="#192026")
                star_lbl.configure(bg="#192026")

    def _apply_filter(self):
        term = self.search_var.get().strip()
        self._refresh_playlist_view(term)

    # ---------------- Control actions ----------------
    def _add_files(self):
        files = filedialog.askopenfilenames(title="Select audio files", filetypes=[("Audio","*.mp3 *.flac *.wav *.m4a *.ogg"),("All","*.*")], initialdir=self.last_folder)
        if not files:
            return
        for f in files:
            f = str(f)
            meta = get_metadata(f)
            entry = {"path": f, "title": meta["title"], "artist": meta["artist"], "rating": 0}
            self.playlist.append(entry)
            self.last_folder = os.path.dirname(f)
        save_playlist_file(self.playlist)
        save_config({"last_folder": self.last_folder, "last_index": self.current_index})
        self._refresh_playlist_view()

    def _add_folder(self):
        folder = filedialog.askdirectory(title="Select folder with audio", initialdir=self.last_folder)
        if not folder:
            return
        # walk folder for audio
        exts = (".mp3", ".flac", ".wav", ".m4a", ".ogg")
        for root, dirs, files in os.walk(folder):
            for fn in files:
                if fn.lower().endswith(exts):
                    path = os.path.join(root, fn)
                    meta = get_metadata(path)
                    entry = {"path": path, "title": meta["title"], "artist": meta["artist"], "rating": 0}
                    self.playlist.append(entry)
                    self.last_folder = folder
        save_playlist_file(self.playlist)
        save_config({"last_folder": self.last_folder, "last_index": self.current_index})
        self._refresh_playlist_view()

    def _save_playlist(self):
        save_playlist_file(self.playlist)
        messagebox.showinfo("Saved", "Playlist saved to disk.")

    def _select_and_play(self, idx):
        self.current_index = idx
        self._play_index(idx)

    def _play_index(self, idx):
        if idx < 0 or idx >= len(self.playlist):
            return
        path = self.playlist[idx]["path"]
        if not os.path.exists(path):
            messagebox.showerror("File missing", f"File not found:\n{path}")
            return

        # load into VLC
        try:
            self.player.stop()
            time.sleep(0.02)
            self.player.load(path)
            # attach end callback to set song_ended flag
            self.player.add_end_callback(lambda: self.song_ended.set())
            self.player.play()

            # Ensure we can pause if first-load(s): wait until player reports time
            # Wait up to ~0.5s for playback to start, then pause if autoplay disabled
            started = False
            for _ in range(15):
                if self.player.get_busy():
                    started = True
                    break
                time.sleep(0.03)
            if not started:
                # still allow; not fatal
                pass

            # initial pause behavior: we start paused by default (mirrors prior behavior)
            if not hasattr(self, 'initial_play_happened') or not self.initial_play_happened:
                # pause right away, user presses play to start
                try:
                    self.player.pause()
                except Exception:
                    pass
                self.initial_play_happened = True
                self.playing = False
                self.paused = False
            else:
                self.playing = True
                self.paused = False

            self._load_metadata_labels(idx)
            self._refresh_playlist_view()

        except Exception as e:
            messagebox.showerror("Playback error", f"Could not play file:\n{e}")

    def _load_metadata_labels(self, idx):
        item = self.playlist[idx]
        self.lbl_title.config(text=item.get("title", os.path.basename(item["path"])))
        self.lbl_artist.config(text=item.get("artist", "Unknown"))
        # update stars
        r = item.get("rating", 0)
        for i, w in enumerate(self.star_widgets):
            w.config(text="★" if i < r else "☆", fg=self.accent if i < r else self.sub)
        # update length label by asking VLC after a short delay (some formats require time)
        def _set_length_label():
            length_ms = self.player.get_length()
            if length_ms:
                self.length_label.config(text=format_time_seconds(length_ms/1000.0))
            else:
                # fallback: check metadata cached length
                # We attempted to store length during add; if not, show ??:
                self.length_label.config(text="??:??")
        self.root.after(200, _set_length_label)

    def _play_pause(self):
        if self.playing:
            # pause
            try:
                self.player.pause()
            except Exception:
                pass
            self.playing = False
            self.paused = True
            self.btn_play.config(text="▶")
        else:
            # if no current song loaded, start at current_index or 0
            if self.current_index == -1 and self.playlist:
                # play first
                self.current_index = 0
                self._play_index(0)
                self.btn_play.config(text="⏸")
                self.playing = True
                self.paused = False
                return
            # unpause/resume
            try:
                self.player.unpause()
            except Exception:
                pass
            self.playing = True
            self.paused = False
            self.btn_play.config(text="⏸")

    def _prev(self):
        if not self.playlist:
            return
        if self.player.get_pos() > 3000:  # if >3s into song, restart
            self.player.set_time(0)
            return
        next_idx = self.current_index - 1 if self.current_index > 0 else (len(self.playlist)-1 if self.repeat_mode == "all" else 0)
        self.current_index = next_idx
        self._play_index(self.current_index)
        self.btn_play.config(text="⏸")
        self.playing = True

    def _next(self):
        if not self.playlist:
            return
        if self.shuffle:
            next_idx = random.randrange(len(self.playlist))
        else:
            next_idx = self.current_index + 1
            if next_idx >= len(self.playlist):
                if self.repeat_mode == "all":
                    next_idx = 0
                else:
                    # stop playback at end
                    self.player.stop()
                    self.playing = False
                    self.btn_play.config(text="▶")
                    return
        self.current_index = next_idx
        self._play_index(self.current_index)
        self.btn_play.config(text="⏸")
        self.playing = True

    def _toggle_shuffle(self):
        self.shuffle = not self.shuffle
        self.shuffle_btn.config(text="Shuffle ✓" if self.shuffle else "Shuffle")

    def _cycle_repeat(self):
        if self.repeat_mode == "off":
            self.repeat_mode = "one"
            self.repeat_btn.config(text="Repeat: One")
        elif self.repeat_mode == "one":
            self.repeat_mode = "all"
            self.repeat_btn.config(text="Repeat: All")
        else:
            self.repeat_mode = "off"
            self.repeat_btn.config(text="Repeat: Off")

    def _on_volume_change(self, v):
        try:
            self.player.set_volume(float(v))
        except Exception:
            pass

    def _set_rating(self, rating):
        if self.current_index >= 0 and self.current_index < len(self.playlist):
            self.playlist[self.current_index]["rating"] = rating
            save_playlist_file(self.playlist)
            self._load_metadata_labels(self.current_index)
            self._refresh_playlist_view()

    def _cycle_item_rating(self, idx):
        cur = self.playlist[idx].get("rating", 0)
        new = (cur + 1) % 6
        self.playlist[idx]["rating"] = new
        save_playlist_file(self.playlist)
        self._refresh_playlist_view()

    def _on_progress_click(self, event):
        # click-based seeking (estimate position)
        try:
            width = self.progress.winfo_width()
            click_x = event.x
            frac = max(0.0, min(1.0, click_x / width))
            length_ms = self.player.get_length()
            if length_ms and length_ms > 0:
                target = frac * length_ms
                self.player.set_time(int(target))
        except Exception:
            pass

    # ---------------- Progress loop (updates UI) ----------------
    def _progress_loop(self):
        while not self._stop_threads:
            try:
                if self.playing:
                    pos_ms = self.player.get_pos()
                    length_ms = self.player.get_length()
                    pos_s = (pos_ms / 1000.0) if pos_ms else 0.0
                    len_s = (length_ms / 1000.0) if length_ms else None

                    # update UI in main thread
                    def _update():
                        self.time_label.config(text=format_time_seconds(pos_s))
                        if len_s:
                            self.length_label.config(text=format_time_seconds(len_s))
                            percent = (pos_s / len_s) * 100 if len_s > 0 else 0.0
                            self.progress_var.set(percent)
                        else:
                            self.progress_var.set(0.0)
                    self.root.after(0, _update)
                else:
                    # even when paused, still update position label
                    pos_ms = self.player.get_pos()
                    pos_s = (pos_ms / 1000.0) if pos_ms else 0.0
                    def _update_paused():
                        self.time_label.config(text=format_time_seconds(pos_s))
                    self.root.after(0, _update_paused)

                # handle end-of-track event
                if self.song_ended.is_set():
                    self.song_ended.clear()
                    # handle according to repeat_mode / queue / shuffle
                    if self.repeat_mode == "one":
                        # replay same
                        self._play_index(self.current_index)
                    else:
                        # advance
                        if self.shuffle:
                            self.current_index = random.randrange(len(self.playlist))
                            self._play_index(self.current_index)
                        else:
                            next_idx = self.current_index + 1
                            if next_idx >= len(self.playlist):
                                if self.repeat_mode == "all":
                                    next_idx = 0
                                    self.current_index = next_idx
                                    self._play_index(self.current_index)
                                else:
                                    # stop playback
                                    self.playing = False
                                    self.root.after(0, lambda: self.btn_play.config(text="▶"))
                            else:
                                self.current_index = next_idx
                                self._play_index(self.current_index)

                time.sleep(0.25)
            except Exception:
                time.sleep(0.3)

    # ---------------- Shutdown ----------------
    def shutdown(self):
        self._stop_threads = True
        try:
            # save last folder & index
            cfg = {"last_folder": self.last_folder, "last_index": self.current_index}
            save_config(cfg)
            save_playlist_file(self.playlist)
            # stop player
            self.player.stop()
        except Exception:
            pass

# ---------- Run ----------
def main():
    root = tk.Tk()
    # make ttk match dark background (basic)
    style = ttk.Style(root)
    # On some platforms 'clam' supports better styling
    try:
        style.theme_use('clam')
    except Exception:
        pass

    app = WavRunApp(root)

    def on_close():
        app.shutdown()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()

if __name__ == "__main__":
    main()
