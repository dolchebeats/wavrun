# core/playlist.py
import os
import json
from pathlib import Path
from .metadata import get_metadata

PLAYLIST_FILE = Path("playlist.json")

def scan_folder(folder, exts=None):
    if exts is None:
        exts = (".mp3", ".flac", ".wav", ".ogg", ".m4a")
    song_files = []
    for root, dirs, files in os.walk(folder):
        for fn in files:
            if fn.lower().endswith(exts):
                path = os.path.join(root, fn)
                meta = get_metadata(path)
                entry = {"path": path, "title": meta["title"], "artist": meta["artist"], "rating": 0}
                song_files.append(entry)

    #song_files.sort()
    return song_files

def make_playlist_from_paths(paths):
    pl = []
    for p in paths:
        meta = get_metadata(p)
        pl.append({"path": p, "title": meta["title"], "artist": meta["artist"], "rating": 0})
    return pl

def load_playlist_file():
    if PLAYLIST_FILE.exists():
        try:
            with open(PLAYLIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_playlist_file(playlist):
    try:
        with open(PLAYLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(playlist, f, indent=2)
    except Exception:
        pass
