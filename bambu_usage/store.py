"""Reading and writing the plugin's own three tables.

models.py declares them, this module is the only place that queries them. The
split exists so service.py can be read as business logic without SQL in the way,
and so the same logic can be exercised against fakes.

Every function takes the session as its first argument and none of them commits.
Where a commit has to happen is decided in service.py, because FilaMan's
SpoolService commits the session itself, see docs/04_Data_Model.md section 3.

Rows come back as SQLAlchemy Row objects, which are read only and carry the
column names. They never travel further than service.py, which turns them into
the models in schemas.py.

May import: models. Must not import tracker, router, service, filaman,
bambulabs_api or fastapi. Enforced by tools/check_architecture.py.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, insert, select, update

from .models import OPEN_STATUSES, filament_table, printer_status_table, prints_table

if TYPE_CHECKING:  # imported for annotations only
    from datetime import datetime
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class FilamentRow:
    """One slicer filament of one print, on its way into the table.

    Built in service.py out of the 3MF and the resolved slot, so no raw dict
    reaches the insert.
    """

    filament_id: int
    slot_index: str | None = None
    spool_id: int | None = None
    material: str | None = None
    color_hex: str | None = None
    tray_info_idx: str | None = None
    estimated_grams: float | None = None
    estimated_length_m: float | None = None
    from_fraction: float | None = None
    to_fraction: float | None = None


async def create_print(
    db: AsyncSession,
    *,
    printer_id: int,
    file_name: str,
    print_type: str,
    status: str,
    started_at: datetime,
    subtask_id: str | None = None,
    plate_id: int | None = None,
    thumbnail: bytes | None = None,
    thumbnail_mime: str | None = None,
    error: str | None = None,
) -> int:
    """Insert one print and return its id.

    Keyword only, because eleven positional arguments in a row is how a printer
    id ends up in the plate column.
    """
    result = await db.execute(
        insert(prints_table).values(
            printer_id=printer_id,
            subtask_id=subtask_id,
            file_name=file_name,
            print_type=print_type,
            plate_id=plate_id,
            started_at=started_at,
            status=status,
            spent=False,
            thumbnail=thumbnail,
            thumbnail_mime=thumbnail_mime,
            error=error,
        )
    )
    return int(result.inserted_primary_key[0])


async def add_filament_rows(db: AsyncSession, print_id: int, rows: list[FilamentRow]) -> None:
    """Insert the filament breakdown of one print."""
    if not rows:
        return

    await db.execute(
        insert(filament_table),
        [{"print_id": print_id, **asdict(row)} for row in rows],
    )


async def get_print(db: AsyncSession, print_id: int) -> Any | None:
    """One print by id."""
    result = await db.execute(select(prints_table).where(prints_table.c.id == print_id))
    return result.first()


async def find_print_by_subtask(
    db: AsyncSession,
    printer_id: int,
    subtask_id: str,
) -> Any | None:
    """The print a printer identifies by *subtask_id*, if it is already known."""
    result = await db.execute(
        select(prints_table).where(
            prints_table.c.printer_id == printer_id,
            prints_table.c.subtask_id == subtask_id,
        )
    )
    return result.first()


async def find_print_by_start(
    db: AsyncSession,
    printer_id: int,
    file_name: str,
    started_at: datetime,
) -> Any | None:
    """The fallback identity for a printer that reports no subtask id."""
    result = await db.execute(
        select(prints_table).where(
            prints_table.c.printer_id == printer_id,
            prints_table.c.file_name == file_name,
            prints_table.c.started_at == started_at,
        )
    )
    return result.first()


async def find_open_print(db: AsyncSession, printer_id: int) -> Any | None:
    """The newest print of one printer that has not ended yet.

    This is how a listener picks a print back up after FilaMan restarted in the
    middle of it. "Open" covers more than running: a print recorded without its
    3MF, or one the plugin joined in the middle, is just as unfinished and would
    otherwise stay open for good.
    """
    result = await db.execute(
        select(prints_table)
        .where(
            prints_table.c.printer_id == printer_id,
            prints_table.c.status.in_(OPEN_STATUSES),
        )
        .order_by(prints_table.c.started_at.desc())
        .limit(1)
    )
    return result.first()


async def list_prints(db: AsyncSession, limit: int = 50, offset: int = 0) -> list[Any]:
    """Prints, newest first. The thumbnail is left out, it is served separately."""
    columns = [column for column in prints_table.c if column.name != "thumbnail"]
    result = await db.execute(
        select(*columns)
        .order_by(prints_table.c.started_at.desc(), prints_table.c.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.all())


async def list_filaments(db: AsyncSession, print_id: int) -> list[Any]:
    """The filament rows of one print, in slicer order."""
    result = await db.execute(
        select(filament_table)
        .where(filament_table.c.print_id == print_id)
        .order_by(filament_table.c.filament_id)
    )
    return list(result.all())


async def list_filaments_for(db: AsyncSession, print_ids: list[int]) -> list[Any]:
    """The filament rows of several prints at once, so a history is two queries."""
    if not print_ids:
        return []

    result = await db.execute(
        select(filament_table)
        .where(filament_table.c.print_id.in_(print_ids))
        .order_by(filament_table.c.print_id, filament_table.c.filament_id)
    )
    return list(result.all())


async def get_filament(db: AsyncSession, filament_row_id: int) -> Any | None:
    """One filament row by id."""
    result = await db.execute(
        select(filament_table).where(filament_table.c.id == filament_row_id)
    )
    return result.first()


async def booking_state(db: AsyncSession, print_id: int) -> tuple[int, int]:
    """How far the booking of one print has come: (still open, already settled).

    Open means a row that has a spool but no ``spent_at``. Settled means a row
    that has been booked. Both are derived rather than remembered in a flag,
    which is what lets a row assigned by hand afterwards reopen a print by
    itself.
    """
    result = await db.execute(
        select(filament_table.c.spool_id, filament_table.c.spent_at).where(
            filament_table.c.print_id == print_id
        )
    )
    rows = result.all()

    open_bookings = sum(1 for row in rows if row.spool_id is not None and row.spent_at is None)
    settled = sum(1 for row in rows if row.spent_at is not None)

    return open_bookings, settled


async def set_print_status(
    db: AsyncSession,
    print_id: int,
    status: str,
    finished_at: datetime | None = None,
    error: str | None = None,
    completed_fraction: float | None = None,
) -> None:
    """Move a print to *status*, optionally recording when and why it ended.

    *completed_fraction* is what makes a booking possible for a print that was
    stopped: it is stored rather than used on the spot, so that booking it later
    by hand uses the same number.
    """
    values: dict[str, Any] = {"status": status}
    if finished_at is not None:
        values["finished_at"] = finished_at
    if error is not None:
        values["error"] = error
    if completed_fraction is not None:
        values["completed_fraction"] = completed_fraction

    await db.execute(update(prints_table).where(prints_table.c.id == print_id).values(**values))


async def set_print_spent(db: AsyncSession, print_id: int, spent: bool) -> None:
    """Record whether everything bookable on this print has been booked."""
    await db.execute(
        update(prints_table).where(prints_table.c.id == print_id).values(spent=spent)
    )


async def set_filament_spool(
    db: AsyncSession,
    filament_row_id: int,
    spool_id: int | None,
    manual: bool = True,
) -> None:
    """Attach a spool to one row after the fact.

    *manual* marks it as done by hand, which is what the interface does. The
    listener passes False when it merely picks up an assignment that reached
    FilaMan a moment after the print had already started.
    """
    values: dict[str, Any] = {"spool_id": spool_id}
    if manual:
        values["manual_override"] = True

    await db.execute(
        update(filament_table).where(filament_table.c.id == filament_row_id).values(**values)
    )


async def narrow_filament_row(
    db: AsyncSession,
    filament_row_id: int,
    estimated_grams: float | None,
    estimated_length_m: float | None,
    to_fraction: float,
) -> None:
    """Close a filament row off at *to_fraction* and shrink its estimate to match.

    The other half of a split; the remainder is inserted as a row of its own.
    See service.split_filament_row.
    """
    await db.execute(
        update(filament_table)
        .where(filament_table.c.id == filament_row_id)
        .values(
            estimated_grams=estimated_grams,
            estimated_length_m=estimated_length_m,
            to_fraction=to_fraction,
        )
    )


async def mark_filament_spent(
    db: AsyncSession,
    filament_row_id: int,
    grams: float,
    spent_at: datetime,
) -> None:
    """Record what was actually booked for one row.

    ``estimated_grams`` is never touched, so the slicer's number stays
    comparable against the scale. See docs/04_Data_Model.md section 4.
    """
    await db.execute(
        update(filament_table)
        .where(filament_table.c.id == filament_row_id)
        .values(spent_grams=grams, spent_at=spent_at)
    )


async def override_filament_amount(
    db: AsyncSession,
    filament_row_id: int,
    grams: float,
    spent_at: datetime,
) -> None:
    """Set the booked amount of one row by hand, and settle it.

    A corrected row counts as booked from then on, whether or not it ever was,
    so the same correction cannot be applied twice.
    """
    await db.execute(
        update(filament_table)
        .where(filament_table.c.id == filament_row_id)
        .values(spent_grams=grams, spent_at=spent_at, manual_override=True)
    )


async def read_thumbnail(db: AsyncSession, print_id: int) -> tuple[bytes, str] | None:
    """The stored plate preview of one print, with its mime type."""
    result = await db.execute(
        select(prints_table.c.thumbnail, prints_table.c.thumbnail_mime).where(
            prints_table.c.id == print_id
        )
    )
    row = result.first()
    if row is None or row.thumbnail is None:
        return None

    return bytes(row.thumbnail), row.thumbnail_mime or ""


async def upsert_printer_status(
    db: AsyncSession,
    *,
    printer_id: int,
    printer_name: str | None,
    connected: bool,
    tracking_enabled: bool,
    updated_at: datetime,
    current_print_id: int | None = None,
    current_file_name: str | None = None,
    progress_percent: int | None = None,
    last_error: str | None = None,
) -> None:
    """Write the live state of one listener.

    An update that hits no row becomes an insert, the same portable pattern the
    settings use. This row is the only place the other worker processes can see
    what the listeners are doing.
    """
    values = {
        "printer_name": printer_name,
        "connected": connected,
        "tracking_enabled": tracking_enabled,
        "current_print_id": current_print_id,
        "current_file_name": current_file_name,
        "progress_percent": progress_percent,
        "last_error": last_error,
        "updated_at": updated_at,
    }

    result = await db.execute(
        update(printer_status_table)
        .where(printer_status_table.c.printer_id == printer_id)
        .values(**values)
    )
    if result.rowcount == 0:
        await db.execute(insert(printer_status_table).values(printer_id=printer_id, **values))


async def list_printer_status(db: AsyncSession) -> list[Any]:
    """The live state of every watched printer."""
    result = await db.execute(
        select(printer_status_table).order_by(printer_status_table.c.printer_id)
    )
    return list(result.all())


async def forget_printers(db: AsyncSession, keep: list[int]) -> None:
    """Drop the status rows of printers that are no longer watched.

    A printer removed or deactivated in FilaMan would otherwise keep a stale row
    on the page for good.
    """
    statement = delete(printer_status_table)
    if keep:
        statement = statement.where(printer_status_table.c.printer_id.not_in(keep))

    await db.execute(statement)
