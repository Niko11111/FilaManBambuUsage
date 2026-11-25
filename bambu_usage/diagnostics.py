"""The diagnostics a user can send back: what of a report is kept, and the text.

Two halves of one job. ``snapshot_of`` decides what of the printer's report is
kept with a print when it starts: the fields that say where the 3MF came from
and which tray each filament was mapped to, and nothing that identifies the
printer or opens it, so no access code, serial number or address. The
``format_*`` functions turn that, with the history rows, into the plain text the
page offers to copy or download.

The text is English on purpose, whatever language the page speaks: it is read by
whoever answers the question, and two reports have to be comparable line by
line. Times are UTC for the same reason.

What a report can carry is documented in docs/03_Bambu_Data_Sources.md, and the
text itself in docs/01_Design.md section 8.4.

May import: nothing, beyond the standard library. Enforced by
tools/check_architecture.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

# The fields of the print section kept verbatim. Each one has helped, or would
# have helped, to explain a print that went wrong: where the 3MF came from, how
# the slicer mapped its filaments, which tray was feeding.
KEPT_PRINT_FIELDS = (
    "command",
    "print_type",
    "gcode_file",
    "subtask_name",
    "ams_mapping",
    "ams_mapping2",
)

# Bounds on what is kept, so a report a firmware fills with something unexpected
# cannot grow a history row without limit. A real ams_mapping has at most a few
# dozen entries and a real file name stays well under the string bound.
MAX_STRING_LENGTH = 300
MAX_LIST_ITEMS = 64
MAX_DICT_KEYS = 16
MAX_NESTING = 3

HTTP_SCHEMES = ("http", "https")

# Labels are padded to this width, so the values line up in a monospace font.
LABEL_WIDTH = 13
TIME_FORMAT = "%Y-%m-%d %H:%M UTC"
HEADING = "Bambu Usage Tracking diagnostics"


@dataclass(frozen=True)
class Versions:
    """What is installed. None where it could not be read."""

    plugin: str
    filaman: str | None = None
    driver: str | None = None


@dataclass(frozen=True)
class PrinterFacts:
    """One watched printer, as its listener last recorded it."""

    printer_id: int
    name: str | None
    connected: bool
    tracking_enabled: bool
    last_error: str | None = None


@dataclass(frozen=True)
class FilamentFacts:
    """One filament of a print, as the plugin resolved and booked it."""

    filament_id: int
    material: str | None
    color_hex: str | None
    estimated_grams: float | None
    slot_index: str | None
    spool_id: int | None
    spool_source: str | None = None


@dataclass(frozen=True)
class PrintFacts:
    """One print with everything the text says about it."""

    print_id: int
    file_name: str
    printer_id: int
    printer_name: str | None
    status: str
    spent: bool
    started_at: datetime | None
    print_type: str
    error: str | None = None
    snapshot: dict[str, Any] | None = None
    filaments: list[FilamentFacts] = field(default_factory=list)


# What is kept with a print.


def snapshot_of(state: dict) -> dict[str, Any]:
    """The part of a merged report worth keeping with a print that starts."""
    section = state.get("print") if isinstance(state, dict) else None
    if not isinstance(section, dict):
        return {}

    kept: dict[str, Any] = {}
    for name in KEPT_PRINT_FIELDS:
        if name in section:
            kept[name] = _plain(section[name])

    url = section.get("url")
    if isinstance(url, str) and url:
        kept["url"] = without_secrets(url)

    ams = section.get("ams")
    if isinstance(ams, dict):
        kept["tray_now"] = _plain(ams.get("tray_now"))
        kept["ams_units"] = _ams_units(ams.get("ams"))
    kept["external"] = _external_ids(section)
    return kept


def without_secrets(url: str) -> str:
    """*url* without what could open it for somebody else.

    A cloud print's URL is signed: its query string is the key to the file.
    Query, fragment and any user name or password are dropped; what is left
    still says where the file was.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return "(unreadable)"
    if parts.scheme.lower() in HTTP_SCHEMES:
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        cleaned = f"{parts.scheme}://{host}{parts.path}"
    else:
        cleaned = f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}{parts.path}"
    return _cut(cleaned)


def _ams_units(units: Any) -> list[dict[str, Any]]:
    """Every AMS unit with its id, type info and the ids of its trays."""
    if not isinstance(units, list):
        return []
    result = []
    for unit in units[:MAX_LIST_ITEMS]:
        if not isinstance(unit, dict):
            continue
        trays = unit.get("tray")
        if not isinstance(trays, list):
            trays = []
        tray_ids = [
            _plain(tray.get("id")) for tray in trays[:MAX_LIST_ITEMS] if isinstance(tray, dict)
        ]
        result.append({
            "id": _plain(unit.get("id")),
            "info": _plain(unit.get("info")),
            "trays": tray_ids,
        })
    return result


def _external_ids(section: dict) -> list[Any]:
    """The ids of the external inputs: vir_slot on newer printers, vt_tray before."""
    inputs = section.get("vir_slot")
    if not isinstance(inputs, list) or not inputs:
        single = section.get("vt_tray")
        inputs = [single] if isinstance(single, dict) else []
    return [_plain(item.get("id")) for item in inputs[:MAX_LIST_ITEMS] if isinstance(item, dict)]


