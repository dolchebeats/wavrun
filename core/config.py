# core/config.py 
import json
import logging
from pathlib import Path
import math
from .storage import CONFIG_DIR, LEGACY_CONFIG_DIR, atomic_write_json, settings_source

CONFIG_FILE = CONFIG_DIR / "config.json"

def load_config():
    source = settings_source(CONFIG_FILE, "config.json")
    try:
        with source.open(encoding="utf-8") as stream:
            cfg = json.load(stream)
        if not isinstance(cfg, dict):
            raise ValueError("Config must be an object")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError):
        logging.warning("Unable to load config %s", CONFIG_FILE, exc_info=True)
        return {}
    index = cfg.get("last_index", 0)
    cfg["last_index"] = index if type(index) is int and index >= 0 else 0
    folder = cfg.get("music_dir")
    if isinstance(folder, str) and folder.strip() and "\0" not in folder:
        path = Path(folder).expanduser()
        base = LEGACY_CONFIG_DIR.parent if source.parent == LEGACY_CONFIG_DIR else source.parent
        cfg["music_dir"] = str(path if path.is_absolute() else base / path)
    else:
        cfg["music_dir"] = None
    volume = cfg.get("volume", 0.7)
    try:
        cfg["volume"] = (float(volume) if type(volume) in (int, float) and
                         math.isfinite(volume) and 0 <= volume <= 1 else 0.7)
    except OverflowError:
        cfg["volume"] = 0.7
    return cfg

def save_config(new_cfg: dict):
    cfg = load_config()  # always start with existing config
    cfg.update(new_cfg)   # merge new values
    atomic_write_json(CONFIG_FILE, cfg)

