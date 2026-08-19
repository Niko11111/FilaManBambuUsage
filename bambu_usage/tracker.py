"""Printer listeners.

One listener per active printer with driver_key "bambulab". This is the only
module that knows where print events come from. Should the Bambu Lab driver
ever publish a print_complete event on FilaMan's event bus, this module is the
single one that gets replaced. See docs/01_Design.md, section 4.

Lifecycle note: an integration plugin gets no start/stop callback from
FilaMan, only drivers do, and only per printer. The listeners therefore start
themselves from an asyncio task when the router is mounted, and have to guard
against several uvicorn workers each starting their own set. FilaMan solves the
same problem in main.py with a lock file under tempfile.gettempdir().

May import: service, settings, schemas, threemf, bambulabs_api. Must not
import router. Enforced by tools/check_architecture.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # imported for annotations only
    from .schemas import PrinterStatus

# How often the printer list is compared against the running listeners, so a
# printer added or removed in FilaMan is picked up without a restart.
PRINTER_REFRESH_INTERVAL_SECONDS = 60

# Reconnect backoff bounds for a single printer. One unreachable printer must
# never stall the others.
RECONNECT_MIN_SECONDS = 5
RECONNECT_MAX_SECONDS = 300

# gcode_state values that matter. PREPARE -> RUNNING marks a local print start.
STATE_IDLE = "IDLE"
STATE_PREPARE = "PREPARE"
STATE_RUNNING = "RUNNING"
STATE_PAUSE = "PAUSE"
STATE_FINISH = "FINISH"
STATE_FAILED = "FAILED"

# A network or cloud print announces itself with this command and carries a
# ready-made ams_mapping. Local prints do not, which is why they are stage 3.
COMMAND_PROJECT_FILE = "project_file"


@dataclass
class PrinterListener:
    """State of the connection to one printer.

    The printer sends partial updates, so the reported state has to be merged
    and compared against the previous one to see transitions at all.
    """

    printer_id: int
    printer_name: str
    host: str
    serial: str
    access_code: str
    connected: bool = False
    last_error: str | None = None
    current_print_id: int | None = None
    state: dict = field(default_factory=dict)
    previous_state: dict = field(default_factory=dict)

    async def start(self) -> None:
        """Connect and begin consuming reports."""
        raise NotImplementedError

    async def stop(self) -> None:
        """Disconnect and release the connection."""
        raise NotImplementedError

    async def handle_message(self, payload: dict, received_at: datetime | None = None) -> None:
        """Merge one report into the state and act on any transition."""
        raise NotImplementedError

    def status(self) -> PrinterStatus:
        """Snapshot for the plugin page."""
        raise NotImplementedError


async def start_tracking() -> None:
    """Bring up listeners for every eligible printer.

    Idempotent. Safe to call from every worker; only one set of listeners is
    meant to exist per FilaMan instance.
    """
    raise NotImplementedError


async def stop_tracking() -> None:
    """Shut every listener down."""
    raise NotImplementedError


async def refresh_printers() -> None:
    """Reconcile the running listeners with FilaMan's printer list."""
    raise NotImplementedError


def get_status() -> list[PrinterStatus]:
    """Status of all listeners, for the page and for the health endpoint."""
    raise NotImplementedError
