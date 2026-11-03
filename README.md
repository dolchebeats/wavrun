# Wavrun

A terminal music player for Windows, macOS, and Linux, using PyAV/FFmpeg for
decoding and sounddevice/PortAudio for audio output. VLC is not required.

## Run from source

Use Python 3.12 or newer with compatible binary wheels, in a virtual environment:

```sh
python -m pip install -r requirements.txt
python main.py
```

On Debian/Ubuntu source installations, install `libportaudio2` using the system
package manager. Windows/macOS sounddevice wheels include PortAudio.

The splash displays for two seconds. Press `g` to choose a music folder; supported
formats are MP3, FLAC, WAV, OGG, and M4A/AAC. Files may be on mounted network shares.

| Control | Action |
| --- | --- |
| Space | Play/pause |
| Enter / click | Play highlighted track |
| n / p | Next / previous in the full library |
| Left / Right | Seek backward / forward five seconds |
| + or = / - | Raise / lower volume |
| s / r | Shuffle / cycle repeat mode |
| / / Escape | Search / clear search |
| j / k | Move in playlist |
| g / F5 | Change music folder / refresh library |
| q | Save and quit |

Settings, playlists, and logs use platform-specific per-user directories. On first
use, existing `config/config.json` and `config/playlist.json` beside the source or
executable are read as a fallback; successful saves move settings to the new
locations without deleting the originals. `WAVRUN_HOME` overrides all writable
directories for portable use or testing. Run `python main.py --diagnostics` to see
paths and dependency versions. `--no-splash` skips the startup image.

## Validate and build

```sh
python -m unittest discover -s tests -v
python -m pip install -r requirements-build.txt
python scripts/build_release.py
python scripts/smoke_release.py dist/wavrun/wavrun
```

On Windows use `dist/wavrun/wavrun.exe`. Distribute the entire `dist/wavrun` directory.
The bundle includes Python, decoder/output native libraries, assets, dependency
notices, and this project's source. Build separately on each target OS/architecture.
See [distribution notes](docs/distribution.md) and [audio engine API](docs/playback-prototype.md).
