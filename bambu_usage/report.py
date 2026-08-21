"""What one report from a Bambu printer means.

A printer sends partial updates about once a second. This module carries the
state along, decides whether a print just started or just ended, and reads out
of the merged state what is needed to record it. Nothing here connects to
anything: tracker.py owns the connection and acts on what is decided here.

Keeping the two apart is what makes this half testable at all. Every payload
shape it reasons about is documented in docs/03_Bambu_Data_Sources.md.

May import: nothing, beyond the standard library. Enforced by
tools/check_architecture.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATE_IDLE = "IDLE"
STATE_PREPARE = "PREPARE"
STATE_RUNNING = "RUNNING"
STATE_PAUSE = "PAUSE"
STATE_FINISH = "FINISH"
STATE_FAILED = "FAILED"

# A print is under way in these, and over in those.
ACTIVE_STATES = frozenset({STATE_PREPARE, STATE_RUNNING, STATE_PAUSE})
FINAL_STATES = frozenset({STATE_FINISH, STATE_FAILED})

# A network or cloud print announces itself with this command and carries a
# ready-made ams_mapping. Local prints do not, which is why they are stage 3.
COMMAND_PROJECT_FILE = "project_file"

PRINT_TYPE_CLOUD = "cloud"
PRINT_TYPE_LOCAL = "local"

TRANSITION_STARTED = "started"
TRANSITION_ENDED = "ended"

# Where a report keeps the fields this plugin reads.
PRINT_SECTION = "print"

# Where the AMS state sits inside a report, and what one tray calls its tag.
AMS_SECTION = "ams"
TRAY_LIST = "tray"
TRAY_UUID = "tray_uuid"
EXTERNAL_TRAY_SECTION = "vt_tray"

# The external holder has no unit of its own. These two say the same thing as
# rules.EXTERNAL_SLOT_INDEX, and they are repeated here because this module may
# not import rules. tests/test_report.py pins the two together.
EXTERNAL_AMS_ID = 255
EXTERNAL_TRAY_ID = 254

# Which tray the printer is drawing from, counted globally across AMS units.
# 255 is the external holder, which rules.tray_to_slot_index expects as a
# negative number instead.
TRAY_NOW = "tray_now"
EXTERNAL_TRAY_NOW = 255

# What the printer says went wrong. Absent or zero after a print somebody
# stopped; a code after a fault.
ERROR_FIELDS = ("print_error", "mc_print_error_code")

# An empty tray, and a spool without a readable tag, report this. Looking it up
# would hang every empty chamber on whichever spool happens to carry it.
EMPTY_TRAY_UUID = "0" * 32


@dataclass(frozen=True)
class Transition:
    """What changed between two reports, as far as this plugin cares."""

    kind: str
    gcode_state: str | None = None


@dataclass(frozen=True)
class TrayTag:
    """The RFID tag of one tray, as the printer reports it.

    Built at the boundary like everything else here, so no raw report dict
    travels deeper. The ids stay numbers; turning them into a slot_index is
    rules.slot_index's job, and this module may not import it.
    """

    ams_id: int
    tray_id: int
    uuid: str


@dataclass(frozen=True)
class PrintJob:
    """What a report says about the job that is starting.

    Built at the boundary so no raw report dict travels deeper. See
    docs/03_Bambu_Data_Sources.md for where each field comes from.
    """

    subtask_id: str | None
    file_name: str
    print_type: str
    ams_mapping: list[Any]
    url: str | None
    remote_path: str | None


def merge_report(previous: dict, update: dict) -> dict:
    """Merge one report into the state carried so far.

    The printer sends partial updates: a message with only ``gcode_state`` in it
    does not mean everything else is gone. Merging one level deep is what the
    protocol implies, and it matches what bambulabs_api does internally.

    Returns a new dict rather than mutating, because detecting a transition
    means comparing the state before against the state after.
    """
    merged = dict(previous)

    for key, value in update.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = {**current, **value}
        else:
            merged[key] = value

    return merged


def gcode_state_of(state: dict) -> str | None:
    """The printer's own state name, or None if it never reported one."""
    value = state.get(PRINT_SECTION, {}).get("gcode_state")
    return str(value) if value is not None else None


