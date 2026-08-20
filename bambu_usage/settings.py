"""Persistent plugin settings.

Follows the pattern of FilaMan's own spoolmanapi plugin: settings live in a
dedicated table rather than in the plugin directory, so a ZIP update does not
wipe them.

Resolution order for a printer: the row for that printer if it exists, otherwise
the global row (printer_id 0), otherwise the defaults declared on
``schemas.PluginSettings``. A printer therefore inherits every setting it has no
opinion of its own about.

**Nothing is cached.** FilaMan runs as four Gunicorn workers, each its own
process (`Dockerfile`, `gunicorn -w 4`). A cache in module state would go stale
in three of them the moment the fourth handled a write, and the wrong value
would be invisible rather than loud: a listener would keep booking against
settings the user had already changed. Reading is one row by primary key, a few
times per print, so there is nothing here worth the risk.

May import: models, schemas. Must not import tracker, router or service.
Enforced by tools/check_architecture.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import insert, select, update

from .models import GLOBAL_SETTINGS_PRINTER_ID, settings_table
from .schemas import PluginSettings

if TYPE_CHECKING:  # imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncSession


async def load_settings(
    db: AsyncSession,
    printer_id: int = GLOBAL_SETTINGS_PRINTER_ID,
) -> PluginSettings:
    """Return the effective settings for *printer_id*.

    Always read from the database, see the module docstring.
    """
    row = await _read_row(db, printer_id)
    if row is None and printer_id != GLOBAL_SETTINGS_PRINTER_ID:
        row = await _read_row(db, GLOBAL_SETTINGS_PRINTER_ID)

    return _to_settings(row, printer_id)


async def save_settings(db: AsyncSession, settings: PluginSettings) -> None:
    """Write *settings*.

    An update that hits no row becomes an insert. That is portable across
    SQLite, MySQL and PostgreSQL, unlike a dialect specific upsert, and this
    table is written by a human now and then, never in a loop.

    Committing is left to the caller.
    """
    values = _writable_values(settings)

    result = await db.execute(
        update(settings_table)
        .where(settings_table.c.printer_id == settings.printer_id)
        .values(**values)
    )
    if result.rowcount == 0:
        await db.execute(
            insert(settings_table).values(printer_id=settings.printer_id, **values)
        )


async def list_settings(db: AsyncSession) -> list[PluginSettings]:
    """Return the global row plus every per-printer override.

    The global row is always part of the answer, with its defaults, so the
    interface has something to render before anything was ever saved.
    """
    rows = (
        await db.execute(select(settings_table).order_by(settings_table.c.printer_id))
    ).all()

    stored = [_to_settings(row, row.printer_id) for row in rows]
    if not any(entry.printer_id == GLOBAL_SETTINGS_PRINTER_ID for entry in stored):
        stored.insert(0, PluginSettings(printer_id=GLOBAL_SETTINGS_PRINTER_ID))

    return stored


async def _read_row(db: AsyncSession, printer_id: int) -> Any | None:
    """Return the raw settings row for *printer_id*, or None."""
    result = await db.execute(
        select(settings_table).where(settings_table.c.printer_id == printer_id)
    )
    return result.first()


def _to_settings(row: Any | None, printer_id: int) -> PluginSettings:
    """Build the wire model from one row, for the printer that was asked for.

    *printer_id* wins over the row's own id, because a printer without a row of
    its own is answered from the global row and must still see itself.
    """
    if row is None:
        return PluginSettings(printer_id=printer_id)

    return PluginSettings(
        printer_id=printer_id,
        tracking_enabled=row.tracking_enabled,
        auto_spend=row.auto_spend,
        spend_on_cancel=row.spend_on_cancel,
        history_retention_days=row.history_retention_days,
    )


def _writable_values(settings: PluginSettings) -> dict[str, Any]:
    """Map the wire model onto the table columns, printer_id excluded.

    Written out rather than derived from the model, because this mapping is the
    job of this module: it is the one place where a renamed column or a new
    setting has to be reflected, and it stays independent of the Pydantic
    version FilaMan happens to ship.
    """
    return {
        "tracking_enabled": settings.tracking_enabled,
        "auto_spend": settings.auto_spend,
        "spend_on_cancel": settings.spend_on_cancel,
        "history_retention_days": settings.history_retention_days,
        "updated_at": datetime.now(timezone.utc),
    }
