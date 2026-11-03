import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from core import config, playlist, storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def test_legacy_settings_are_read_and_saved_without_modifying_originals(self):
        legacy = self.folder / "legacy" / "config"
        legacy.mkdir(parents=True)
        old_config = {"music_dir": "music", "last_index": 2, "custom": 42}
        (legacy / "config.json").write_text(json.dumps(old_config), encoding="utf-8")
        (legacy / "playlist.json").write_text('[{"path":"music/song.wav","title":null}]', encoding="utf-8")
        new_config = self.folder / "home" / "config" / "config.json"
        new_playlist = self.folder / "home" / "data" / "playlist.json"
        with patch.object(storage, "LEGACY_CONFIG_DIR", legacy), \
                patch.object(config, "LEGACY_CONFIG_DIR", legacy), \
                patch.object(playlist, "LEGACY_CONFIG_DIR", legacy), \
                patch.object(config, "CONFIG_FILE", new_config), \
                patch.object(playlist, "PLAYLIST_FILE", new_playlist):
            cfg = config.load_config()
            songs = playlist.load_playlist_file()
            self.assertEqual(cfg["music_dir"], str(legacy.parent / "music"))
            self.assertEqual(songs[0]["path"], str(legacy.parent / "music" / "song.wav"))
            self.assertEqual(songs[0]["title"], "song.wav")
            config.save_config(dict(cfg, volume=0.3))
            playlist.save_playlist_file(songs)
            self.assertEqual(config.load_config()["volume"], 0.3)
            self.assertEqual(config.load_config()["custom"], 42)
        self.assertEqual(json.loads((legacy / "config.json").read_text()), old_config)
        self.assertTrue(new_config.is_file() and new_playlist.is_file())

    def test_new_settings_take_precedence_over_legacy(self):
        target = self.folder / "config.json"
        target.write_text("{}", encoding="utf-8")
        self.assertEqual(storage.settings_source(target, "config.json"), target)

    def test_portable_home_is_independent_of_launch_directory(self):
        root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ, WAVRUN_HOME=str(self.folder / "portable"))
        script = """
import json,sys
sys.path.insert(0,sys.argv[1])
from core.storage import APP_DIR,CONFIG_DIR,DATA_DIR,LOG_DIR
print(json.dumps([str(p) for p in (APP_DIR,CONFIG_DIR,DATA_DIR,LOG_DIR)]))
"""
        result = subprocess.run([sys.executable, "-c", script, str(root)], cwd=self.folder,
                                env=environment, capture_output=True, text=True, check=True)
        paths = json.loads(result.stdout)
        self.assertEqual(Path(paths[0]), root)
        self.assertEqual(Path(paths[1]), self.folder / "portable" / "config")
        self.assertEqual(Path(paths[2]), self.folder / "portable" / "data")
        self.assertEqual(Path(paths[3]), self.folder / "portable" / "logs")

    def test_invalid_volume_does_not_break_config(self):
        target = self.folder / "config.json"
        with patch.object(config, "CONFIG_FILE", target):
            for value in (float("nan"), 10 ** 400, -1, 5, "loud", None):
                target.write_text(json.dumps({"volume": value}), encoding="utf-8")
                self.assertEqual(config.load_config()["volume"], 0.7)

    def test_atomic_failure_preserves_existing_file(self):
        target = self.folder / "settings.json"
        storage.atomic_write_json(target, {"original": True})
        with patch.object(storage.os, "replace", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                storage.atomic_write_json(target, {"replacement": True})
        self.assertEqual(json.loads(target.read_text()), {"original": True})
        self.assertEqual(list(self.folder.iterdir()), [target])
