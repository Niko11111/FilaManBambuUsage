"""Database tables owned by this plugin.

The tables live in a private ``MetaData`` so Alembic, which manages FilaMan's
own schema, never sees them. They are created lazily via ``CREATE TABLE IF NOT
EXISTS`` on first access, which means they survive a plugin ZIP update and an
uninstall keeps the recorded history around for a later reinstall.

``printer_id`` and ``spool_id`` are plain integers on purpose. A real foreign
key into FilaMan's tables would either block deleting a spool or silently take
the history with it. History should outlive the spool it refers to.

Column semantics are documented in docs/04_Data_Model.md.

May import: sqlalchemy. Must not import tracker, router, service or schemas.
Enforced by tools/check_architecture.py.
"""

from __future__ import annotations

from datetime import datetime

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
    func,
)

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


settings_table = Table(
    f"{TABLE_PREFIX}settings",
    metadata,
    Column("printer_id", Integer, primary_key=True),
    Column("tracking_enabled", Boolean, nullable=False, default=True),
    Column("auto_spend", Boolean, nullable=False, default=True),
    Column("spend_on_cancel", Boolean, nullable=False, default=False),
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


async def ensure_tables() -> None:
    """Create the plugin tables if they do not exist yet.

    Idempotent and safe to call from every worker.
    """
    raise NotImplementedError


async def purge_expired_history(retention_days: int, now: datetime | None = None) -> int:
    """Delete prints older than *retention_days* and return how many went.

    A retention of 0 means keep everything. Thumbnails are stored inline, so
    this is what bounds the growth of the database.
    """
    raise NotImplementedError
