"""Persistent plugin settings.

Follows the pattern of FilaMan's own spoolmanapi plugin: settings live in a
dedicated table rather than in the plugin directory, so a ZIP update does not
wipe them.

Resolution order for a printer: the row for that printer if it exists,
otherwise the global row (printer_id 0), otherwise the dataclass defaults.

May import: models, schemas. Must not import tracker, router or service.
Enforced by tools/check_architecture.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # imported for annotations only
    from .schemas import PluginSettings


async def load_settings(printer_id: int = 0) -> PluginSettings:
    """Return the effective settings for *printer_id*.

    Falls back to the global row and then to the defaults. Cached in module
    state after the first read; ``invalidate_cache`` drops it.
    """
    raise NotImplementedError


async def save_settings(settings: PluginSettings) -> None:
    """Write *settings* and refresh the cache."""
    raise NotImplementedError


async def list_settings() -> list[PluginSettings]:
    """Return the global row plus every per-printer override."""
    raise NotImplementedError


def invalidate_cache() -> None:
    """Drop the cached settings, forcing the next read to hit the database."""
    raise NotImplementedError
