"""Build a terminal bundle on the current OS; no cross-compilation or publishing."""
import ctypes
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def collect_notices(bundle):
    destination = bundle / "licenses"
    destination.mkdir(exist_ok=True)
    pending = ["av", "numpy", "sounddevice", "mutagen", "textual", "rich", "platformdirs"]
    packages = {}
    while pending:
        name = pending.pop()
        distribution = metadata.distribution(name)
        name = distribution.metadata["Name"]
        if name in packages:
            continue
        directory = destination / name
        directory.mkdir(exist_ok=True)
        copied = []
        for index, file in enumerate(distribution.files or []):
            lower = str(file).lower()
            if (any(word in Path(lower).name for word in ("license", "copying", "notice")) or
                    "/licenses/" in lower) and "__pycache__" not in lower:
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    target = directory / f"{index}-{source.name}"
                    shutil.copy2(source, target)
                    copied.append(str(target.relative_to(bundle)))
        packages[name] = {"version": distribution.version,
                          "license": distribution.metadata.get("License-Expression") or
                                     distribution.metadata.get("License"), "notice_files": copied}
        for raw in distribution.requires or []:
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
    (destination / "packages.json").write_text(json.dumps(packages, indent=2), encoding="utf-8")
    # Record the actual wheel's native FFmpeg configuration/license, not an assumed one.
    import av
    native = {"versions": av.library_versions, "libraries": []}
    for file in metadata.distribution("av").files or []:
        path = Path(metadata.distribution("av").locate_file(file))
        if "avcodec" not in path.name or not any(suffix in path.name for suffix in (".dll", ".so", ".dylib")):
            continue
        try:
            library = ctypes.CDLL(str(path))
            details = {"file": path.name}
            for key in ("license", "configuration"):
                function = getattr(library, f"avcodec_{key}")
                function.restype = ctypes.c_char_p
                details[key] = function().decode("utf-8", errors="replace")
            native["libraries"].append(details)
        except (OSError, AttributeError):
            continue
    (destination / "ffmpeg-build.json").write_text(json.dumps(native, indent=2), encoding="utf-8")


def main():
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--console",
               "--noupx", "--name", "wavrun", "--distpath", str(ROOT / "dist"),
               "--workpath", str(ROOT / "build" / "pyinstaller"),
               "--specpath", str(ROOT / "build"), "--exclude-module", "vlc",
               "--add-data", f"{ROOT / 'splash.txt'}:.",
               "--add-data", f"{ROOT / 'core' / 'wavrun.css'}:core"]
    for package in ("av", "textual", "rich", "_sounddevice_data"):
        command += ["--collect-all", package]
    command.append(str(ROOT / "main.py"))
    environment = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(ROOT / "build" / "pyinstaller-cache"))
    subprocess.run(command, cwd=ROOT, env=environment, check=True)
    bundle = ROOT / "dist" / "wavrun"
    collect_notices(bundle)
    shutil.copy2(ROOT / "LICENSE", bundle / "LICENSE")
    shutil.copy2(ROOT / "README.md", bundle / "README.md")
    shutil.copy2(ROOT / "docs" / "distribution.md", bundle / "DISTRIBUTION.md")
    shutil.copytree(ROOT / "docs", bundle / "docs", dirs_exist_ok=True)
    # Include this project's source, never personal settings, logs, or the environment.
    source = bundle / "source"
    source.mkdir(exist_ok=True)
    for name in ("main.py", "LICENSE", "README.md", "requirements.txt", "requirements-playback.txt",
                 "requirements-build.txt", "splash.txt"):
        shutil.copy2(ROOT / name, source / name)
    for directory in ("core", "docs", "tests", "scripts", ".github"):
        shutil.copytree(ROOT / directory, source / directory, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    binaries = {}
    for path in bundle.rglob("*"):
        if path.is_file() and (path.suffix.lower() in (".dll", ".dylib", ".so", ".pyd", ".exe")
                               or ".so." in path.name):
            binaries[str(path.relative_to(bundle))] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"platform": platform.platform(), "machine": platform.machine(),
                "python": platform.python_version(), "binary_sha256": binaries}
    (bundle / "build-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Built {bundle}")


if __name__ == "__main__":
    main()
