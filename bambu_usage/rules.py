"""The arithmetic of consumption.

Every rule in this plugin that can be decided without a database, without
FilaMan and without a printer lives here: which tray a slicer filament came
from, what a spool is charged, how much of an estimate a stopped print costs,
and where a row is divided when the spool it draws from changed.

That is the point of the module. These are the parts that are worth testing, and
keeping them apart from everything that opens a session or a socket is what makes
them testable at all. Nothing here reads or writes anything.

May import: nothing, beyond the standard library. Enforced by
tools/check_architecture.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # imported for annotations only
    from collections.abc import Iterable

    from .threemf import FilamentInfo

# Bambu reserves these for the external spool holder. FilaMan stores the same
# pair as the slot_index "255-254".
EXTERNAL_SPOOL_AMS_ID = 255
EXTERNAL_SPOOL_TRAY_ID = 254

# Trays are numbered globally across AMS units, four trays per unit.
TRAYS_PER_AMS = 4


def slot_index(ams_id: int, tray_id: int) -> str:
    """The way FilaMan names one tray of one AMS.

    The one place this format is written. Two callers build it from different
    inputs and must not drift apart: tray_to_slot_index counts globally across
    AMS units, while a report names the unit and the tray separately.

    >>> slot_index(1, 1)
    '1-1'
    """
    return f"{ams_id}-{tray_id}"


EXTERNAL_SLOT_INDEX = slot_index(EXTERNAL_SPOOL_AMS_ID, EXTERNAL_SPOOL_TRAY_ID)


def tray_to_slot_index(tray: int) -> str:
    """Translate a global Bambu tray number into FilaMan's slot_index.

    A negative tray number means the print does not use an AMS slot for this
    filament, which is the external spool holder.

    >>> tray_to_slot_index(5)
    '1-1'
    >>> tray_to_slot_index(0)
    '0-0'
    >>> tray_to_slot_index(-1)
    '255-254'
    """
    if tray < 0:
        return EXTERNAL_SLOT_INDEX
    return slot_index(tray // TRAYS_PER_AMS, tray % TRAYS_PER_AMS)


def resolve_slot_indexes(
    filaments: list[FilamentInfo],
    ams_mapping: list[Any],
) -> dict[int, str | None]:
    """Map every slicer filament to the slot it printed from.

    ``ams_mapping`` is indexed from zero while the slicer numbers its filaments
    from one. Containing that off-by-one is the whole job of this function; get
    it wrong and every spool of a multi colour print is charged to its neighbour.

    A filament the mapping does not cover, or covers with something that is not
    a tray number, resolves to None. The print then lands with an open
    assignment instead of a guessed one.
    """
    resolved: dict[int, str | None] = {}

    for filament in filaments:
        index = filament.filament_id - 1
        if index < 0 or index >= len(ams_mapping):
            resolved[filament.filament_id] = None
            continue

        try:
            tray = int(ams_mapping[index])
        except (TypeError, ValueError):
            resolved[filament.filament_id] = None
            continue

        resolved[filament.filament_id] = tray_to_slot_index(tray)

    return resolved


def sum_grams_per_spool(rows: Iterable[tuple[int | None, float | None]]) -> dict[int, float]:
    """Total the grams per spool, so one spool produces one booking.

    A print can address the same tray several times, for instance a multi colour
    model reusing one colour. Booking each row on its own would hang several
    events off the same spool and blur which print cost what, so the amounts are
    summed first. See docs/04_Data_Model.md section 6.

    Rows without a spool or without a usable amount are dropped: there is
    nothing to book, and a zero gram event is noise in the spool log.
    """
    totals: dict[int, float] = {}

    for spool_id, grams in rows:
        if spool_id is None or grams is None or grams <= 0:
            continue
        totals[spool_id] = totals.get(spool_id, 0.0) + float(grams)

    return totals


def should_spend(*, finished_normally: bool, auto_spend: bool, spend_on_cancel: bool) -> bool:
    """Whether a print that just ended is booked automatically.

    Booking happens at the end and not at the start, so an aborted print does not
    cost the full estimate. See docs/01_Design.md section 6.4.
    """
    if not auto_spend:
        return False
    if finished_normally:
        return True
    return spend_on_cancel


def split_share(
    from_fraction: float | None,
    to_fraction: float | None,
    at: float,
) -> float | None:
    """What share of a filament row belongs to the part before *at*.

    A row covers a span of the print, by default the whole of it, and both ends
    being None means exactly that. Splitting a span (a, b) at *at* leaves the
    first part with ``(at - a) / (b - a)``.

    Returns None when *at* does not fall strictly inside the span. Splitting at
    an edge would produce an empty row, and a span that does not contain the
    moment belongs to a part of the print that is already over.
    """
    start = 0.0 if from_fraction is None else float(from_fraction)
    end = 1.0 if to_fraction is None else float(to_fraction)

    if not start < at < end:
        return None

    return (at - start) / (end - start)


def share_at_layer(curve: list[float] | None, layer: int | None) -> float | None:
    """How much of a filament had been used once *layer* was done.

    The curve comes out of the plate gcode and says what the print really laid
    down, which is not the same as how far through the layers it got: a
    filament used only in the last third sits at zero for two thirds of them.
    That difference is the whole reason the gcode is read at all.

    None where there is no curve or no layer, and the caller then falls back
    to the linear share.
    """
    if not curve or layer is None or layer < 1:
        return None

    return curve[min(layer, len(curve)) - 1]


def booking_factor(*, was_stopped: bool, completed_fraction: float | None) -> float:
    """What share of the estimate a print costs.

    A print that ran to the end costs all of it. One that was stopped costs the
    share it got through, and **nothing at all** when that share is unknown: an
    invented number on a spool is worse than an open row somebody can correct,
    and it is exactly the mistake of booking the full estimate for an abort.

    A print this plugin only saw part of is not "stopped" in this sense. It is
    never booked automatically, and when a human books it anyway that is a
    decision, so it costs the full estimate.
    """
    if not was_stopped:
        return 1.0
    if completed_fraction is None:
        return 0.0

    return min(max(float(completed_fraction), 0.0), 1.0)


def print_cost(rows: Iterable[Any], prices: dict[int, float]) -> float | None:
    """What a print cost, or None when nothing behind it carries a price.

    Costed on what was actually booked where there is a booking, on the estimate
    otherwise, which is the same rule the booking itself follows. A row whose
    spool has no price is skipped rather than counted as free: a total that
    silently leaves parts out would look like a bargain.
    """
    total = None

    for row in rows:
        price = prices.get(row.spool_id)
        amount = amount_of(row)
        if price is None or amount is None:
            continue
        total = (total or 0.0) + price * amount

    return total


def share_of(row: Any, factor: float) -> float | None:
    """What a row costs once the share of the print is taken into account."""
    amount = amount_of(row)
    return None if amount is None else amount * factor


def amount_of(row: Any) -> float | None:
    """What a filament row costs: the corrected amount if there is one.

    The slicer estimate is the fallback, never the other way round, so a value
    somebody entered by hand is not overruled by the machine.
    """
    if row.spent_grams is not None:
        return float(row.spent_grams)
    return None if row.estimated_grams is None else float(row.estimated_grams)


def split_amounts(total: float | None, share: float) -> tuple[float | None, float | None]:
    """Divide an estimate into the part before a split and the part after.

    An unknown estimate stays unknown on both sides rather than becoming a
    number nobody measured.
    """
    if total is None:
        return None, None

    kept = total * share
    return kept, total - kept
