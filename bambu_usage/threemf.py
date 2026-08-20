"""Reading a Bambu 3MF archive.

A 3MF is a ZIP. Three entries matter:

  Metadata/slice_info.config   XML, per filament the estimated used_g / used_m,
                               the material and the tray_info_idx
  Metadata/plate_<N>.png       the plate preview shown in the history
  Metadata/plate_<N>.gcode     only needed to recover the filament order for
                               local prints, which is stage 3

This module is deliberately free of MQTT and HTTP frameworks so it can be tested
against a 3MF on disk. See docs/03_Bambu_Data_Sources.md.

``parse`` is forgiving on purpose. A 3MF comes off a printer and a firmware
update may add or drop an entry, so a missing or unreadable part becomes an
empty field rather than an exception. Only an archive that cannot be opened at
all raises, because then there is nothing to record. The caller decides what a
print without filaments means, see docs/01_Design.md section 7.

``bambulabs_api`` is imported inside the function that needs it. It is a runtime
dependency declared in plugin.json, present wherever FilaMan's Bambu Lab driver
is installed, but absent from the test environment, and this module has to stay
importable there.

May import: nothing from this plugin. Must not import fastapi or sqlalchemy
either, so it stays callable against a file on disk. Enforced by
tools/check_architecture.py.
"""

from __future__ import annotations

import asyncio
import ftplib
import http.client
import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Only the file name of a remote path is ever used. Bambu's FTP happily accepts
# a traversal, so path components are dropped before the fetch.
FTP_USER = "bblp"
FTP_PORT = 990

SLICE_INFO_PATH = "Metadata/slice_info.config"
PLATE_IMAGE_TEMPLATE = "Metadata/plate_{plate_id}.png"
PLATE_GCODE_TEMPLATE = "Metadata/plate_{plate_id}.gcode"

# A 3MF also carries plate_1_small.png, plate_no_light_1.png and similar. Only
# the exact plate image is the preview, hence the anchored pattern.
PLATE_IMAGE_PATTERN = re.compile(r"^Metadata/plate_(\d+)\.png$")

THUMBNAIL_MIME = "image/png"

DEFAULT_TIMEOUT_SECONDS = 30
TRANSFER_BLOCK_BYTES = 512 * 1024

# The preview ends up as a BLOB in the database, so it needs an upper bound. A
# real plate preview is a few hundred kilobytes; anything past this is not a
# preview any more and is dropped rather than stored.
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024

# slice_info.config is a few kilobytes of XML. The bound only exists so a
# malformed archive cannot be expanded into memory.
MAX_SLICE_INFO_BYTES = 4 * 1024 * 1024

# A 3MF with its plate gcode runs to tens of megabytes. This is a guard against
# a runaway download, not a tuning knob.
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024

# The metadata key under <plate> that carries the plate number.
PLATE_INDEX_KEY = "index"


class ThreeMFError(RuntimeError):
    """A 3MF could not be fetched or opened at all."""


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
    discarded. Raises ThreeMFError on failure, and the caller records the print
    as no_3mf rather than dropping it.
    """
    file_name = Path(remote_path).name
    if not file_name:
        raise ThreeMFError(f"remote path '{remote_path}' names no file")

    await asyncio.to_thread(_fetch_over_ftps, host, access_code, file_name, dest, timeout)


def _fetch_over_ftps(
    host: str,
    access_code: str,
    file_name: str,
    dest: Path,
    timeout: float,
) -> None:
    """Blocking FTPS transfer, meant to be run in a worker thread.

    This drives the connection of bambulabs_api's PrinterFTPClient instead of
    calling its ``download_file()``, for two reasons that both matter here: that
    method collects the whole archive in a BytesIO, and its ``connect_and_run``
    decorator catches every exception, logs it and returns None, which would
    turn a refused connection into an empty file and a print booked at zero.
    Streaming to disk with our own error handling costs a handful of lines and
    keeps both the memory and the failure visible.

    The library also leaves the socket timeout unset, which means "block
    forever". Setting it is the one thing that keeps a silent printer from
    parking a thread for good.
    """
    from bambulabs_api import PrinterFTPClient

    client = PrinterFTPClient(host, access_code)
    connection = client.ftps
    connection.timeout = timeout

    try:
        connection.connect(host=host, port=FTP_PORT)
        connection.login(FTP_USER, access_code)
        connection.prot_p()
        with dest.open("wb") as handle:
            connection.retrbinary(
                f"RETR {file_name}", handle.write, blocksize=TRANSFER_BLOCK_BYTES
            )
    except (OSError, EOFError, ftplib.Error) as exc:
        raise ThreeMFError(f"could not fetch '{file_name}' from {host}: {exc}") from exc
    finally:
        with suppress(OSError, EOFError, ftplib.Error):
            connection.close()


async def download_from_url(
    url: str,
    dest: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Fetch a 3MF from the URL a cloud print reports in print.url."""
    await asyncio.to_thread(_fetch_over_http, url, dest, timeout)


