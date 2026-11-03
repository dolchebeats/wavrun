"""Stable application paths and atomic JSON persistence."""
import json
import os
import tempfile
import sys
from pathlib import Path
from platformdirs import user_config_path, user_data_path, user_log_path

APP_DIR = Path(__file__).resolve().parent.parent
LEGACY_CONFIG_DIR = (Path(sys.executable).parent if getattr(sys, "frozen", False) else APP_DIR) / "config"
_override = os.environ.get("WAVRUN_HOME")
if _override:
    _home = Path(_override).expanduser().resolve()
    CONFIG_DIR, DATA_DIR, LOG_DIR = _home / "config", _home / "data", _home / "logs"
else:
    CONFIG_DIR = user_config_path("wavrun", appauthor=False)
    DATA_DIR = user_data_path("wavrun", appauthor=False)
    LOG_DIR = user_log_path("wavrun", appauthor=False)


def settings_source(target, filename):
    """Read old settings until the first successful save to the new location."""
    legacy = LEGACY_CONFIG_DIR / filename
    if not target.exists() and legacy.is_file():
        return legacy
    return target


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
