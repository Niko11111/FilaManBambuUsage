"""Resolving slots to spools, and booking the consumption.

This module knows nothing about MQTT. Everything it needs arrives as plain
data, which is what keeps the door open to dropping the own printer connection
later and consuming a print_complete event off FilaMan's event bus instead.
See docs/01_Design.md, section 4.

The resolution chain, in full:

    slice_info.config filament id (1-based)
      -> ams_mapping[id - 1]                 global tray number
      -> ams_id = tray // 4, tray_id = tray % 4
      -> slot_index "<ams_id>-<tray_id>"
      -> PrinterSlot.custom_fields["slot_index"]
      -> PrinterSlotAssignment.spool_id
      -> SpoolService.record_consumption(...)

If the chain breaks anywhere the row is stored with spool_id NULL and offered
for manual assignment. Never guess.

Everything this module reads out of FilaMan goes through filaman.py, never
through FilaMan's own modules directly. That is what keeps the business logic
here callable against fakes.

May import: models, schemas, settings, threemf, filaman. Must not import tracker,
router, app, bambulabs_api or fastapi, which is what keeps the business logic
independent of where events come from. Enforced by
tools/check_architecture.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # imported for annotations only, keeps the module import-light
    from .schemas import PrintRecord
    from .threemf import PrintMetadata

# Bambu reserves these for the external spool holder. FilaMan stores the same
# pair as the slot_index "255-254".
EXTERNAL_SPOOL_AMS_ID = 255
EXTERNAL_SPOOL_TRAY_ID = 254
EXTERNAL_SLOT_INDEX = f"{EXTERNAL_SPOOL_AMS_ID}-{EXTERNAL_SPOOL_TRAY_ID}"

# Trays are numbered globally across AMS units, four trays per unit.
TRAYS_PER_AMS = 4


def tray_to_slot_index(tray: int) -> str:
    """Translate a global Bambu tray number into FilaMan's slot_index.

    A negative tray number means the print does not use an AMS slot for this
    filament, which is the external spool holder.

    >>> tray_to_slot_index(5)
    '1-1'
    >>> tray_to_slot_index(0)
    '0-0'
    >>> tray_to_slot_index(-1)
    '255-254'
    """
    if tray < 0:
        return EXTERNAL_SLOT_INDEX
    return f"{tray // TRAYS_PER_AMS}-{tray % TRAYS_PER_AMS}"


async def start_print(
    printer_id: int,
    file_name: str,
    print_type: str,
    metadata: PrintMetadata,
    ams_mapping: list[int],
    subtask_id: str | None = None,
    started_at: datetime | None = None,
) -> int:
    """Record a beginning print and return its id.

    Idempotent on (printer_id, subtask_id), falling back to
    (printer_id, file_name, started_at) when the printer reports no subtask id,
    so a repeated MQTT message does not create a second print.
    """
    raise NotImplementedError


async def finish_print(
    print_id: int,
    status: str,
    finished_at: datetime | None = None,
) -> None:
    """Close a print and book it if the settings allow.

    Booking happens here rather than at print start, unlike OpenSpoolMan, so an
    aborted print does not cost the full estimate. See docs/01_Design.md 6.3.
    """
    raise NotImplementedError


async def spend_print(print_id: int) -> dict[int, float]:
    """Book the consumption of one print and return grams per spool.

    Amounts are summed per spool before booking, so a print that uses the same
    spool for several slicer filaments produces one event, not several.
    Delegates to SpoolService.record_consumption, which owns the sign, the
    aggregation and the clamp at zero.
    """
    raise NotImplementedError


async def assign_spool(filament_row_id: int, spool_id: int | None, spend_now: bool = False) -> None:
    """Attach a spool to a filament row after the fact.

    The path for local prints in stage 1, and for anything the automatic
    resolution left open.
    """
    raise NotImplementedError


async def correct_usage(filament_row_id: int, grams: float) -> None:
    """Override the booked amount for one row.

    Adjusts the spool by the difference and marks the row as manually
    overridden. The slicer estimate is kept untouched.
    """
    raise NotImplementedError


async def get_history(limit: int = 50, offset: int = 0) -> list[PrintRecord]:
    """Return prints, newest first, with their filament breakdown."""
    raise NotImplementedError


async def get_thumbnail(print_id: int) -> tuple[bytes, str] | None:
    """Return the stored plate preview and its mime type.

    Previews cannot ship inside the plugin ZIP, which rejects image files, so
    they are served from the database instead. See docs/01_Design.md 8.1.
    """
    raise NotImplementedError
