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

The arithmetic itself is in rules.py, which knows nothing about a database and is
where the tests for it live, and reading the history back is views.py. What is
left here is the booking path: the order of operations around a service that
commits by itself.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from . import rules

if TYPE_CHECKING:  # imported for annotations only, keeps the module import-light
    from collections.abc import Iterable

    from sqlalchemy.ext.asyncio import AsyncSession

    from .schemas import PluginSettings
    from .threemf import PrintMetadata

logger = logging.getLogger(__name__)

# The error column of a print is this wide, so an explanation is cut to fit
# rather than failing the very write that was meant to record a failure.
MAX_ERROR_LENGTH = 500

# Note written to the spool log when an amount was corrected by hand. The file
# name of the print is the note on a normal booking, see spend_print.
_CORRECTION_NOTE = "correction"


class UsageError(RuntimeError):
    """A booking was asked for that cannot be carried out."""


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
        layer_shares=metadata.layer_shares,
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

    slots = rules.resolve_slot_indexes(metadata.filaments, ams_mapping)
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
    stopped_at_layer: int | None = None,
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
        db,
        print_id,
        status,
        finished_at=moment,
        completed_fraction=completed_fraction,
        stopped_at_layer=stopped_at_layer,
    )
    await db.commit()

    if not rules.should_spend(
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

    was_stopped = record.status in STOPPED_STATUSES
    linear = rules.booking_factor(
        was_stopped=was_stopped,
        completed_fraction=record.completed_fraction,
    )
    curves = store.decode_layer_shares(record.layer_shares)

    def share_for(row: Any) -> float:
        """What share of this filament the print had laid down when it stopped.

        The curve out of the plate gcode where there is one, the linear share
        of the layers otherwise. They differ most exactly where it matters: a
        filament used only near the end sits at zero for most of the print.
        """
        if not was_stopped:
            return 1.0

        exact = rules.share_at_layer(curves.get(int(row.filament_id)), record.stopped_at_layer)
        return linear if exact is None else exact

    rows = await store.list_filaments(db, print_id)
    pending = [row for row in rows if row.spool_id is not None and row.spent_at is None]
    totals = rules.sum_grams_per_spool(
        (row.spool_id, rules.share_of(row, share_for(row))) for row in pending
    )

    moment = record.finished_at or datetime.now(timezone.utc)
    booked: dict[int, float] = {}
    failures: list[str] = []

    for spool_id, grams in totals.items():
        spool = await filaman.load_spool(db, spool_id)
        if spool is None:
            logger.warning(
                "print %s: spool %s no longer exists, %.2f g not booked", print_id, spool_id, grams
            )
            failures.append(f"spool {spool_id} no longer exists")
            continue

        try:
            # A row without an amount is marked at zero rather than left open,
            # otherwise its print would never count as fully booked.
            for row in (r for r in pending if r.spool_id == spool_id):
                await store.mark_filament_spent(
                    db, row.id, rules.share_of(row, share_for(row)) or 0.0, moment
                )
            await filaman.record_consumption(db, spool, grams, moment, record.file_name)
        except (SQLAlchemyError, filaman.FilaManUnavailableError) as exc:
            await db.rollback()
            logger.exception("print %s: booking %.2f g on spool %s failed", print_id, grams, spool_id)
            failures.append(f"spool {spool_id}: {exc}")
            continue

        booked[spool_id] = grams

    # Written whether or not anything failed, so that a retry which works clears
    # the warning instead of leaving a stale one behind.
    await store.set_print_error(db, print_id, _failure_note(failures))
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

    share = rules.split_share(row.from_fraction, row.to_fraction, at_fraction)
    if share is None:
        return False

    kept_grams, rest_grams = rules.split_amounts(row.estimated_grams, share)
    kept_length, rest_length = rules.split_amounts(row.estimated_length_m, share)

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
                estimated_grams=rest_grams,
                estimated_length_m=rest_length,
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

    A row that was already booked is **moved** rather than relabelled: see
    _move_booking. Assigning the spool it already has does nothing at all.
    """
    from . import store

    row = await store.get_filament(db, filament_row_id)
    if row is None:
        raise UsageError(f"filament row {filament_row_id} does not exist")

    if row.spool_id == spool_id:
        return

    if row.spent_at is not None:
        await _move_booking(db, row, spool_id)
        return

    await store.set_filament_spool(db, filament_row_id, spool_id)
    await db.commit()

    if spend_now and spool_id is not None:
        await spend_print(db, int(row.print_id))
        return

    await _refresh_spent_flag(db, print_id=int(row.print_id))


async def _move_booking(db: AsyncSession, row: Any, new_spool_id: int | None) -> None:
    """Move a row that was already booked onto another spool.

    Changing the spool alone would only change a label while the consumption
    stays on the spool that never printed it. What was taken is taken again
    where it belongs and given back where it does not, which leaves both
    spools right and both movements readable in FilaMan's spool log.

    The new spool is charged **before** the old one is credited. Should the
    second half fail, the material is counted twice, which is visible and
    correctable; the other order would make filament appear out of nowhere
    and let somebody run out mid print.
    """
    from . import filaman, store

    if new_spool_id is None:
        raise UsageError("a booked row cannot be moved to no spool at all")

    new_spool = await filaman.load_spool(db, new_spool_id)
    if new_spool is None:
        raise UsageError(f"spool {new_spool_id} does not exist any more")

    old_spool = await filaman.load_spool(db, int(row.spool_id))
    grams = float(row.spent_grams or 0.0)
    moment = datetime.now(timezone.utc)

    await store.set_filament_spool(db, int(row.id), new_spool_id)

    if grams <= 0:
        await db.commit()
        return

    await filaman.record_consumption(
        db, new_spool, grams, moment, f"moved from spool {row.spool_id}"
    )

    if old_spool is None:
        logger.warning(
            "spool %s no longer exists, %.2f g could not be given back", row.spool_id, grams
        )
        return

    await filaman.record_adjustment(
        db, old_spool, grams, moment, f"moved to spool {new_spool_id}"
    )


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




def _failure_note(failures: list[str]) -> str | None:
    """One line for the history about what did not book, or None if all did.

    A booking is skipped so the other spools still go through, and without this
    the reason would live in the container log and nowhere a user can see it.
    """
    if not failures:
        return None

    return "; ".join(failures)[:MAX_ERROR_LENGTH]


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