def subtask_of(state: dict) -> str | None:
    """The job identifier, or None if the printer reports none."""
    value = state.get(PRINT_SECTION, {}).get("subtask_id")
    return str(value) if value not in (None, "") else None


def detect_transition(previous: dict, current: dict) -> Transition | None:
    """Decide whether a print just started or just ended.

    Two signals, and the second one matters more than it looks: a printer can go
    from one job straight into the next without ever leaving RUNNING, and only
    the changed subtask id gives that away.

    A first report that already shows an active state is a start as well. That
    is the plugin attaching in the middle of a print, and the caller is the one
    that decides such a print can never be booked.
    """
    before = gcode_state_of(previous)
    after = gcode_state_of(current)

    if after in ACTIVE_STATES and before not in ACTIVE_STATES:
        return Transition(TRANSITION_STARTED)

    if after in ACTIVE_STATES and subtask_of(previous) != subtask_of(current):
        return Transition(TRANSITION_STARTED)

    if after in FINAL_STATES and before in ACTIVE_STATES:
        return Transition(TRANSITION_ENDED, gcode_state=after)

    return None


def active_tray(state: dict) -> int | None:
    """The tray the printer is drawing from, as a global tray number.

    The external holder comes back as -1, because that is how
    rules.tray_to_slot_index wants it and 255 would otherwise be worked out as
    tray 3 of AMS 63.

    None where the printer says nothing, which is the normal state between
    prints and must not be mistaken for tray zero.
    """
    section = state.get(PRINT_SECTION, {})
    if not isinstance(section, dict):
        return None

    ams = section.get(AMS_SECTION)
    if not isinstance(ams, dict):
        return None

    tray = _to_int(ams.get(TRAY_NOW))
    if tray is None:
        return None
    return -1 if tray == EXTERNAL_TRAY_NOW else tray


def error_code(state: dict) -> int | None:
    """What the printer reported as the reason a print ended.

    None where it reported nothing and where it reported zero: both mean there
    was no fault, and a print without a fault is one somebody stopped.

    **This reading is an assumption**, taken from how the Bambu reports are
    commonly understood, and it was not verified against this printer. The code
    itself is stored with the print, so the first real fault shows whether the
    rule stands the right way round.
    """
    section = state.get(PRINT_SECTION, {})
    if not isinstance(section, dict):
        return None

    for field in ERROR_FIELDS:
        code = _to_int(section.get(field))
        if code:
            return code
    return None


def tray_tags(state: dict) -> list[TrayTag]:
    """Every tray the printer says holds a spool with a readable tag.

    This is what FilaMan's Bambu Lab driver drops on the floor: it keeps type,
    colour and tray_info_idx of a tray, but not the uuid, so a spool carrying
    that uuid can never be matched to the tray it sits in. Reading it here is
    what lets a print resolve its spools without anybody assigning them.

    Nothing is trusted to exist or to be a number. The report comes off a
    printer, and a firmware update may rename or drop any of it.
    """
    section = state.get(PRINT_SECTION, {})
    if not isinstance(section, dict):
        return []

    tags = []
    for unit in _units(section):
        ams_id = _to_int(unit.get("id"))
        if ams_id is None:
            continue
        for tray in _trays(unit):
            tag = _tag_of(tray)
            tray_id = _to_int(tray.get("id"))
            if tag is not None and tray_id is not None:
                tags.append(TrayTag(ams_id=ams_id, tray_id=tray_id, uuid=tag))

    external = section.get(EXTERNAL_TRAY_SECTION)
    if isinstance(external, dict):
        tag = _tag_of(external)
        if tag is not None:
            tags.append(TrayTag(ams_id=EXTERNAL_AMS_ID, tray_id=EXTERNAL_TRAY_ID, uuid=tag))

    return tags


