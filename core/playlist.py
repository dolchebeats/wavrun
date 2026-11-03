# core/playlist.py
import os
import json
import logging
import math
from pathlib import Path
from .metadata import get_metadata
from .storage import APP_DIR, CONFIG_DIR, atomic_write_json

PLAYLIST_FILE = CONFIG_DIR / "playlist.json"

def scan_folder(folder, exts=None):
    folder = Path(folder).expanduser().resolve()
    if not os.path.isdir(folder):
        raise NotADirectoryError(f"Music folder is not a directory: {folder}")

    def raise_scan_error(error):
        # Do not replace a saved library with a partial scan of an unreadable folder.
        raise error

    if exts is None:
        exts = (".mp3", ".flac", ".wav", ".ogg", ".m4a")
    song_files = []
    for root, dirs, files in os.walk(folder, onerror=raise_scan_error):
        for fn in files:
            if fn.lower().endswith(exts):
                path = os.path.join(root, fn)
                meta = get_metadata(path)
                entry = {"path": path, "title": meta["title"], "artist": meta["artist"], "rating": 0, "length": meta["length"]}
                song_files.append(entry)

    song_files.sort(key=lambda song: song["path"].casefold())
    return song_files

def make_playlist_from_paths(paths):
    pl = []
    for p in paths:
        meta = get_metadata(p)
        pl.append({"path": p, "title": meta["title"], "artist": meta["artist"], "rating": 0, "length": meta["length"]})
    return pl

def load_playlist_file():
    try:
        with PLAYLIST_FILE.open(encoding="utf-8") as stream:
            entries = json.load(stream)
        if not isinstance(entries, list):
            raise ValueError("Playlist must be a list")
    except FileNotFoundError:
        return []
    except (OSError, ValueError, UnicodeError):
        logging.warning("Unable to load playlist %s", PLAYLIST_FILE, exc_info=True)
        return []
    playlist = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            logging.warning("Ignoring invalid playlist entry")
            continue
        if not entry["path"].strip() or "\0" in entry["path"]:
            continue
        path = Path(entry["path"]).expanduser()
        path = str(path if path.is_absolute() else APP_DIR / path)
        if path in seen:
            continue
        seen.add(path)
        song = {key: entry.get(key) for key in ("title", "artist", "length", "rating")}
        song["path"] = path
        for key, default in (("title", Path(path).name), ("artist", "Unknown")):
            if not isinstance(song.get(key), str) or not song[key]:
                song[key] = default
        for key, default in (("length", None), ("rating", 0)):
            value = song.get(key)
            try:
                valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
            except OverflowError:
                valid = False
            song[key] = value if valid else default
        if song["length"] == 0:
            song["length"] = None
        playlist.append(song)
    return playlist

def save_playlist_file(playlist):
    atomic_write_json(PLAYLIST_FILE, playlist)
