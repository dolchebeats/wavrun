# Building and distributing Wavrun

Use `requirements-build.txt` and `scripts/build_release.py` on the target OS. This
creates a console/terminal **directory bundle**, not a graphical application or
installer. Users launch `wavrun.exe` on Windows or `./wavrun` from a terminal on
macOS/Linux. Keep the entire directory together. No VLC installation is used.

The entry point calls `multiprocessing.freeze_support()` before parsing arguments
or importing the UI. Decoder subprocesses therefore reuse the frozen executable
without recursively launching the splash/TUI. The splash and stylesheet are bundled
as read-only resources, separately from writable per-user settings and logs.

`scripts/smoke_release.py` runs the built executable from an unrelated directory
with isolated settings and without developer Python/VLC search-path overrides. It
checks assets, headless TUI startup/cleanup, and real decoding in a spawned process.
It produces no audible output. It is not a substitute for listening tests on a
clean machine with real output devices.

The build collects installed dependency license/notice files into `licenses/`,
records package versions and the native FFmpeg build configuration/license, and
includes this project's GPL-licensed source under `source/`. The native wheel may
include additional codec libraries with their own terms. Before public release,
use the inventory and `licenses/ffmpeg-build.json` to prepare the matching native
source/build materials and any additional notices required for the exact binaries
being shipped. Those native corresponding sources are not fetched by this script.
Upstream source/build locations include https://github.com/PyAV-Org/PyAV and
https://github.com/PyAV-Org/pyav-ffmpeg; FFmpeg is at https://ffmpeg.org/.

Builds are unsigned developer artifacts. Signing/notarization, installers, and
publication are separate release actions. Preserve symbolic links when archiving
macOS/Linux directory bundles; use tar archives on those platforms. Build Linux
on the oldest distribution/glibc baseline you intend to support. CPU architectures
need their own compatible native wheels and builds.

CI defines Windows, macOS, and Linux test/build/smoke jobs. Local validation can
establish only the OS actually used. Test default and alternate output devices,
all supported formats, seeking, network-share interruption, device removal, and
quit-during-buffering on each target before declaring a public release supported.