def _units(section: dict) -> list[dict]:
    """The AMS units of a report. The key nests twice, and may be missing."""
    ams = section.get(AMS_SECTION)
    if not isinstance(ams, dict):
        return []

    units = ams.get(AMS_SECTION)
    return [unit for unit in units if isinstance(unit, dict)] if isinstance(units, list) else []


def _trays(unit: dict) -> list[dict]:
    trays = unit.get(TRAY_LIST)
    return [tray for tray in trays if isinstance(tray, dict)] if isinstance(trays, list) else []


def _tag_of(tray: dict) -> str | None:
    """The uuid of one tray, or None where there is nothing worth looking up."""
    value = tray.get(TRAY_UUID)
    if not isinstance(value, str):
        return None

    uuid = value.strip()
    if not uuid or uuid == EMPTY_TRAY_UUID:
        return None
    return uuid


def _to_int(value: Any) -> int | None:
    """Tray and unit ids arrive as strings. Anything unusable becomes None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def describe_job(state: dict) -> PrintJob:
    """Read out of a merged report what is needed to record the print.

    Nothing here is trusted to exist. A firmware update may drop a field, and a
    print with a missing file name still belongs in the history.
    """
    section = state.get(PRINT_SECTION, {})

    url = section.get("url") or None
    remote_path = section.get("gcode_file") or None

    name = section.get("subtask_name") or (Path(remote_path).name if remote_path else None)

    reported_type = section.get("print_type")
    if reported_type:
        print_type = str(reported_type)
    else:
        print_type = PRINT_TYPE_CLOUD if url else PRINT_TYPE_LOCAL

    mapping = section.get("ams_mapping")
    if not isinstance(mapping, list):
        mapping = []

    return PrintJob(
        subtask_id=subtask_of(state),
        file_name=str(name) if name else "unknown",
        print_type=print_type,
        ams_mapping=mapping,
        url=str(url) if url else None,
        remote_path=str(remote_path) if remote_path else None,
    )


def progress_of(state: dict) -> int | None:
    """Percent complete, for the status line. None when it is not a number."""
    return _as_int(state.get(PRINT_SECTION, {}).get("mc_percent"))


def completed_fraction(state: dict) -> float | None:
    """How far the print got, as a share between 0 and 1, or None if unknown.

    Layers come first, because a layer is a better proxy for material than time
    is, and ``mc_percent`` on a Bambu is progress in time. Both are still
    approximations: a dense bottom layer weighs more than a sparse one in the
    middle. The exact answer needs the cumulative extrusion per layer out of the
    plate gcode, which is a later stage; see docs/01_Design.md section 10.

    None is a real answer and not a failure. A stopped print whose progress was
    never reported is left unbooked rather than charged a made up amount.
    """
    section = state.get(PRINT_SECTION, {})

    layer = _as_int(section.get("layer_num"))
    total = _as_int(section.get("total_layer_num"))
    if layer is not None and total:
        return _clamp(layer / total)

    percent = _as_int(section.get("mc_percent"))
    if percent is not None:
        return _clamp(percent / 100)

    return None


def layer_of(state: dict) -> int | None:
    """Which layer the printer is on, or None when it does not say."""
    return _as_int(state.get(PRINT_SECTION, {}).get("layer_num"))


def total_layers_of(state: dict) -> int | None:
    """How many layers the print has in total, or None when it does not say."""
    return _as_int(state.get(PRINT_SECTION, {}).get("total_layer_num"))


def remaining_minutes_of(state: dict) -> int | None:
    """How much longer the printer thinks it needs, in minutes.

    The field is ``mc_remaining_time``, the one bambulabs_api reads for the same
    purpose. Bambu reports it in minutes, which is what its own display shows.
    A four digit number next to a print of half an hour would mean seconds, and
    this is the single place that would have to be corrected.
    """
    return _as_int(state.get(PRINT_SECTION, {}).get("mc_remaining_time"))


def _as_int(value: Any) -> int | None:
    """Read a number a printer reported, tolerating text and nonsense."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clamp(share: float) -> float:
    """Keep a share inside 0 to 1, whatever the printer counted."""
    return min(max(share, 0.0), 1.0)
