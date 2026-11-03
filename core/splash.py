"""Display the startup artwork before opening the player."""
import logging
import time

from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .storage import APP_DIR


def show_splash():
    try:
        artwork = (APP_DIR / "splash.txt").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        logging.warning("Unable to read splash artwork", exc_info=True)
        return

    console = Console()
    splash = Panel.fit(
        Text(artwork.rstrip()),
        padding=(1, 3),
        style="white on #172b4d",
        border_style="bright_cyan on #172b4d",
    )
    with console.screen():
        console.print(Align.center(splash, vertical="middle",
                                   height=console.size.height), end="")
        time.sleep(2)
