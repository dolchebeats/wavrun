# core/config.py
import json
from pathlib import Path

CONFIG_FILE = Path("config.json")

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            print("config file no good.")
            return {}
    return {}

def save_config(new_cfg: dict):
    cfg = load_config()  # always start with existing config
    cfg.update(new_cfg)   # merge new values
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

