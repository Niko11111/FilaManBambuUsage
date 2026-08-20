"""Turning stored rows into what the page shows.

The read side, kept apart from service.py so the booking path is not read
through a layer of presentation. Nothing here decides or writes anything: it
loads rows, adds what FilaMan knows about the spools behind them, and builds the
models in schemas.py.

``printer_name`` and ``spool_label`` stay empty on purpose. The page already
holds FilaMan's printer and spool lists for its dropdowns, so resolving the names
there costs nothing, while doing it here would add two more couplings into
FilaMan for display text alone.

May import: store, schemas, filaman, rules. Must not import tracker, supervisor,
router, app, bambulabs_api or fastapi. Enforced by tools/check_architecture.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import rules

if TYPE_CHECKING:  # imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession

    from .schemas import PrinterStatus, PrintRecord


async def get_history(db: AsyncSession, limit: int = 50, offset: int = 0) -> list[PrintRecord]:
    """Return prints, newest first, with their filament breakdown.

    ``printer_name`` and ``spool_label`` are left empty on purpose. The page
    already holds FilaMan's printer and spool lists for its dropdowns, so
    resolving the names there costs nothing, while doing it here would add two
    more couplings into FilaMan for display text alone.
    """
    from . import filaman, store
    from .schemas import FilamentUsage, PrintRecord

    prints = await store.list_prints(db, limit=limit, offset=offset)
    rows = await store.list_filaments_for(db, [int(entry.id) for entry in prints])

    by_print: dict[int, list[Any]] = {}
    for row in rows:
        by_print.setdefault(int(row.print_id), []).append(row)

    prices = await filaman.load_spool_prices(
        db, sorted({int(row.spool_id) for row in rows if row.spool_id is not None})
    )

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
                cost=rules.print_cost(by_print.get(int(entry.id), []), prices),
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
            layer_num=row.layer_num,
            total_layer_num=row.total_layer_num,
            remaining_minutes=row.remaining_minutes,
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
