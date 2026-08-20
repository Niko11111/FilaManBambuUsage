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

**Transactions belong to this module.** FilaMan's SpoolService commits the
session inside every call that books, so pretending the caller owns the
transaction would be a lie. The commits here are placed deliberately and the
reasons are in the docstrings; a router must not wrap these calls in a
transaction of its own.

**store, filaman, models and schemas are imported inside the functions that use
them.** All four pull in SQLAlchemy or Pydantic, and keeping them out of the
module header is what lets the decisions in this module be tested without a
database, without FilaMan and without a printer, which is the point of the
separation in the first place.

May import: models, store, schemas, settings, threemf, filaman. Must not import
tracker, router, app, bambulabs_api or fastapi, which is what keeps the business
logic independent of where events come from. Enforced by
tools/check_architecture.py.

On the size of this file, past the 400 lines CLAUDE.md asks a reason for: what
lives here is one topic, the booking path, and its hard part is the order of
operations around a service that commits by itself. Splitting that reasoning
across two files would hide exactly the thing a reviewer has to check.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # imported for annotations only, keeps the module import-light
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

    from .schemas import PluginSettings, PrinterStatus, PrintRecord
    from .threemf import FilamentInfo, PrintMetadata

logger = logging.getLogger(__name__)

# Bambu reserves these for the external spool holder. FilaMan stores the same
# pair as the slot_index "255-254".
EXTERNAL_SPOOL_AMS_ID = 255
EXTERNAL_SPOOL_TRAY_ID = 254
EXTERNAL_SLOT_INDEX = f"{EXTERNAL_SPOOL_AMS_ID}-{EXTERNAL_SPOOL_TRAY_ID}"

# Trays are numbered globally across AMS units, four trays per unit.
TRAYS_PER_AMS = 4


class UsageError(RuntimeError):
    """A booking was asked for that cannot be carried out."""


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


def resolve_slot_indexes(
    filaments: list[FilamentInfo],
    ams_mapping: list[Any],
) -> dict[int, str | None]:
    """Map every slicer filament to the slot it printed from.

    ``ams_mapping`` is indexed from zero while the slicer numbers its filaments
    from one. Containing that off-by-one is the whole job of this function; get
    it wrong and every spool of a multi colour print is charged to its neighbour.

    A filament the mapping does not cover, or covers with something that is not
    a tray number, resolves to None. The print then lands with an open
    assignment instead of a guessed one.
    """
    resolved: dict[int, str | None] = {}

    for filament in filaments:
        index = filament.filament_id - 1
        if index < 0 or index >= len(ams_mapping):
            resolved[filament.filament_id] = None
            continue

        try:
            tray = int(ams_mapping[index])
        except (TypeError, ValueError):
            resolved[filament.filament_id] = None
            continue

        resolved[filament.filament_id] = tray_to_slot_index(tray)

    return resolved


def sum_grams_per_spool(rows: Iterable[tuple[int | None, float | None]]) -> dict[int, float]:
    """Total the grams per spool, so one spool produces one booking.

    A print can address the same tray several times, for instance a multi colour
    model reusing one colour. Booking each row on its own would hang several
    events off the same spool and blur which print cost what, so the amounts are
    summed first. See docs/04_Data_Model.md section 6.

    Rows without a spool or without a usable amount are dropped: there is
    nothing to book, and a zero gram event is noise in the spool log.
    """
    totals: dict[int, float] = {}

    for spool_id, grams in rows:
        if spool_id is None or grams is None or grams <= 0:
            continue
        totals[spool_id] = totals.get(spool_id, 0.0) + float(grams)

    return totals


def should_spend(*, finished_normally: bool, auto_spend: bool, spend_on_cancel: bool) -> bool:
    """Whether a print that just ended is booked automatically.

    Booking happens at the end and not at the start, so an aborted print does not
    cost the full estimate. See docs/01_Design.md section 6.3.
    """
    if not auto_spend:
        return False
    if finished_normally:
        return True
    return spend_on_cancel


