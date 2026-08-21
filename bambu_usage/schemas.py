"""Pydantic models for the plugin's HTTP surface.

Kept separate from models.py so the wire format can change without touching
the database layout, and so router.py never hands raw rows to the page.

May import: pydantic. Must not import tracker, router, service or models.
Enforced by tools/check_architecture.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field


def _as_utc(value: datetime | None) -> datetime | None:
    """Stamp a naive timestamp as UTC, and leave an aware one alone.

    The columns are DateTime(timezone=True), but SQLite hands back what it was
    given without an offset it was never asked to store. Serialised like that,
    a browser reads the value as local time and shows every print an hour or two
    early. Saying UTC is not a guess: everything this plugin writes comes from
    datetime.now(timezone.utc).
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


# Every timestamp that leaves this plugin carries its offset. One definition,
# so the next field somebody adds inherits the decision rather than the bug.
UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


class PluginSettings(BaseModel):
    """Behaviour for one printer, or the global defaults.

    ``printer_id`` 0 addresses the global row. See docs/04_Data_Model.md.
    """

    printer_id: int = Field(default=0, ge=0)
    tracking_enabled: bool = True
    auto_spend: bool = True
    # On by default, because what a stopped print used is booked in proportion
    # to how far it got, not as the full estimate. Booking nothing would leave
    # the spool wrong after every abort.
    spend_on_cancel: bool = True
    history_retention_days: int = Field(default=365, ge=0)


class PrinterStatus(BaseModel):
    """Live state of one listener, shown at the top of the plugin page."""

    printer_id: int
    printer_name: str
    connected: bool
    tracking_enabled: bool
    current_print_id: int | None = None
    current_file_name: str | None = None
    progress_percent: int | None = None
    layer_num: int | None = None
    total_layer_num: int | None = None
    remaining_minutes: int | None = None
    last_error: str | None = None
    # When the listener last wrote this row. A row that stops being refreshed is
    # how a dead tracker becomes visible from the page.
    updated_at: UtcDatetime | None = None


class FilamentUsage(BaseModel):
    """One slicer filament of one print.

    ``spool_id`` is None when the slot could not be resolved. The page
    highlights those rows and offers the dropdown to assign a spool.
    """

    id: int
    filament_id: int
    slot_index: str | None = None
    spool_id: int | None = None
    spool_label: str | None = None
    material: str | None = None
    color_hex: str | None = None
    estimated_grams: float | None = None
    spent_grams: float | None = None
    spent_at: UtcDatetime | None = None
    manual_override: bool = False
    # Which part of the print this row covers, when a spool was swapped during
    # it. Both None means the whole print, which is the normal case.
    # How much filament the slicer wanted, as a length. Only ever shown, never
    # booked: the plugin keeps its books in grams.
    estimated_length_m: float | None = None
    # What the print had laid down of this filament by the layer it is on now.
    # Only filled while it runs, and never booked from here.
    used_so_far: float | None = None
    from_fraction: float | None = None
    to_fraction: float | None = None


class PrintRecord(BaseModel):
    """One print job with its per-filament breakdown."""

    id: int
    printer_id: int
    printer_name: str | None = None
    file_name: str
    print_type: str
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    status: str
    spent: bool
    # How far a print that did not finish got, 0.0 to 1.0. None when unknown.
    completed_fraction: float | None = None
    # How many layers the print has, taken from the curves read out of its
    # gcode. None when there are none.
    layer_count: int | None = None
    # What the slicer said before the print started. None where its 3MF could
    # not be read.
    estimated_seconds: int | None = None
    object_count: int | None = None
    nozzle_diameter: float | None = None
    # What the print cost, in FilaMan's currency. None when no spool involved
    # carries the numbers to work it out.
    cost: float | None = None
    has_thumbnail: bool = False
    error: str | None = None
    filaments: list[FilamentUsage] = Field(default_factory=list)


class AssignSpoolRequest(BaseModel):
    """Assign a spool to a filament row after the fact.

    Used for local prints, where no ams_mapping is available, and whenever the
    automatic resolution came up empty.
    """

    spool_id: int | None = None
    spend_now: bool = False


class CorrectUsageRequest(BaseModel):
    """Override the booked amount for one filament row."""

    grams: float = Field(ge=0)


class HealthResponse(BaseModel):
    """Answer of the health endpoint, also used to prove the router mounted."""

    plugin: str = "bambu_usage"
    version: str
    tracking_active: bool = False
    printers_watched: int = 0
    # Whether the worker answering this request has run its one-time setup. The
    # plugin has no startup hook, so this is the only outside view of it.
    tables_ready: bool = False
    # FilaMan's currency code, so the page can label a cost without a second
    # endpoint and without administrator rights.
    currency: str | None = None
