"""The seam to FilaMan.

This is the only module that reaches into FilaMan's own ``app`` package, and it
is the executable form of the coupling table in ``docs/02_FilaMan_Plugin_API.md``
section 8. When a FilaMan update moves a model or renames a service, exactly one
file of this plugin has to follow.

Every import of ``app`` and of SQLAlchemy happens inside the function that needs
it, never at the module header. Two reasons, both deliberate:

  * ``app`` exists only inside a running FilaMan. A module level import would
    make this plugin unimportable for the test suite, which runs on the standard
    library alone, and would take every pure helper down with it.
  * A moved or renamed FilaMan internal becomes one named error,
    ``FilaManUnavailableError``, instead of an ImportError raised from somewhere
    deep inside an MQTT callback.

Nothing here decides anything. Reading is reading, and booking is delegated to
FilaMan's ``SpoolService``, which owns the sign, the aggregation and the clamp at
zero (``docs/04_Data_Model.md`` section 3).

May import: nothing from this plugin. Must not import tracker, router, service,
store, bambulabs_api or fastapi. Enforced by tools/check_architecture.py.
"""

from __future__ import annotations

import importlib
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # imported for annotations only
    from collections.abc import AsyncIterator
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

logger = logging.getLogger(__name__)

# The driver plugin whose slot state this plugin reads. A printer on any other
# driver is none of our business.
BAMBU_DRIVER_KEY = "bambulab"

# Written to SpoolEvent.source, so a booking is recognisable in FilaMan's spool
# log and separable from the scale and from manual entry.
CONSUMPTION_SOURCE = "bambu_usage"

# Keys the Bambu Lab driver stores in Printer.driver_config through its
# config_schema. A printer missing one of them cannot be watched.
CONFIG_HOST = "host"
CONFIG_SERIAL = "serial"
CONFIG_ACCESS_CODE = "access_code"

# Where the Bambu Lab driver records "<ams_id>-<tray_id>" on a slot. This is the
# bridge between a tray number out of ams_mapping and a FilaMan spool.
SLOT_INDEX_FIELD = "slot_index"

# FilaMan's own word for a correction that moves the remaining weight by a given
# amount, in either direction.
ADJUSTMENT_RELATIVE = "relative"


class FilaManUnavailableError(RuntimeError):
    """FilaMan's internals are not importable, or they have moved."""


@dataclass(frozen=True)
class BambuPrinter:
    """One printer this plugin may watch, reduced to what a listener needs.

    Built at this boundary so no raw driver_config dict travels deeper into the
    plugin.
    """

    printer_id: int
    name: str
    host: str
    serial: str
    access_code: str


def _import_names(module_name: str, *names: str) -> tuple[Any, ...]:
    """Return named attributes out of a module the FilaMan runtime provides.

    A missing module and a missing attribute end up as the same error class on
    purpose: from this plugin's point of view they are one failure, namely that
    the host application does not look the way this module expects.
    """
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise FilaManUnavailableError(
            f"module '{module_name}' is not importable, "
            "this plugin only runs inside FilaMan"
        ) from exc

    try:
        return tuple(getattr(module, name) for name in names)
    except AttributeError as exc:
        raise FilaManUnavailableError(
            f"'{module_name}' does not provide {', '.join(names)}, "
            "FilaMan may have moved or renamed it"
        ) from exc