def split_share(
    from_fraction: float | None,
    to_fraction: float | None,
    at: float,
) -> float | None:
    """What share of a filament row belongs to the part before *at*.

    A row covers a span of the print, by default the whole of it, and both ends
    being None means exactly that. Splitting a span (a, b) at *at* leaves the
    first part with ``(at - a) / (b - a)``.

    Returns None when *at* does not fall strictly inside the span. Splitting at
    an edge would produce an empty row, and a span that does not contain the
    moment belongs to a part of the print that is already over.
    """
    start = 0.0 if from_fraction is None else float(from_fraction)
    end = 1.0 if to_fraction is None else float(to_fraction)

    if not start < at < end:
        return None

    return (at - start) / (end - start)


def booking_factor(*, was_stopped: bool, completed_fraction: float | None) -> float:
    """What share of the estimate a print costs.

    A print that ran to the end costs all of it. One that was stopped costs the
    share it got through, and **nothing at all** when that share is unknown: an
    invented number on a spool is worse than an open row somebody can correct,
    and it is exactly the mistake of booking the full estimate for an abort.

    A print this plugin only saw part of is not "stopped" in this sense. It is
    never booked automatically, and when a human books it anyway that is a
    decision, so it costs the full estimate.
    """
    if not was_stopped:
        return 1.0
    if completed_fraction is None:
        return 0.0

    return min(max(float(completed_fraction), 0.0), 1.0)


async def start_print(
    db: AsyncSession,
    *,
    printer_id: int,
    file_name: str,
    print_type: str,
    metadata: PrintMetadata,
    ams_mapping: list[Any],
    subtask_id: str | None = None,
    started_at: datetime | None = None,
    status: str | None = None,
) -> int:
    """Record a beginning print and return its id.

    Idempotent on (printer_id, subtask_id), falling back to
    (printer_id, file_name, started_at) when the printer reports no subtask id,
    so a repeated MQTT message does not create a second print.

    *status* overrides the normal "running", for the two cases where a print is
    recorded although it can never be booked: attaching in the middle of one,
    and a 3MF that could not be fetched. Both belong in the history all the
    same, because a print that vanishes without trace is worse than one without
    numbers.

    Commits, because from here on the print has to survive a restart: the
    booking happens at the end, and the mapping resolved now is what it will be
    booked against.
    """
    from . import store
    from .models import STATUS_RUNNING

    moment = started_at or datetime.now(timezone.utc)

    if subtask_id:
        existing = await store.find_print_by_subtask(db, printer_id, subtask_id)
    else:
        existing = await store.find_print_by_start(db, printer_id, file_name, moment)
    if existing is not None:
        return int(existing.id)

    print_id = await store.create_print(
        db,
        printer_id=printer_id,
        file_name=file_name,
        print_type=print_type,
        status=status or STATUS_RUNNING,
        started_at=moment,
        subtask_id=subtask_id,
        plate_id=metadata.plate_id,
        thumbnail=metadata.thumbnail,
        thumbnail_mime=metadata.thumbnail_mime,
    )

    await store.add_filament_rows(
        db,
        print_id,
        await _build_filament_rows(db, printer_id, metadata, ams_mapping),
    )
    await db.commit()

    return print_id


async def _build_filament_rows(
    db: AsyncSession,
    printer_id: int,
    metadata: PrintMetadata,
    ams_mapping: list[Any],
) -> list[Any]:
    """Turn the 3MF filaments into table rows, resolving the spool for each."""
    from . import filaman, store

    slots = resolve_slot_indexes(metadata.filaments, ams_mapping)
    rows = []

    for filament in metadata.filaments:
        slot_index = slots.get(filament.filament_id)
        spool_id = None
        if slot_index is not None:
            spool_id = await filaman.resolve_spool_for_slot(db, printer_id, slot_index)

        rows.append(
            store.FilamentRow(
                filament_id=filament.filament_id,
                slot_index=slot_index,
                spool_id=spool_id,
                material=filament.material,
                color_hex=filament.color_hex,
                tray_info_idx=filament.tray_info_idx,
                estimated_grams=filament.used_g,
                estimated_length_m=filament.used_m,
            )
        )

    return rows


