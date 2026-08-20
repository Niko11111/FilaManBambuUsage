"""Pydantic models for the plugin's HTTP surface.

Kept separate from models.py so the wire format can change without touching
the database layout, and so router.py never hands raw rows to the page.

May import: pydantic. Must not import tracker, router, service or models.
Enforced by tools/check_architecture.py.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


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
    last_error: str | None = None
    # When the listener last wrote this row. A row that stops being refreshed is
    # how a dead tracker becomes visible from the page.
    updated_at: datetime | None = None


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
    spent_at: datetime | None = None
    manual_override: bool = False
    # Which part of the print this row covers, when a spool was swapped during
    # it. Both None means the whole print, which is the normal case.
    from_fraction: float | None = None
    to_fraction: float | None = None


class PrintRecord(BaseModel):
    """One print job with its per-filament breakdown."""

    id: int
    printer_id: int
    printer_name: str | None = None
    file_name: str
    print_type: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    spent: bool
    # How far a print that did not finish got, 0.0 to 1.0. None when unknown.
    completed_fraction: float | None = None
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
