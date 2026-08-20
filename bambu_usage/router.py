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
import contextlib
import logging
import re
from pathlib import Path

from app.api.deps import DBSession, RequirePermission, require_auth
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.exc import SQLAlchemyError

from . import __version__, filaman, models, schemas, service, settings

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

# The dictionary is never cached. It changes on exactly the occasion where a
# stale copy hurts most, a plugin update, and a page showing raw keys like
# "history.heading" for an hour afterwards looks broken in a way nobody can
# explain. It is a few kilobytes, fetched once per page load, on a LAN.
LOCALE_CACHE_CONTROL = "no-store"

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

# Correcting an amount downwards gives material back, which FilaMan books as an
# adjustment rather than a consumption. The permission follows the event.
ADJUSTMENT_PERMISSION = "spool_events:create_adjustment"

# Upper bound on one history page, so a client cannot ask for everything at once.
MAX_HISTORY_LIMIT = 200

# A stored preview is a PNG. The fallback only matters for a row written before
# the mime column was filled.
THUMBNAIL_FALLBACK_MIME = "image/png"

# A preview never changes once stored, so it may be cached for a day.
THUMBNAIL_CACHE_SECONDS = 86400

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


def _booking_failed(exc: Exception) -> HTTPException:
    """Turn a refused booking into a translatable answer.

    Everything service.py refuses is a conflict rather than a server fault: the
    print, the row or the spool is not in a state the request assumes. The
    message says which, the code is what the page translates.
    """
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={"code": "errors.bookingFailed", "message": str(exc)},
    )


def _not_found() -> HTTPException:
    """Nothing to return under this id."""
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={"code": "errors.notFound", "message": "Not found"},
    )


def _load_locale(language: str) -> str | None:
    """Return the raw JSON of one dictionary, or None if there is no such file."""
    path = LOCALES_DIR / f"{language}.json"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


@router.get("/health", response_model=schemas.HealthResponse)
async def health(db: DBSession) -> schemas.HealthResponse:
    """Liveness of the plugin.

    **Never fails on the database.** Its job is to answer whether the plugin is
    mounted at all, and an answer that disappears when the database hiccups is
    no answer. The tracking figures are read from the status table rather than
    from this process, because the listeners run in one worker out of four and
    asking the local process would say "no" three times out of four.

    ``tables_ready`` stays a fact about this worker: whether it has run its own
    one-time setup, which is the only way to see a request driven bootstrap from
    outside.
    """
    watched: list[schemas.PrinterStatus] = []
    with contextlib.suppress(SQLAlchemyError, filaman.FilaManUnavailableError):
        watched = await service.get_printer_status(db)

    return schemas.HealthResponse(
        plugin="bambu_usage",
        version=__version__,
        tracking_active=any(entry.connected for entry in watched),
        printers_watched=len(watched),
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
        headers={"Cache-Control": LOCALE_CACHE_CONTROL},
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
    dependencies=[Depends(require_auth), Depends(ensure_ready)],
)
async def printer_status(db: DBSession) -> list[schemas.PrinterStatus]:
    """Live state of every listener, as they last recorded it."""
    try:
        return await service.get_printer_status(db)
    except SQLAlchemyError as exc:
        raise _database_unavailable(exc) from exc


@router.get(
    "/history",
    response_model=list[schemas.PrintRecord],
    dependencies=[Depends(require_auth), Depends(ensure_ready)],
)
async def history(
    db: DBSession,
    limit: int = Query(default=50, ge=1, le=MAX_HISTORY_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> list[schemas.PrintRecord]:
    """Prints, newest first, with their filament breakdown."""
    try:
        return await service.get_history(db, limit=limit, offset=offset)
    except SQLAlchemyError as exc:
        raise _database_unavailable(exc) from exc


@router.get(
    "/thumb/{print_id}",
    dependencies=[Depends(require_auth), Depends(ensure_ready)],
)
async def thumbnail(print_id: int, db: DBSession) -> Response:
    """Plate preview of one print.

    Served from the database because the plugin ZIP may not carry image files.
    See docs/01_Design.md section 8.1.
    """
    try:
        found = await service.get_thumbnail(db, print_id)
    except SQLAlchemyError as exc:
        raise _database_unavailable(exc) from exc

    if found is None:
        raise _not_found()

    payload, media_type = found
    # media_type is explicit here, as it must be on every raw Response. Without
    # it this endpoint answers 404 in a way that looks like a routing bug.
    return Response(
        content=payload,
        media_type=media_type or THUMBNAIL_FALLBACK_MIME,
        headers={"Cache-Control": f"max-age={THUMBNAIL_CACHE_SECONDS}"},
    )


@admin_router.post(
    "/filament/{filament_row_id}/assign",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[RequirePermission(CONSUMPTION_PERMISSION), Depends(ensure_ready)],
)
async def assign_spool(
    filament_row_id: int,
    body: schemas.AssignSpoolRequest,
    db: DBSession,
) -> None:
    """Assign a spool to a filament row after the fact."""
    try:
        await service.assign_spool(db, filament_row_id, body.spool_id, body.spend_now)
    except service.UsageError as exc:
        raise _booking_failed(exc) from exc
    except SQLAlchemyError as exc:
        await db.rollback()
        raise _database_unavailable(exc) from exc


@admin_router.post(
    "/filament/{filament_row_id}/correct",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[RequirePermission(ADJUSTMENT_PERMISSION), Depends(ensure_ready)],
)
async def correct_usage(
    filament_row_id: int,
    body: schemas.CorrectUsageRequest,
    db: DBSession,
) -> None:
    """Override the booked amount for one filament row."""
    try:
        await service.correct_usage(db, filament_row_id, body.grams)
    except service.UsageError as exc:
        raise _booking_failed(exc) from exc
    except SQLAlchemyError as exc:
        await db.rollback()
        raise _database_unavailable(exc) from exc


@admin_router.post(
    "/print/{print_id}/spend",
    response_model=dict[int, float],
    dependencies=[RequirePermission(CONSUMPTION_PERMISSION), Depends(ensure_ready)],
)
async def spend_print(print_id: int, db: DBSession) -> dict[int, float]:
    """Book a print that was recorded but not deducted, for auto_spend off.

    Returns the grams booked, keyed by spool id.
    """
    try:
        return await service.spend_print(db, print_id)
    except service.UsageError as exc:
        raise _booking_failed(exc) from exc
    except SQLAlchemyError as exc:
        await db.rollback()
        raise _database_unavailable(exc) from exc