def _plain(value: Any, depth: int = 0) -> Any:
    """*value* reduced to bounded JSON: numbers, short strings, small lists."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _cut(value)
    if depth >= MAX_NESTING:
        return None
    if isinstance(value, list):
        return [_plain(item, depth + 1) for item in value[:MAX_LIST_ITEMS]]
    if isinstance(value, dict):
        items = list(value.items())[:MAX_DICT_KEYS]
        return {str(key)[:MAX_STRING_LENGTH]: _plain(item, depth + 1) for key, item in items}
    return None


def _cut(text: str) -> str:
    """*text* without control characters and within the string bound."""
    clean = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    return clean[:MAX_STRING_LENGTH]


# The text.


def format_print(versions: Versions, facts: PrintFacts, now: datetime) -> str:
    """The text about one print, for the window in its history row."""
    lines = _heading(versions, now)
    lines.append("")
    lines.append(f"Print #{facts.print_id} {_quoted(facts.file_name)} on {_printer(facts)}")
    lines.extend(_print_lines(facts, "  "))
    lines.append("")
    lines.extend(_layout_lines(facts.snapshot, ""))
    return "\n".join(lines)


def format_overview(
    versions: Versions,
    printers: list[PrinterFacts],
    recent: list[PrintFacts],
    now: datetime,
) -> str:
    """The text about every printer and the latest prints, for the help card."""
    lines = _heading(versions, now)
    for printer in printers:
        lines.append("")
        lines.extend(_printer_lines(printer, _latest_snapshot(printer.printer_id, recent)))
    lines.append("")
    lines.append(f"Last {len(recent)} print starts" if recent else "No prints recorded yet")
    for facts in recent:
        lines.append(f"  #{facts.print_id} {_quoted(facts.file_name)} on {_printer(facts)}")
        lines.extend(_print_lines(facts, "    "))
    return "\n".join(lines)


def _heading(versions: Versions, now: datetime) -> list[str]:
    installed = f"Plugin {versions.plugin}, FilaMan {versions.filaman or 'unknown'}"
    installed += f", Bambu Lab driver {versions.driver or 'unknown'}"
    return [HEADING, installed, f"Generated {_time(now)}"]


def _printer_lines(printer: PrinterFacts, snapshot: dict[str, Any] | None) -> list[str]:
    state = "connected" if printer.connected else "not connected"
    tracking = "tracking on" if printer.tracking_enabled else "tracking off"
    lines = [
        f"Printer {printer.name or 'unnamed'} (id {printer.printer_id})",
        f"  {state}, {tracking}",
    ]
    lines.extend(_layout_lines(snapshot, "  "))
    lines.append(_row("  ", "Last error", printer.last_error or "none"))
    return lines


def _print_lines(facts: PrintFacts, indent: str) -> list[str]:
    booked = "booked" if facts.spent else "not booked"
    lines = [
        _row(indent, "Status", f"{facts.status}, {booked}"),
        _row(indent, "Started", f"{_time(facts.started_at)}, {facts.print_type}"),
    ]
    snapshot = facts.snapshot
    if snapshot is None:
        lines.append(_row(indent, "Report", "not kept, the print started before 1.0.2"))
    else:
        lines.append(_row(indent, "3MF from", snapshot.get("url") or "-"))
        for name in ("gcode_file", "ams_mapping", "ams_mapping2", "tray_now"):
            lines.append(_row(indent, name, _compact(snapshot.get(name))))
    for filament in facts.filaments:
        lines.append(_row(indent, f"Filament {filament.filament_id}", _filament(filament)))
    lines.append(_row(indent, "Error", facts.error or "none"))
    return lines


def _layout_lines(snapshot: dict[str, Any] | None, indent: str) -> list[str]:
    """The AMS units and external inputs the printer reported at a print start."""
    if not snapshot:
        return [_row(indent, "AMS units", "unknown, no print start with a kept report")]
    units = snapshot.get("ams_units") or []
    described = ", ".join(
        f"{unit.get('id')} (info {unit.get('info')}, trays {_compact(unit.get('trays'))})"
        for unit in units
    )
    return [
        _row(indent, "AMS units", described or "none"),
        _row(indent, "External", _compact(snapshot.get("external"))),
    ]


def _latest_snapshot(printer_id: int, recent: list[PrintFacts]) -> dict[str, Any] | None:
    for facts in recent:
        if facts.printer_id == printer_id and facts.snapshot:
            return facts.snapshot
    return None


def _filament(filament: FilamentFacts) -> str:
    grams = "-" if filament.estimated_grams is None else f"{filament.estimated_grams:.2f} g"
    spool = "none" if filament.spool_id is None else f"#{filament.spool_id}"
    source = f" ({filament.spool_source})" if filament.spool_source else ""
    return (
        f"{filament.material or '?'} {filament.color_hex or '?'} {grams}"
        f" -> slot {filament.slot_index or 'none'} -> spool {spool}{source}"
    )


def _printer(facts: PrintFacts) -> str:
    return f"{facts.printer_name or 'printer'} (id {facts.printer_id})"


def _row(indent: str, label: str, value: str) -> str:
    return f"{indent}{label.ljust(LABEL_WIDTH)}{value}"


def _compact(value: Any) -> str:
    """JSON-like and without spaces, which is how it looks in a report."""
    if value is None:
        return "-"
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _quoted(name: str) -> str:
    return f'"{name}"'


def _time(moment: datetime | None) -> str:
    if moment is None:
        return "-"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(TIME_FORMAT)
