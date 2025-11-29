# core/metadata.py
import os
try:
    from mutagen import File as MutagenFile
    HAS_MUTAGEN = True
except Exception:
    HAS_MUTAGEN = False

def get_metadata(filepath):
    """Return dict {title, artist, length_seconds}"""
    title = os.path.basename(filepath)
    artist = "Unknown"
    length = None
    if HAS_MUTAGEN:
        try:
            audio = MutagenFile(filepath, easy=True)
            if audio:
                t = audio.get("title") or audio.get("TIT2") or audio.get("TITLE")
                a = audio.get("artist") or audio.get("TPE1") or audio.get("ARTIST")
                if t:
                    title = t[0] if isinstance(t, (list,tuple)) else str(t)
                if a:
                    artist = a[0] if isinstance(a, (list,tuple)) else str(a)
                if hasattr(audio, "info") and getattr(audio.info, "length", None):
                    length = float(audio.info.length)
        except Exception:
            pass
    return {"title": title, "artist": artist, "length": length}
