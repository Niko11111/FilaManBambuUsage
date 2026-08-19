"""Reading a Bambu 3MF archive.

A 3MF is a ZIP. Three entries matter:

  Metadata/slice_info.config   XML, per filament the estimated used_g / used_m,
                               the material and the tray_info_idx
  Metadata/plate_<N>.png       the plate preview shown in the history
  Metadata/plate_<N>.gcode     only needed to recover the filament order for
                               local prints, which is stage 3

This module is deliberately free of MQTT and HTTP so it can be tested against a
3MF on disk. See docs/03_Bambu_Data_Sources.md.

Downloads go through bambulabs_api rather than pycurl: it is already a
dependency of the Bambu Lab driver plugin and needs no compiler in the
container.

May import: nothing from this plugin. Must not import fastapi or sqlalchemy
either, so it stays callable against a file on disk. Enforced by
tools/check_architecture.py.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Only the file name of a remote path is ever used. Bambu's FTP happily accepts
# a traversal, so path components are dropped before the fetch.
FTP_USER = "bblp"

SLICE_INFO_PATH = "Metadata/slice_info.config"
PLATE_IMAGE_TEMPLATE = "Metadata/plate_{plate_id}.png"
PLATE_GCODE_TEMPLATE = "Metadata/plate_{plate_id}.gcode"

DEFAULT_TIMEOUT_SECONDS = 30

# The metadata key under <plate> that carries the plate number.
PLATE_INDEX_KEY = "index"


@dataclass
class FilamentInfo:
    """One filament entry out of slice_info.config.

    ``filament_id`` is 1-based, as the slicer writes it, while ams_mapping is
    0-based. The conversion lives in service.py and is a classic off-by-one.
    """

    filament_id: int
    material: str | None = None
    color_hex: str | None = None
    tray_info_idx: str | None = None
    used_g: float | None = None
    used_m: float | None = None


@dataclass
class PrintMetadata:
    """Everything this plugin needs out of one 3MF."""

    plate_id: int | None = None
    filaments: list[FilamentInfo] = field(default_factory=list)
    thumbnail: bytes | None = None
    thumbnail_mime: str | None = None
    # Filament order parsed from the plate gcode. Stage 3 only, empty until then.
    filament_order: dict[int, int] = field(default_factory=dict)


async def download_from_printer(
    host: str,
    access_code: str,
    remote_path: str,
    dest: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Fetch a 3MF off the printer over FTPS into *dest*.

    Only the file name of *remote_path* is used; any directory part is
    discarded. Raises on failure, the caller records the print as no_3mf rather
    than dropping it.
    """
    raise NotImplementedError


async def download_from_url(url: str, dest: Path, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
    """Fetch a 3MF from the URL a cloud print reports in print.url."""
    raise NotImplementedError


def parse(archive: Path, want_gcode_order: bool = False) -> PrintMetadata:
    """Read *archive* and return what the tracker needs.

    Set *want_gcode_order* only for local prints. The plate gcode is large and
    is streamed line by line, never read into memory as a whole.
    """
    raise NotImplementedError


def parse_slice_info(xml_bytes: bytes) -> tuple[int | None, list[FilamentInfo]]:
    """Parse slice_info.config into the plate id and its filaments.

    Only the first plate is read. A Bambu print job covers exactly one plate,
    and the plate id is what names the preview image and the gcode entry.

    Attributes are tolerated as missing rather than assumed: the file comes off
    a printer, and a firmware update may add or drop one. A filament without a
    usable id is skipped, because it cannot be mapped to a tray anyway.
    """
    root = ET.fromstring(xml_bytes)

    plate = root.find(".//plate")
    if plate is None:
        return None, []

    plate_id = None
    for metadata in plate.findall("metadata"):
        if metadata.get("key") == PLATE_INDEX_KEY:
            plate_id = _to_int(metadata.get("value"))
            break

    filaments = []
    for element in plate.findall("filament"):
        filament_id = _to_int(element.get("id"))
        if filament_id is None:
            continue
        filaments.append(
            FilamentInfo(
                filament_id=filament_id,
                material=element.get("type"),
                color_hex=element.get("color"),
                tray_info_idx=element.get("tray_info_idx"),
                used_g=_to_float(element.get("used_g")),
                used_m=_to_float(element.get("used_m")),
            )
        )

    return plate_id, filaments


def _to_int(value: str | None) -> int | None:
    """Parse an attribute as an int, returning None instead of raising."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_float(value: str | None) -> float | None:
    """Parse an attribute as a float, returning None instead of raising."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_filament_order(gcode_lines) -> dict[int, int]:
    """Count how often each filament is selected, in order of appearance.

    Stage 3 only. Feeds the reconstruction of ams_mapping for local prints.
    """
    raise NotImplementedError
