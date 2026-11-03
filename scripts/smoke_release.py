"""Validate a built bundle using only its executable and native libraries."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import wave


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    args = parser.parse_args()
    executable = args.executable.resolve()
    with tempfile.TemporaryDirectory() as folder:
        environment = dict(os.environ, WAVRUN_HOME=str(Path(folder) / "settings"))
        # Do not allow a developer's Python/VLC environment to supply the runtime.
        for key in ("PYTHONPATH", "PYTHONHOME", "VLC_PLUGIN_PATH", "PYTHON_VLC_LIB_PATH"):
            environment.pop(key, None)
        for argument in ("--diagnostics", "--self-test"):
            result = subprocess.run([str(executable), argument], cwd=folder, env=environment,
                                    check=True, capture_output=True, text=True, timeout=30)
            info = json.loads(result.stdout)
            assert info["splash"] and info["stylesheet"] and not info["vlc_loaded"], info
            if argument == "--self-test":
                assert info["ui_closed_cleanly"], info
        path = Path(folder) / "silence.wav"
        with wave.open(str(path), "wb") as output:
            output.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
            output.writeframes(b"\0" * 4410 * 4)
        result = subprocess.run([str(executable), "--decode-only", str(path)], cwd=folder,
                                env=environment, check=True, capture_output=True, text=True, timeout=30)
        info = json.loads(result.stdout)
        assert info["frames"] == 4800 and info["channels"] == 2, info
    print("Bundle smoke checks passed: resources, headless UI, spawned decoder, and no VLC import.")


if __name__ == "__main__":
    main()
