"""HTTP surface of the plugin.

FilaMan mounts this router under the manifest's mount_prefix, "/bambu-usage",
when the application starts. The page itself is served by FilaMan's catch-all at
/plugin-page/bambu-usage and resolved per request, so it appears right after
installation while a newly mounted router needs a restart.

May import: schemas, service, settings. Must not import tracker (no printer
access from an HTTP handler) or bambulabs_api. Enforced by
tools/check_architecture.py.

Milestone 1 implements the health and i18n endpoints only. Health has no auth
dependency on purpose: it is what proves the router mounted at all, and it
exposes nothing. Everything else is a stub. The auth wiring for the
administrative routes follows FilaMan's spoolmanapi plugin, which takes
DBSession and RequirePermission from app.api.deps, and lands in stage 2 with the
first real implementation.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, status

from . import __version__, schemas

router = APIRouter(tags=["bambu-usage"])

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


def _not_implemented() -> HTTPException:
    """Stub response carrying a translatable code plus an English fallback.

    The page maps the code to the user's language; the message keeps a raw curl
    readable. See docs/01_Design.md section 9.
    """
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
    answer to the question whether the plugin is mounted.
    """
    return schemas.HealthResponse(
        plugin="bambu_usage",
        version=__version__,
        tracking_active=False,
        printers_watched=0,
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


@router.get("/status", response_model=list[schemas.PrinterStatus])
async def printer_status() -> list[schemas.PrinterStatus]:
    """Live state of every listener."""
    raise _not_implemented()


@router.get("/history", response_model=list[schemas.PrintRecord])
async def history(limit: int = 50, offset: int = 0) -> list[schemas.PrintRecord]:
    """Prints, newest first."""
    raise _not_implemented()


@router.get("/thumb/{print_id}")
async def thumbnail(print_id: int) -> Response:
    """Plate preview of one print.

    Served from the database because the plugin ZIP may not carry image files.
    See docs/01_Design.md section 8.1.
    """
    raise _not_implemented()


@router.post("/filament/{filament_row_id}/assign")
async def assign_spool(filament_row_id: int, body: schemas.AssignSpoolRequest) -> None:
    """Assign a spool to a filament row after the fact."""
    raise _not_implemented()


@router.post("/filament/{filament_row_id}/correct")
async def correct_usage(filament_row_id: int, body: schemas.CorrectUsageRequest) -> None:
    """Override the booked amount for one filament row."""
    raise _not_implemented()


@router.post("/print/{print_id}/spend")
async def spend_print(print_id: int) -> None:
    """Book a print that was recorded but not deducted, for auto_spend off."""
    raise _not_implemented()


@router.get("/settings", response_model=list[schemas.PluginSettings])
async def get_settings() -> list[schemas.PluginSettings]:
    """Global defaults plus every per-printer override."""
    raise _not_implemented()


@router.put("/settings", response_model=schemas.PluginSettings)
async def put_settings(body: schemas.PluginSettings) -> schemas.PluginSettings:
    """Store settings for one printer, or the global row for printer_id 0."""
    raise _not_implemented()
