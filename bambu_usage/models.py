"""Database tables owned by this plugin.

The tables live in a private ``MetaData`` so Alembic, which manages FilaMan's
own schema, never sees them. They are created lazily via ``CREATE TABLE IF NOT
EXISTS`` on first access, which means they survive a plugin ZIP update and an
uninstall keeps the recorded history around for a later reinstall.

``printer_id`` and ``spool_id`` are plain integers on purpose. A real foreign
key into FilaMan's tables would either block deleting a spool or silently take
the history with it. History should outlive the spool it refers to.

``bambu_usage_printer_status`` is the odd one out: it holds live state, not
history. It exists because the listeners run in one worker process while the
plugin page is answered by any of the four, so the only place all of them can
see is the database. Its rows are always current and never accumulate, one per
watched printer.

Column semantics are documented in docs/04_Data_Model.md.

May import: sqlalchemy. Must not import tracker, router, service or schemas.
Enforced by tools/check_architecture.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    delete,
    func,
    inspect,
    select,
    text,
)

if TYPE_CHECKING:  # imported for annotations only
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

# Private metadata: keeps these tables out of FilaMan's Alembic migrations.
metadata = MetaData()

# Table name prefix, so the origin of every table is obvious in the database.
TABLE_PREFIX = "bambu_usage_"

# Sentinel printer id for the global settings row.
GLOBAL_SETTINGS_PRINTER_ID = 0

# Print status values written to bambu_usage_prints.status.
STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_INCOMPLETE = "incomplete"
STATUS_NO_3MF = "no_3mf"

# A print in one of these has not ended yet, whatever else is true about it. A
# listener that reattaches after a restart looks for exactly these.
OPEN_STATUSES = frozenset({STATUS_RUNNING, STATUS_INCOMPLETE, STATUS_NO_3MF})

# ... and in one of these it can never be booked, so ending it must not quietly
# relabel it as a normal finish.
UNBOOKABLE_STATUSES = frozenset({STATUS_INCOMPLETE, STATUS_NO_3MF})

# A print in one of these was stopped part way, so it costs the share of the
# estimate it got through rather than all of it.
STOPPED_STATUSES = frozenset({STATUS_FAILED, STATUS_CANCELLED})


settings_table = Table(
    f"{TABLE_PREFIX}settings",
    metadata,
    Column("printer_id", Integer, primary_key=True),
    Column("tracking_enabled", Boolean, nullable=False, default=True),
    Column("auto_spend", Boolean, nullable=False, default=True),
    Column("spend_on_cancel", Boolean, nullable=False, default=True),
    Column("clear_assignment_when_empty", Boolean, nullable=False, default=False),
    Column("history_retention_days", Integer, nullable=False, default=365),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)


prints_table = Table(
    f"{TABLE_PREFIX}prints",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("printer_id", Integer, nullable=False, index=True),
    Column("subtask_id", String(100), nullable=True),
    Column("file_name", String(512), nullable=False),
    Column("print_type", String(20), nullable=False),
    Column("plate_id", Integer, nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=False, index=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("status", String(20), nullable=False),
    # How much of the print ran, 0.0 to 1.0, for a print that did not finish.
    # None means it could not be determined, and then nothing is booked rather
    # than a number being invented.
    Column("completed_fraction", Float, nullable=True),
    Column("spent", Boolean, nullable=False, default=False),
    Column("thumbnail", LargeBinary, nullable=True),
    Column("thumbnail_mime", String(50), nullable=True),
    Column("error", String(512), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    # A repeated MQTT message must not create a second print. Where the printer
    # reports a subtask id this is exact; the fallback is handled in service.py.
    UniqueConstraint("printer_id", "subtask_id", name="uq_bambu_usage_print_subtask"),
)


filament_table = Table(
    f"{TABLE_PREFIX}filament",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "print_id",
        Integer,
        ForeignKey(f"{TABLE_PREFIX}prints.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    ),
    # Slicer filament number, 1-based as it appears in slice_info.config.
    Column("filament_id", Integer, nullable=False),
    # "<ams_id>-<tray_id>", "255-254" for the external spool.
    Column("slot_index", String(20), nullable=True),
    # NULL means the spool could not be resolved and needs manual assignment.
    Column("spool_id", Integer, nullable=True, index=True),
    Column("material", String(50), nullable=True),
    Column("color_hex", String(20), nullable=True),
    Column("tray_info_idx", String(50), nullable=True),
    # The slicer estimate. Never overwritten, so a correction stays traceable.
    Column("estimated_grams", Float, nullable=True),
    Column("estimated_length_m", Float, nullable=True),
    # What was actually booked against the spool.
    Column("spent_grams", Float, nullable=True),
    Column("spent_at", DateTime(timezone=True), nullable=True),
    Column("manual_override", Boolean, nullable=False, default=False),
)


printer_status_table = Table(
    f"{TABLE_PREFIX}printer_status",
    metadata,
    Column("printer_id", Integer, primary_key=True),
    Column("printer_name", String(255), nullable=True),
    Column("connected", Boolean, nullable=False, default=False),
    Column("tracking_enabled", Boolean, nullable=False, default=True),
    Column("current_print_id", Integer, nullable=True),
    Column("current_file_name", String(512), nullable=True),
    Column("progress_percent", Integer, nullable=True),
    Column("last_error", String(512), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


async def ensure_tables(engine: AsyncEngine) -> None:
    """Bring the plugin tables up to date with what this version declares.

    Idempotent and safe to call from every uvicorn worker: ``checkfirst`` turns
    the creation into a no-op for every table that is already there, so no worker
    has to know whether it is the first one.

    Creation alone is not enough. ``create_all`` only ever creates whole tables,
    so a column added in a later version of this plugin would simply be missing
    on an instance that already ran an earlier one, and every query naming it
    would fail. Alembic never sees these tables, so this is the only place an
    additive change can happen.

    The engine is passed in rather than imported, which keeps this module free of
    any knowledge about where FilaMan keeps it.
    """
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all, checkfirst=True)
        await connection.run_sync(_add_missing_columns)


def _add_missing_columns(connection: Any) -> None:
    """Add columns an existing table has not got yet.

    Only nullable columns can be added this way, which is exactly what the
    additive-only rule in CLAUDE.md allows: a NOT NULL column would need a value
    for every row that already exists, and inventing one is how history gets
    corrupted. Such a column is refused loudly rather than added wrongly.
    """
    inspector = inspect(connection)
    quote = connection.dialect.identifier_preparer.quote

    for table in metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue

        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue

            if not column.nullable and column.server_default is None:
                raise RuntimeError(
                    f"cannot add {table.name}.{column.name} to an existing table: "
                    "a new column has to be nullable, see CLAUDE.md section 6"
                )

            definition = column.type.compile(connection.dialect)
            connection.execute(
                text(f"ALTER TABLE {quote(table.name)} ADD COLUMN {quote(column.name)} {definition}")
            )


async def purge_expired_history(
    db: AsyncSession,
    retention_days: int,
    now: datetime | None = None,
) -> int:
    """Delete prints older than *retention_days* and return how many went.

    A retention of 0 means keep everything. Thumbnails are stored inline, so
    this is what bounds the growth of the database.

    The filament rows are deleted explicitly instead of through the declared
    cascade. The cascade only fires where the database enforces foreign keys,
    and orphaned filament rows would be invisible in the interface, which is the
    worst kind of leak.

    Committing is left to the caller, so a purge can share the transaction of
    whatever triggered it.
    """
    if retention_days <= 0:
        return 0

    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=retention_days)

    expired = select(prints_table.c.id).where(prints_table.c.started_at < cutoff)
    await db.execute(delete(filament_table).where(filament_table.c.print_id.in_(expired)))
    result = await db.execute(delete(prints_table).where(prints_table.c.started_at < cutoff))

    return result.rowcount or 0
