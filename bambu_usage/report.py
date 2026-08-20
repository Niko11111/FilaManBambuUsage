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


@dataclass(frozen=True)
class Transition:
    """What changed between two reports, as far as this plugin cares."""

    kind: str
    gcode_state: str | None = None


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


def _as_int(value: Any) -> int | None:
    """Read a number a printer reported, tolerating text and nonsense."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clamp(share: float) -> float:
    """Keep a share inside 0 to 1, whatever the printer counted."""
    return min(max(share, 0.0), 1.0)
