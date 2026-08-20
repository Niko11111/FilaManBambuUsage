"""HTTP surface of the plugin.

FilaMan mounts two routers out of this module. ``router`` goes under the
manifest's mount_prefix, "/bambu-usage", and carries everything that only reads.
``admin_router`` goes under "/api/v1" and carries **every write**, because
FilaMan's CsrfMiddleware only guards paths below "/api/v1/". A state changing
endpoint anywhere else would be the one write on the instance without that
protection. The pattern is FilaMan's own, see the spoolmanapi plugin.

The page itself is served by FilaMan's catch-all at /plugin-page/bambu-usage and
resolved per request, so it appears right after installation while a newly
mounted router needs a restart.

**There is no startup hook.** FilaMan imports this module and calls
``mount_deferred_plugin_routers(app)`` at module import time, before uvicorn
starts an event loop, and it hands FastAPI its own ``lifespan``, which makes
router ``on_startup`` handlers inert. The first request into this router is
therefore the earliest moment any code of this plugin can run, which is what
``ensure_ready`` exists for. See docs/02_FilaMan_Plugin_API.md section 4.

May import: schemas, service, settings, models, filaman. Must not import tracker
(no printer access from an HTTP handler) or bambulabs_api. Enforced by
tools/check_architecture.py.

This module and filaman.py are the only two that import FilaMan's ``app``
package. Here it is unavoidable: FastAPI resolves its dependencies while the
decorators run, so the authentication dependencies have to be present at import
time and cannot be fetched lazily.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from app.api.deps import DBSession, RequirePermission, require_auth
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import SQLAlchemyError

from . import __version__, filaman, models, schemas, settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["bambu-usage"])

# Mounted by FilaMan under "/api/v1", so the full path of a write is
# /api/v1/plugins/bambu-usage/... . Writes live here and nowhere else, see the
# module docstring. A session authenticated write therefore has to carry the
# X-CSRF-Token header matching the csrf_token cookie, exactly as FilaMan's own
# interface does it.
ADMIN_PREFIX = "/plugins/bambu-usage"
admin_router = APIRouter(prefix=ADMIN_PREFIX, tags=["bambu-usage"])

# Translation dictionaries ship inside the package, because .json is one of the
# few extensions FilaMan's plugin ZIP accepts. See docs/01_Design.md section 9.
LOCALES_DIR = Path(__file__).parent / "locales"
FALLBACK_LANGUAGE = "en"

# A language code reaches us straight from the browser, so it is never used to
# build a path before it has matched this. "en", "de", "pt-br".
LANGUAGE_PATTERN = re.compile(r"^[a-z]{2}(-[a-z]{2})?$")

JSON_MEDIA_TYPE = "application/json"

# Clients may cache a dictionary for an hour. It only changes on a plugin update.
LOCALE_CACHE_SECONDS = 3600

# Changing a setting of this plugin changes how a printer is tracked, so it
# borrows FilaMan's own permission for changing a printer. Inventing a key of
# our own would be worse than useless: RequirePermission resolves keys against
# the roles FilaMan seeds, and an unknown key belongs to no role, which would
# lock out everybody except a superadmin.
SETTINGS_PERMISSION = "printers:update"

# Assigning a spool, correcting an amount and booking a print all end in a
# consumption event on a spool, so they borrow the permission FilaMan uses for
# exactly that on its own endpoints.
CONSUMPTION_PERMISSION = "spool_events:create_consumption"

# Set once the plugin's tables have been ensured in this worker. The lock keeps
# concurrent first requests from racing each other into create_all.
_tables_ready = asyncio.Event()
_bootstrap_lock = asyncio.Lock()


async def ensure_ready() -> None:
    """Create the plugin's tables once per worker, on first use.

    Used as a dependency on every route that touches the database. It is not on
    the router itself: /health and /i18n answer without a database, and that is
    what makes them a reliable answer to the question whether the plugin mounted
    at all.
    """
    if _tables_ready.is_set():
        return

    async with _bootstrap_lock:
        if _tables_ready.is_set():
            return

        try:
            await models.ensure_tables(filaman.get_engine())
        except (filaman.FilaManUnavailableError, SQLAlchemyError) as exc:
            logger.exception("bambu_usage: could not create the plugin tables")
            raise _database_unavailable(exc) from exc

        _tables_ready.set()
        logger.info("bambu_usage: plugin tables are ready")


def _database_unavailable(exc: Exception) -> HTTPException:
    """Turn a database failure into a translatable answer.

    The code is what the page translates, the message is what keeps a raw curl
    readable. See docs/01_Design.md section 9.
    """
    logger.warning("bambu_usage: database call failed: %s", exc)
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "errors.databaseUnavailable",
            "message": "The plugin database is not available",
        },
    )


def _not_implemented() -> HTTPException:
    """Stub response carrying a translatable code plus an English fallback."""
    return HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail={"code": "errors.notImplemented", "message": "Not implemented yet"},
    )


def _load_locale(language: str) -> str | None:
    """Return the raw JSON of one dictionary, or None if there is no such file."""
    path = LOCALES_DIR / f"{language}.json"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


@router.get("/health", response_model=schemas.HealthResponse)
async def health() -> schemas.HealthResponse:
    """Liveness of the plugin.

    Answers without touching the database or any printer, so it stays a reliable
    answer to the question whether the plugin is mounted. ``tables_ready`` tells
    whether this worker has run its one-time setup yet, which is the only way to
    see from outside that a request driven bootstrap took place.
    """
    return schemas.HealthResponse(
        plugin="bambu_usage",
        version=__version__,
        tracking_active=False,
        printers_watched=0,
        tables_ready=_tables_ready.is_set(),
    )


@router.get("/i18n/languages", response_model=list[str])
async def available_languages() -> list[str]:
    """List the languages that ship with this build.

    Derived from the files present, so adding a language means adding a file and
    nothing else.
    """
    try:
        return sorted(p.stem for p in LOCALES_DIR.glob("*.json"))
    except OSError:
        return [FALLBACK_LANGUAGE]


@router.get("/i18n/{language}")
async def translations(language: str) -> Response:
    """Return one translation dictionary, falling back to English.

    Served as a raw body rather than a parsed model: the content is a nested
    dictionary of free form keys, and re-serialising it through Pydantic would
    only cost time and risk reordering.
    """
    if not LANGUAGE_PATTERN.match(language):
        language = FALLBACK_LANGUAGE

    body = _load_locale(language)
    if body is None and language != FALLBACK_LANGUAGE:
        body = _load_locale(FALLBACK_LANGUAGE)

    if body is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "errors.localesMissing",
                "message": "Translation files are missing from the installed plugin",
            },
        )

    # media_type is explicit here, as it must be on every raw Response.
    return Response(
        content=body,
        media_type=JSON_MEDIA_TYPE,
        headers={"Cache-Control": f"max-age={LOCALE_CACHE_SECONDS}"},
    )


@router.get(
    "/settings",
    response_model=list[schemas.PluginSettings],
    dependencies=[Depends(require_auth), Depends(ensure_ready)],
)
async def get_settings(db: DBSession) -> list[schemas.PluginSettings]:
    """Global defaults plus every per-printer override."""
    try:
        return await settings.list_settings(db)
    except SQLAlchemyError as exc:
        raise _database_unavailable(exc) from exc


@admin_router.put(
    "/settings",
    response_model=schemas.PluginSettings,
    dependencies=[RequirePermission(SETTINGS_PERMISSION), Depends(ensure_ready)],
)
async def put_settings(
    body: schemas.PluginSettings,
    db: DBSession,
) -> schemas.PluginSettings:
    """Store settings for one printer, or the global row for printer_id 0.

    Returns the effective settings afterwards, which for a printer without a row
    of its own is the global row and not what was sent.
    """
    try:
        await settings.save_settings(db, body)
        await db.commit()
        return await settings.load_settings(db, body.printer_id)
    except SQLAlchemyError as exc:
        await db.rollback()
        raise _database_unavailable(exc) from exc


@router.get(
    "/status",
    response_model=list[schemas.PrinterStatus],
    dependencies=[Depends(require_auth)],
)
async def printer_status() -> list[schemas.PrinterStatus]:
    """Live state of every listener."""
    raise _not_implemented()


@router.get(
    "/history",
    response_model=list[schemas.PrintRecord],
    dependencies=[Depends(require_auth), Depends(ensure_ready)],
)
async def history(limit: int = 50, offset: int = 0) -> list[schemas.PrintRecord]:
    """Prints, newest first."""
    raise _not_implemented()


@router.get(
    "/thumb/{print_id}",
    dependencies=[Depends(require_auth), Depends(ensure_ready)],
)
async def thumbnail(print_id: int) -> Response:
    """Plate preview of one print.

    Served from the database because the plugin ZIP may not carry image files.
    See docs/01_Design.md section 8.1.
    """
    raise _not_implemented()


@admin_router.post(
    "/filament/{filament_row_id}/assign",
    dependencies=[RequirePermission(CONSUMPTION_PERMISSION), Depends(ensure_ready)],
)
async def assign_spool(filament_row_id: int, body: schemas.AssignSpoolRequest) -> None:
    """Assign a spool to a filament row after the fact."""
    raise _not_implemented()


@admin_router.post(
    "/filament/{filament_row_id}/correct",
    dependencies=[RequirePermission(CONSUMPTION_PERMISSION), Depends(ensure_ready)],
)
async def correct_usage(filament_row_id: int, body: schemas.CorrectUsageRequest) -> None:
    """Override the booked amount for one filament row."""
    raise _not_implemented()


@admin_router.post(
    "/print/{print_id}/spend",
    dependencies=[RequirePermission(CONSUMPTION_PERMISSION), Depends(ensure_ready)],
)
async def spend_print(print_id: int) -> None:
    """Book a print that was recorded but not deducted, for auto_spend off."""
    raise _not_implemented()
