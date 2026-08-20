"""Deciding which worker process runs the listeners, and keeping their list current.

An integration plugin gets no lifecycle and no startup hook. FilaMan mounts the
router while the module is imported, before an event loop exists, and hands
FastAPI its own lifespan, which makes router on_startup handlers inert. See
docs/02_FilaMan_Plugin_API.md section 4. The only moment left is the import
itself, and there is no loop there to attach a task to, so the listeners get a
daemon thread with an event loop of their own.

The image starts four Gunicorn workers, so this happens four times over. An
exclusive ``fcntl.flock`` decides which single one actually connects to the
printers. The other three keep checking, and take over when the owner dies,
which is the same mechanism FilaMan uses in main.py for its own one-time work.

Nothing here parses a report or books anything. It starts listeners, stops them,
and reconciles them against FilaMan's printer list.

Every import that needs SQLAlchemy or FilaMan sits inside the function that uses
it, so importing this module costs nothing outside a running FilaMan.

May import: tracker, filaman, settings, store, models. Must not import router,
service or app. Enforced by tools/check_architecture.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # imported for annotations only
    from .tracker import PrinterListener

logger = logging.getLogger(__name__)

# FilaMan runs its migrations while this plugin is already imported, so the
# first look at the printer table waits for them.
STARTUP_DELAY_SECONDS = 15

# How often the printer list is compared against the running listeners, so a
# printer added, removed or reconfigured in FilaMan is picked up without a
# restart.
PRINTER_REFRESH_INTERVAL_SECONDS = 60

# How often a worker without the lock checks whether the owner has gone.
TAKEOVER_INTERVAL_SECONDS = 60

# Only one worker of the four may hold the listeners. The file is deliberately
# never deleted, so the others can keep flock()ing it while they wait.
LOCK_PATH = Path(tempfile.gettempdir()) / "filaman-bambu-usage.lock"

THREAD_NAME = "bambu-usage-tracker"


class _Supervisor:
    """The listeners of this process, and the thread they live in.

    An object rather than a handful of module level dicts, which is the
    OpenSpoolMan failure mode this project set out to avoid. One instance,
    private to this module.
    """

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.listeners: dict[int, PrinterListener] = {}
        self.engine: Any = None
        self.sessions: Any = None
        self.lock_handle: Any = None

    @property
    def owns_listeners(self) -> bool:
        return self.lock_handle is not None


_supervisor = _Supervisor()


def start_in_background() -> None:
    """Bring the listeners up in a thread of their own. Idempotent.

    Called while the package is imported, which is the only hook this plugin
    has. Returns immediately; everything else happens in the thread.
    """
    if _supervisor.thread is not None:
        return

    thread = threading.Thread(target=_run, name=THREAD_NAME, daemon=True)
    _supervisor.thread = thread
    thread.start()
    logger.info("bambu_usage: tracker thread started")


def is_tracking() -> bool:
    """Whether this worker is the one holding the listeners."""
    return _supervisor.owns_listeners


def watched_printers() -> int:
    """How many listeners this worker runs. Zero in three workers out of four."""
    return len(_supervisor.listeners)


def _run() -> None:
    """Thread entry point.

    Catches everything: a thread that dies silently would leave the plugin
    looking installed and doing nothing, which is the failure this plugin exists
    to prevent in the first place.
    """
    try:
        asyncio.run(_supervise())
    except Exception:
        logger.exception("bambu_usage: the tracker thread stopped")


async def _supervise() -> None:
    """Wait for FilaMan, take the lock if it is free, then keep the list current."""
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    _supervisor.loop = asyncio.get_running_loop()

    while not _acquire_lock():
        logger.debug("bambu_usage: another worker owns the listeners")
        await asyncio.sleep(TAKEOVER_INTERVAL_SECONDS)

    logger.info("bambu_usage: this worker owns the listeners")

    await start_tracking()
    while True:
        await asyncio.sleep(PRINTER_REFRESH_INTERVAL_SECONDS)
        try:
            await refresh_printers()
        except Exception:
            # The reconcile loop must survive a database hiccup; the next round
            # tries again in a minute.
            logger.exception("bambu_usage: refreshing the printer list failed")


def _acquire_lock() -> bool:
    """Take the exclusive lock, or report that somebody else holds it.

    The handle is kept for the life of the process. Closing it, or the process
    dying, releases the lock and lets another worker take over.
    """
    try:
        handle = LOCK_PATH.open("w")
    except OSError as exc:
        logger.warning("bambu_usage: cannot open the lock file %s: %s", LOCK_PATH, exc)
        return False

    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False

    _supervisor.lock_handle = handle
    return True


async def start_tracking() -> None:
    """Bring up the database connection of this thread and the listeners."""
    from . import filaman, models

    _supervisor.engine = filaman.create_background_engine()
    _supervisor.sessions = filaman.background_sessions(_supervisor.engine)

    # The listeners may well be the first thing here to touch the database, and
    # they answer to no request that could have created the tables.
    await models.ensure_tables(_supervisor.engine)

    await refresh_printers()


async def stop_tracking() -> None:
    """Shut every listener down and release the database connection."""
    for listener in list(_supervisor.listeners.values()):
        with contextlib.suppress(Exception):
            await listener.stop()

    _supervisor.listeners.clear()

    if _supervisor.engine is not None:
        with contextlib.suppress(Exception):
            await _supervisor.engine.dispose()
        _supervisor.engine = None
        _supervisor.sessions = None


async def refresh_printers() -> None:
    """Reconcile the running listeners with FilaMan's printer list.

    Three things can have changed since the last round: a printer appeared or
    vanished, its credentials were edited, or tracking was switched off for it.
    All three are handled by comparing rather than by remembering.
    """
    from . import filaman, settings, store

    async with _supervisor.sessions() as db:
        printers = await filaman.list_bambu_printers(db)
        wanted = {printer.printer_id: printer for printer in printers}
        enabled = {}
        for printer_id in wanted:
            config = await settings.load_settings(db, printer_id)
            enabled[printer_id] = config.tracking_enabled

        await store.forget_printers(db, list(wanted))
        await db.commit()

    for printer_id in list(_supervisor.listeners):
        printer = wanted.get(printer_id)
        listener = _supervisor.listeners[printer_id]
        if printer is None or not enabled[printer_id] or not listener.describes(printer):
            await _drop_listener(printer_id)

    for printer_id, printer in wanted.items():
        if not enabled[printer_id]:
            await _publish_idle(printer)
            continue
        if printer_id not in _supervisor.listeners:
            await _add_listener(printer)

    for listener in _supervisor.listeners.values():
        with contextlib.suppress(Exception):
            await listener.publish_status()


async def _add_listener(printer: Any) -> None:
    """Start one listener. A printer that refuses must not stop the others."""
    from .tracker import PrinterListener

    listener = PrinterListener(
        printer_id=printer.printer_id,
        printer_name=printer.name,
        host=printer.host,
        serial=printer.serial,
        access_code=printer.access_code,
        sessions=_supervisor.sessions,
        loop=_supervisor.loop,
    )

    try:
        await listener.start()
    except Exception as exc:
        logger.exception("printer %s: listener could not be started", printer.printer_id)
        listener.last_error = str(exc)
        with contextlib.suppress(Exception):
            await listener.publish_status()
        return

    _supervisor.listeners[printer.printer_id] = listener


async def _drop_listener(printer_id: int) -> None:
    """Stop one listener and forget it."""
    listener = _supervisor.listeners.pop(printer_id, None)
    if listener is None:
        return

    with contextlib.suppress(Exception):
        await listener.stop()


async def _publish_idle(printer: Any) -> None:
    """Show a printer with tracking switched off, rather than not at all."""
    from datetime import datetime, timezone

    from . import store

    async with _supervisor.sessions() as db:
        await store.upsert_printer_status(
            db,
            printer_id=printer.printer_id,
            printer_name=printer.name,
            connected=False,
            tracking_enabled=False,
            updated_at=datetime.now(timezone.utc),
        )
        await db.commit()
