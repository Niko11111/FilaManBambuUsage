"""FilaMan plugin: Bambu Lab print consumption tracking.

Watches Bambu Lab printers for print jobs, reads the per-filament weight
estimate out of the 3MF the printer is running, resolves each slicer filament
to a physical AMS tray and from there to the FilaMan spool sitting in it, and
deducts the consumed grams once the print finishes.

The plugin deliberately does not duplicate anything the Bambu Lab driver
plugin already provides: AMS overview, slot assignment, RFID and auto matching
stay where they are. This plugin only reads that state.

See docs/01_Design.md in the repository for the full design.
"""

import logging

__version__ = "0.2.3"

_logger = logging.getLogger(__name__)


def _start_tracker() -> None:
    """Bring the listeners up, if this is running inside FilaMan.

    Importing this package is the only hook an integration plugin has: FilaMan
    mounts the router at module import time, before an event loop exists, and
    its own lifespan makes router startup handlers inert. See
    docs/02_FilaMan_Plugin_API.md section 4 and supervisor.py.

    The check for FilaMan is what keeps this out of the way everywhere else. The
    test suite imports this package too, and it must not spawn a thread that
    reaches for a database nobody configured.
    """
    try:
        import app.core.database  # noqa: F401
    except ImportError:
        return

    try:
        from . import supervisor

        supervisor.start_in_background()
    except Exception:
        # A plugin that cannot start its listeners still has to serve its page
        # and its history. Failing the import here would take both down, and
        # FilaMan would only log a warning about it.
        _logger.exception("bambu_usage: the tracker could not be started")


_start_tracker()
