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


async def get_history(
    db: AsyncSession,
    limit: int = 50,
    offset: int = 0,
    *,
    search: str | None = None,
    printer_id: int | None = None,
    hide_failed: bool = False,
    order: str = "newest",
) -> list[PrintRecord]:
    """Return prints, newest first, with their filament breakdown.

    ``printer_name`` and ``spool_label`` are left empty on purpose. The page
    already holds FilaMan's printer and spool lists for its dropdowns, so
    resolving the names there costs nothing, while doing it here would add two
    more couplings into FilaMan for display text alone.
    """
    from . import filaman, store
    from .schemas import FilamentUsage, PrintRecord

    prints = await store.list_prints(
        db,
        limit=limit,
        offset=offset,
        search=search,
        printer_id=printer_id,
        hide_failed=hide_failed,
        order=order,
    )
    rows = await store.list_filaments_for(db, [int(entry.id) for entry in prints])

    by_print: dict[int, list[Any]] = {}
    for row in rows:
        by_print.setdefault(int(row.print_id), []).append(row)

    prices = await filaman.load_spool_prices(
        db, sorted({int(row.spool_id) for row in rows if row.spool_id is not None})
    )
    live_layers = await _live_layers(db)

    records = []
    for entry in prints:
        layer = live_layers.get(int(entry.id))
        curves = store.decode_layer_shares(entry.layer_shares) if layer else {}

        filaments = [
            FilamentUsage(
                id=int(row.id),
                filament_id=int(row.filament_id),
                slot_index=row.slot_index,
                spool_id=row.spool_id,
                material=row.material,
                color_hex=row.color_hex,
                estimated_grams=row.estimated_grams,
                estimated_length_m=row.estimated_length_m,
                spent_grams=row.spent_grams,
                spent_at=row.spent_at,
                manual_override=bool(row.manual_override),
                spool_source=row.spool_source,
                used_so_far=_used_so_far(row, curves, layer),
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
                printer_error_code=entry.printer_error_code,
                layer_count=_layer_count(entry.layer_shares),
                estimated_seconds=entry.estimated_seconds,
                object_count=entry.object_count,
                nozzle_diameter=entry.nozzle_diameter,
                cost=rules.print_cost(by_print.get(int(entry.id), []), prices),
                has_thumbnail=entry.thumbnail_mime is not None,
                error=entry.error,
                filaments=filaments,
            )
        )

    return records


async def _live_layers(db: AsyncSession) -> dict[int, int]:
    """Which print each printer is on right now, and which layer it reached.

    Read from the status rows because they are what the listeners keep current;
    the print row itself only learns the layer when the print stops.
    """
    from . import store

    return {
        int(row.current_print_id): int(row.layer_num)
        for row in await store.list_printer_status(db)
        if row.current_print_id is not None and row.layer_num
    }


def _used_so_far(row: Any, curves: dict[int, list[float]], layer: int | None) -> float | None:
    """What a running print has laid down of one filament by now.

    Deliberately the same arithmetic the booking uses for a print that stopped,
    so the figure somebody watches grows towards exactly what will be booked
    rather than towards a second opinion. Without a curve there is no answer:
    the share of the layers says nothing about a filament used only at the end,
    and a wrong number here would be read as fact.
    """
    share = rules.share_at_layer(curves.get(int(row.filament_id)), layer)
    return None if share is None else rules.share_of(row, share)


def _layer_count(stored: str | None) -> int | None:
    """How many layers a print has, read off the curves it stored.

    Every curve has one entry per layer, so any of them answers it and no
    column of its own is needed.
    """
    from . import store

    for curve in store.decode_layer_shares(stored).values():
        return len(curve)
    return None


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
            active_slot_index=row.active_slot_index,
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