async def finish_print(
    db: AsyncSession,
    print_id: int,
    status: str,
    settings: PluginSettings,
    finished_at: datetime | None = None,
    completed_fraction: float | None = None,
) -> None:
    """Close a print and book it if the settings allow.

    The status is committed before anything is booked. A booking that fails must
    not take the record of how the print ended down with it.

    A print that cannot be booked keeps the status that says so and is never
    booked automatically. That covers one the plugin joined in the middle, where
    the mapping was never seen, and one whose 3MF could not be fetched, where
    there are no amounts at all. Relabelling either as a normal finish would
    hide the very thing the history is meant to show. See docs/01_Design.md
    section 7.
    """
    from . import store
    from .models import STATUS_FINISHED, UNBOOKABLE_STATUSES

    record = await store.get_print(db, print_id)
    if record is None:
        raise UsageError(f"print {print_id} does not exist")

    moment = finished_at or datetime.now(timezone.utc)

    if record.status in UNBOOKABLE_STATUSES:
        await store.set_print_status(db, print_id, record.status, finished_at=moment)
        await db.commit()
        return

    await store.set_print_status(
        db, print_id, status, finished_at=moment, completed_fraction=completed_fraction
    )
    await db.commit()

    if not should_spend(
        finished_normally=status == STATUS_FINISHED,
        auto_spend=settings.auto_spend,
        spend_on_cancel=settings.spend_on_cancel,
    ):
        return

    await spend_print(db, print_id)


