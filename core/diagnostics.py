"""Non-audible checks usable in source installations and release bundles."""
import asyncio
import json
import sys

from .storage import APP_DIR, CONFIG_DIR, DATA_DIR, LOG_DIR


def report(*, self_test=False):
    import av
    import numpy
    import sounddevice
    import textual
    result = {
        "python": sys.version.split()[0], "av": av.__version__,
        "numpy": numpy.__version__, "sounddevice": sounddevice.__version__,
        "textual": textual.__version__, "config_dir": str(CONFIG_DIR),
        "data_dir": str(DATA_DIR), "log_dir": str(LOG_DIR),
        "splash": (APP_DIR / "splash.txt").is_file(),
        "stylesheet": (APP_DIR / "core" / "wavrun.css").is_file(),
        "vlc_loaded": "vlc" in sys.modules,
    }
    if self_test:
        from .cli_playback import wavrun
        async def check():
            app = wavrun(config={}, playlist=[])
            async with app.run_test() as pilot:
                await pilot.pause()
                app.exit()
            return app._closed_player
        result["ui_closed_cleanly"] = asyncio.run(check())
    print(json.dumps(result, indent=2))
    return 0 if (result["splash"] and result["stylesheet"] and not result["vlc_loaded"]
                 and result.get("ui_closed_cleanly", True)) else 1