def _fetch_over_http(url: str, dest: Path, timeout: float) -> None:
    """Blocking HTTP transfer, meant to be run in a worker thread.

    The URL arrives in an MQTT message, so the scheme is checked before it is
    opened: urllib would happily read a local file through file:// otherwise.
    """
    if not url.lower().startswith(("http://", "https://")):
        raise ThreeMFError(f"refusing to fetch a 3MF from '{url[:40]}', not an HTTP URL")

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            written = 0
            with dest.open("wb") as handle:
                while True:
                    chunk = response.read(TRANSFER_BLOCK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_ARCHIVE_BYTES:
                        raise ThreeMFError(
                            f"3MF at '{url[:40]}' is larger than {MAX_ARCHIVE_BYTES} bytes"
                        )
                    handle.write(chunk)
    except (OSError, http.client.HTTPException) as exc:
        raise ThreeMFError(f"could not fetch a 3MF from '{url[:40]}': {exc}") from exc


def parse(archive: Path) -> PrintMetadata:
    """Read *archive* and return what the tracker needs.

    Raises ThreeMFError only when the file is not a readable ZIP. Everything
    inside it is optional: a missing entry becomes an empty field, so a print
    still reaches the history even when its estimate does not.
    """
    try:
        with zipfile.ZipFile(archive) as bundle:
            return _read_metadata(bundle)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ThreeMFError(f"'{archive.name}' is not a readable 3MF: {exc}") from exc


def _read_metadata(bundle: zipfile.ZipFile) -> PrintMetadata:
    """Pull plate, filaments and preview out of an open archive."""
    plate_id, filaments = _read_slice_info(bundle)
    thumbnail = _read_thumbnail(bundle, plate_id)

    return PrintMetadata(
        plate_id=plate_id,
        filaments=filaments,
        thumbnail=thumbnail,
        thumbnail_mime=THUMBNAIL_MIME if thumbnail else None,
    )


def _read_slice_info(bundle: zipfile.ZipFile) -> tuple[int | None, list[FilamentInfo]]:
    """Read slice_info.config, tolerating its absence and malformed XML."""
    raw = _read_entry(bundle, SLICE_INFO_PATH, MAX_SLICE_INFO_BYTES)
    if raw is None:
        logger.warning("3MF carries no %s, no consumption can be derived", SLICE_INFO_PATH)
        return None, []

    try:
        return parse_slice_info(raw)
    except ET.ParseError as exc:
        logger.warning("%s is not valid XML: %s", SLICE_INFO_PATH, exc)
        return None, []


def _read_thumbnail(bundle: zipfile.ZipFile, plate_id: int | None) -> bytes | None:
    """Return the plate preview, or None when there is none worth storing.

    The plate named in slice_info.config wins. Only if that is missing does the
    first plate image in the archive stand in, which is what keeps a print
    without a readable slice_info from losing its picture as well.
    """
    candidates = []
    if plate_id is not None:
        candidates.append(PLATE_IMAGE_TEMPLATE.format(plate_id=plate_id))
    candidates.extend(_plate_image_names(bundle))

    for name in candidates:
        data = _read_entry(bundle, name, MAX_THUMBNAIL_BYTES)
        if data:
            return data

    return None


def _plate_image_names(bundle: zipfile.ZipFile) -> list[str]:
    """Every Metadata/plate_<N>.png in the archive, in plate order."""
    matches = []
    for name in bundle.namelist():
        found = PLATE_IMAGE_PATTERN.match(name)
        if found:
            matches.append((int(found.group(1)), name))

    return [name for _, name in sorted(matches)]


def _read_entry(bundle: zipfile.ZipFile, name: str, limit: int) -> bytes | None:
    """Read one archive entry, or None when it is absent or implausibly large.

    The size is taken from the archive index before anything is read, so a
    declared gigabyte never becomes an allocated one.
    """
    try:
        info = bundle.getinfo(name)
    except KeyError:
        return None

    if info.file_size > limit:
        logger.warning("skipping %s, %d bytes exceeds the limit of %d", name, info.file_size, limit)
        return None

    try:
        return bundle.read(name)
    except (OSError, zipfile.BadZipFile) as exc:
        logger.warning("could not read %s out of the 3MF: %s", name, exc)
        return None


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
