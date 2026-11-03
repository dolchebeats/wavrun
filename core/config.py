# core/config.py 
import json
import logging
from pathlib import Path
from .storage import APP_DIR, CONFIG_DIR, atomic_write_json

CONFIG_FILE = CONFIG_DIR / "config.json"

def load_config():
    try:
        with CONFIG_FILE.open(encoding="utf-8") as stream:
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
        cfg["music_dir"] = str(path if path.is_absolute() else APP_DIR / path)
    else:
        cfg["music_dir"] = None
    return cfg

def save_config(new_cfg: dict):
    cfg = load_config()  # always start with existing config
    cfg.update(new_cfg)   # merge new values
    atomic_write_json(CONFIG_FILE, cfg)