async def spend_print(db: AsyncSession, print_id: int) -> dict[int, float]:
    """Book everything of one print that has a spool and is not booked yet.

    Returns the grams booked per spool.

    The order inside the loop is deliberate. ``record_consumption`` commits the
    session, so the rows are marked **before** the booking and both land in the
    same commit. If a booking fails, the rollback drops only that spool's marks;
    whatever was already committed stays booked and is not booked again, because
    a row with a ``spent_at`` is never picked up twice.

    One spool that has since been deleted, or one booking that fails, must not
    stop the others. Both are logged and skipped.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from . import filaman, store
    from .models import STOPPED_STATUSES

    record = await store.get_print(db, print_id)
    if record is None:
        raise UsageError(f"print {print_id} does not exist")

    factor = booking_factor(
        was_stopped=record.status in STOPPED_STATUSES,
        completed_fraction=record.completed_fraction,
    )

    rows = await store.list_filaments(db, print_id)
    pending = [row for row in rows if row.spool_id is not None and row.spent_at is None]
    totals = sum_grams_per_spool((row.spool_id, _share_of(row, factor)) for row in pending)

    moment = record.finished_at or datetime.now(timezone.utc)
    booked: dict[int, float] = {}

    for spool_id, grams in totals.items():
        spool = await filaman.load_spool(db, spool_id)
        if spool is None:
            logger.warning(
                "print %s: spool %s no longer exists, %.2f g not booked", print_id, spool_id, grams
            )
            continue

        try:
            # A row without an amount is marked at zero rather than left open,
            # otherwise its print would never count as fully booked.
            for row in (r for r in pending if r.spool_id == spool_id):
                await store.mark_filament_spent(db, row.id, _share_of(row, factor) or 0.0, moment)
            await filaman.record_consumption(db, spool, grams, moment, record.file_name)
        except (SQLAlchemyError, filaman.FilaManUnavailableError):
            await db.rollback()
            logger.exception("print %s: booking %.2f g on spool %s failed", print_id, grams, spool_id)
            continue

        booked[spool_id] = grams

    await _refresh_spent_flag(db, print_id)

    return booked


async def split_filament_row(
    db: AsyncSession,
    filament_row_id: int,
    at_fraction: float,
    new_spool_id: int,
) -> bool:
    """Divide one filament row where the spool it draws from changed.

    A spool that runs empty gets replaced, and from that moment the print draws
    from a different one. Charging either spool for the whole print would be
    wrong in one direction or the other, so the row becomes two rows: the old
    spool keeps the share up to *at_fraction*, the new one gets the rest.

    Two rows rather than a table of segments, because everything downstream
    already works per row: the booking, the summing per spool, the correction by
    hand and the display. A print that used two spools for one slicer filament
    genuinely is two entries.

    Returns whether anything was split. A row that was already booked, or whose
    span does not contain the moment, is left alone.
    """
    from . import store

    row = await store.get_filament(db, filament_row_id)
    if row is None:
        raise UsageError(f"filament row {filament_row_id} does not exist")

    if row.spent_at is not None:
        return False

    share = split_share(row.from_fraction, row.to_fraction, at_fraction)
    if share is None:
        return False

    kept_grams = _scaled(row.estimated_grams, share)
    kept_length = _scaled(row.estimated_length_m, share)

    await store.narrow_filament_row(
        db,
        filament_row_id,
        estimated_grams=kept_grams,
        estimated_length_m=kept_length,
        to_fraction=at_fraction,
    )
    await store.add_filament_rows(
        db,
        int(row.print_id),
        [
            store.FilamentRow(
                filament_id=int(row.filament_id),
                slot_index=row.slot_index,
                spool_id=new_spool_id,
                material=row.material,
                color_hex=row.color_hex,
                tray_info_idx=row.tray_info_idx,
                estimated_grams=_remainder(row.estimated_grams, kept_grams),
                estimated_length_m=_remainder(row.estimated_length_m, kept_length),
                from_fraction=at_fraction,
                to_fraction=row.to_fraction,
            )
        ],
    )
    await db.commit()

    return True


async def assign_spool(
    db: AsyncSession,
    filament_row_id: int,
    spool_id: int | None,
    spend_now: bool = False,
) -> None:
    """Attach a spool to a filament row after the fact.

    The path for local prints in stage 1, and for anything the automatic
    resolution left open. With *spend_now* the print is booked afterwards, which
    picks up this row along with any other that is still open.
    """
    from . import store

    row = await store.get_filament(db, filament_row_id)
    if row is None:
        raise UsageError(f"filament row {filament_row_id} does not exist")

    await store.set_filament_spool(db, filament_row_id, spool_id)
    await db.commit()

    if spend_now and spool_id is not None:
        await spend_print(db, int(row.print_id))
        return

    await _refresh_spent_flag(db, print_id=int(row.print_id))


async def correct_usage(db: AsyncSession, filament_row_id: int, grams: float) -> None:
    """Override the booked amount for one row.

    Only the difference is moved on the spool, and the direction decides how:
    more consumed is a consumption, less consumed has to be an adjustment,
    because ``record_consumption`` turns every positive value into a deduction
    and can never give material back.

    ``estimated_grams`` stays untouched, so the slicer's number remains
    comparable against the scale.
    """
    from . import filaman, store

    row = await store.get_filament(db, filament_row_id)
    if row is None:
        raise UsageError(f"filament row {filament_row_id} does not exist")
    if row.spool_id is None:
        raise UsageError(f"filament row {filament_row_id} has no spool to correct")

    spool = await filaman.load_spool(db, int(row.spool_id))
    if spool is None:
        raise UsageError(f"spool {row.spool_id} does not exist any more")

    moment = datetime.now(timezone.utc)
    difference = grams - (row.spent_grams or 0.0)

    await store.override_filament_amount(db, filament_row_id, grams, moment)

    if difference > 0:
        await filaman.record_consumption(db, spool, difference, moment, _CORRECTION_NOTE)
    elif difference < 0:
        await filaman.record_adjustment(db, spool, -difference, moment, _CORRECTION_NOTE)
    else:
        await db.commit()

    await _refresh_spent_flag(db, print_id=int(row.print_id))


async def get_history(db: AsyncSession, limit: int = 50, offset: int = 0) -> list[PrintRecord]:
    """Return prints, newest first, with their filament breakdown.

    ``printer_name`` and ``spool_label`` are left empty on purpose. The page
    already holds FilaMan's printer and spool lists for its dropdowns, so
    resolving the names there costs nothing, while doing it here would add two
    more couplings into FilaMan for display text alone.
    """
    from . import store
    from .schemas import FilamentUsage, PrintRecord

    prints = await store.list_prints(db, limit=limit, offset=offset)
    rows = await store.list_filaments_for(db, [int(entry.id) for entry in prints])

    by_print: dict[int, list[Any]] = {}
    for row in rows:
        by_print.setdefault(int(row.print_id), []).append(row)

    records = []
    for entry in prints:
        filaments = [
            FilamentUsage(
                id=int(row.id),
                filament_id=int(row.filament_id),
                slot_index=row.slot_index,
                spool_id=row.spool_id,
                material=row.material,
                color_hex=row.color_hex,
                estimated_grams=row.estimated_grams,
                spent_grams=row.spent_grams,
                spent_at=row.spent_at,
                manual_override=bool(row.manual_override),
                from_fraction=row.from_fraction,
                to_fraction=row.to_fraction,
            )
            for row in by_print.get(int(entry.id), [])
        ]
        records.append(
            PrintRecord(
                id=int(entry.id),
                printer_id=int(entry.printer_id),
                file_name=entry.file_name,
                print_type=entry.print_type,
                started_at=entry.started_at,
                finished_at=entry.finished_at,
                status=entry.status,
                spent=bool(entry.spent),
                completed_fraction=entry.completed_fraction,
                has_thumbnail=entry.thumbnail_mime is not None,
                error=entry.error,
                filaments=filaments,
            )
        )

    return records


async def get_printer_status(db: AsyncSession) -> list[PrinterStatus]:
    """Return what the listeners last wrote about themselves.

    Read from the database rather than from the listeners, because they live in
    one worker process while this is answered by any of the four. The database
    is the only place all of them can see.
    """
    from . import store
    from .schemas import PrinterStatus

    return [
        PrinterStatus(
            printer_id=int(row.printer_id),
            printer_name=row.printer_name or "",
            connected=bool(row.connected),
            tracking_enabled=bool(row.tracking_enabled),
            current_print_id=row.current_print_id,
            current_file_name=row.current_file_name,
            progress_percent=row.progress_percent,
            last_error=row.last_error,
            updated_at=row.updated_at,
        )
        for row in await store.list_printer_status(db)
    ]


async def get_thumbnail(db: AsyncSession, print_id: int) -> tuple[bytes, str] | None:
    """Return the stored plate preview and its mime type.

    Previews cannot ship inside the plugin ZIP, which rejects image files, so
    they are served from the database instead. See docs/01_Design.md 8.1.
    """
    from . import store

    return await store.read_thumbnail(db, print_id)


# Note written to the spool log when an amount was corrected by hand. The file
# name of the print is the note on a normal booking, see spend_print.
_CORRECTION_NOTE = "correction"


def _scaled(value: float | None, share: float) -> float | None:
    """Multiply an estimate by a share, leaving an unknown estimate unknown."""
    return None if value is None else value * share


def _remainder(total: float | None, kept: float | None) -> float | None:
    """What is left of an estimate after the kept part was taken out."""
    if total is None or kept is None:
        return None
    return total - kept


def _share_of(row: Any, factor: float) -> float | None:
    """What a row costs once the share of the print is taken into account."""
    amount = _amount_of(row)
    return None if amount is None else amount * factor


def _amount_of(row: Any) -> float | None:
    """What a filament row costs: the corrected amount if there is one.

    The slicer estimate is the fallback, never the other way round, so a value
    somebody entered by hand is not overruled by the machine.
    """
    if row.spent_grams is not None:
        return float(row.spent_grams)
    return None if row.estimated_grams is None else float(row.estimated_grams)


async def _refresh_spent_flag(db: AsyncSession, print_id: int) -> None:
    """Keep prints.spent in step with the rows, and commit.

    A print counts as booked when nothing bookable is left **and** something was
    actually booked. The second half matters: a print whose slots could not be
    resolved has no open bookings either, and calling that one deducted would
    hide exactly the case the history is supposed to surface.

    Deriving the flag beats setting it once, because a row assigned by hand
    afterwards reopens the print by itself.
    """
    from . import store

    open_bookings, settled = await store.booking_state(db, print_id)
    await store.set_print_spent(db, print_id, open_bookings == 0 and settled > 0)
    await db.commit()