def get_engine() -> AsyncEngine:
    """Return FilaMan's database engine, for DDL that needs no session."""
    (engine,) = _import_names("app.core.database", "engine")
    return engine


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a database session for work that has no HTTP request behind it.

    The listeners run outside any request and therefore cannot use FilaMan's
    DBSession dependency. Committing is left to the caller, so that one unit of
    work stays one transaction.
    """
    (async_session_maker,) = _import_names("app.core.database", "async_session_maker")
    async with async_session_maker() as session:
        yield session


async def list_bambu_printers(db: AsyncSession) -> list[BambuPrinter]:
    """Return the active printers driven by the Bambu Lab plugin.

    Credentials come out of Printer.driver_config, so a printer is configured
    exactly once, in FilaMan. This plugin never asks for them again.
    """
    (select,) = _import_names("sqlalchemy", "select")
    (Printer,) = _import_names("app.models.printer", "Printer")

    statement = select(Printer).where(
        Printer.driver_key == BAMBU_DRIVER_KEY,
        Printer.is_active.is_(True),
        Printer.deleted_at.is_(None),
    )
    rows = (await db.execute(statement)).scalars().all()

    printers = []
    for row in rows:
        printer = _to_bambu_printer(row)
        if printer is not None:
            printers.append(printer)
    return printers


def _to_bambu_printer(row: Any) -> BambuPrinter | None:
    """Convert one Printer record into this plugin's own dataclass.

    Returns None when the driver config is incomplete. A printer the Bambu Lab
    driver itself cannot reach is not an error of this plugin, it is a printer
    to skip, and skipping it must not take the other printers down.
    """
    config = row.driver_config
    if not isinstance(config, dict):
        logger.warning("Printer %s has no usable driver_config, skipping", row.id)
        return None

    host = config.get(CONFIG_HOST)
    serial = config.get(CONFIG_SERIAL)
    access_code = config.get(CONFIG_ACCESS_CODE)
    if not (host and serial and access_code):
        logger.warning(
            "Printer %s is missing host, serial or access code in driver_config, skipping",
            row.id,
        )
        return None

    return BambuPrinter(
        printer_id=row.id,
        name=row.name,
        host=str(host),
        serial=str(serial),
        access_code=str(access_code),
    )


async def resolve_spool_for_slot(
    db: AsyncSession,
    printer_id: int,
    slot_index: str,
) -> int | None:
    """Look up which FilaMan spool currently sits in *slot_index*.

    The slot is matched in Python rather than in SQL. slot_index lives inside the
    JSON column PrinterSlot.custom_fields, JSON access differs between SQLite,
    MySQL and PostgreSQL, and a printer has at most a handful of slots. A
    dialect independent comparison is worth more here than an index.

    Returns None when the slot is unknown or holds nothing. That is a normal
    outcome and not an error: the caller records the row for manual assignment
    instead of guessing.
    """
    (select,) = _import_names("sqlalchemy", "select")
    PrinterSlot, PrinterSlotAssignment = _import_names(
        "app.models.printer", "PrinterSlot", "PrinterSlotAssignment"
    )

    slots = (
        await db.execute(select(PrinterSlot).where(PrinterSlot.printer_id == printer_id))
    ).scalars().all()

    slot = next((s for s in slots if _slot_index_of(s) == slot_index), None)
    if slot is None:
        return None

    assignment = (
        await db.execute(
            select(PrinterSlotAssignment).where(PrinterSlotAssignment.slot_id == slot.id)
        )
    ).scalars().first()

    return assignment.spool_id if assignment is not None else None


def _slot_index_of(slot: Any) -> str | None:
    """Read custom_fields["slot_index"] off a PrinterSlot, tolerating anything.

    The column is free form JSON written by another plugin, so every shape other
    than a dict carrying the key is treated as "this slot has no index".
    """
    fields = slot.custom_fields
    if not isinstance(fields, dict):
        return None

    value = fields.get(SLOT_INDEX_FIELD)
    return None if value is None else str(value)


async def load_spool(db: AsyncSession, spool_id: int) -> Any | None:
    """Load one spool. Returns None when it no longer exists.

    History outlives the spool it refers to, which is why a missing spool is an
    expected answer rather than a failure.
    """
    (Spool,) = _import_names("app.models.spool", "Spool")
    return await db.get(Spool, spool_id)


async def record_consumption(
    db: AsyncSession,
    spool: Any,
    grams: float,
    event_at: datetime,
    note: str | None = None,
) -> None:
    """Book *grams* against *spool* through FilaMan's own service.

    Pass a positive value: SpoolService flips the sign, writes the SpoolEvent
    that forms the audit trail, aggregates events from the same source that fall
    close together and clamps the remaining weight at zero. This plugin
    recomputes none of that.

    **The call commits the session**, on every path through it. Whatever this
    plugin has pending in the same session is committed along with it, which is
    what decides the order of operations in service.py.
    """
    (SpoolService,) = _import_names("app.services.spool_service", "SpoolService")
    await SpoolService(db).record_consumption(
        spool,
        delta_weight_g=grams,
        event_at=event_at,
        source=CONSUMPTION_SOURCE,
        note=note,
    )


async def record_adjustment(
    db: AsyncSession,
    spool: Any,
    delta_grams: float,
    event_at: datetime,
    note: str | None = None,
) -> None:
    """Move the remaining weight of *spool* by *delta_grams*.

    Positive puts material back, negative takes more away. This is the only way
    to correct a booking downwards: record_consumption turns a positive value
    into a deduction and can therefore never give anything back.

    Commits the session, exactly like record_consumption.
    """
    (SpoolService,) = _import_names("app.services.spool_service", "SpoolService")
    await SpoolService(db).record_adjustment(
        spool,
        adjustment_type=ADJUSTMENT_RELATIVE,
        event_at=event_at,
        delta_weight_g=delta_grams,
        source=CONSUMPTION_SOURCE,
        note=note,
    )
