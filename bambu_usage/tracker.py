"""Printer listeners.

One listener per active printer with driver_key "bambulab". This is the only
module that knows where print events come from. Should the Bambu Lab driver ever
publish a print_complete event on FilaMan's event bus, this module is the single
one that gets replaced. See docs/01_Design.md, section 4.

What runs these listeners, and in which of the four worker processes, is
supervisor.py. This module is only about one printer: what its reports mean and
what to do about them.

**The paho callback runs in paho's thread, not in ours.** Every message is
handed over to the tracker's own event loop with ``run_coroutine_threadsafe``,
and everything after that point is ordinary asyncio.

Because the listeners live in one process while the plugin page is answered by
any of the four, their state is written to ``bambu_usage_printer_status`` rather
than kept in memory. The database is the only place all four can see.

Every import that needs SQLAlchemy, Pydantic or the printer library sits inside
the function that uses it, for the same reason as in service.py: the pure parts
of this module, the ones that decide what a report means, stay testable without
a database, without FilaMan and without a printer.

May import: service, settings, store, models, threemf, filaman, bambulabs_api.
Must not import router, supervisor or app. Enforced by
tools/check_architecture.py.

On the size of this file, past the 400 lines CLAUDE.md asks a reason for: the
pure functions at the top have exactly one caller, ``handle_message`` below
them, and what a report means and what the listener does about it is the one
thing a reviewer has to read together. Putting them one import apart would make
the file shorter and the review harder.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # imported for annotations only
    from .filaman import BambuPrinter

logger = logging.getLogger(__name__)

# Reconnecting is paho's own job: connect_async plus loop_start retries with a
# growing delay by itself, and one unreachable printer therefore never stalls
# the others. Nothing in this plugin has to schedule that.

# An error message goes into a column of this width, so it is cut to fit rather
# than failing the insert.
MAX_ERROR_LENGTH = 500

# gcode_state values that matter. PREPARE -> RUNNING marks a local print start.
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


class TrackerError(RuntimeError):
    """A listener could not be brought up or kept alive."""


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


@dataclass
class PrinterListener:
    """The connection to one printer, and what it has told us so far.

    The printer sends partial updates, so the reported state has to be carried
    along and merged; a single message is rarely complete. Keeping that state on
    the object rather than in a module level dict is the lesson from
    OpenSpoolMan, where PRINTER_STATE and friends are globals mutated from MQTT
    callbacks.
    """

    printer_id: int
    printer_name: str
    host: str
    serial: str
    access_code: str
    sessions: Any
    loop: asyncio.AbstractEventLoop
    connected: bool = False
    last_error: str | None = None
    current_print_id: int | None = None
    current_file_name: str | None = None
    state: dict = field(default_factory=dict)
    client: Any = None

    def describes(self, printer: BambuPrinter) -> bool:
        """Whether this listener still matches how FilaMan knows the printer.

        A changed host or access code means the connection has to be rebuilt,
        not merely relabelled.
        """
        return (self.printer_name, self.host, self.serial, self.access_code) == (
            printer.name,
            printer.host,
            printer.serial,
            printer.access_code,
        )

    async def start(self) -> None:
        """Connect and begin consuming reports.

        bambulabs_api brings the parts that are tedious to get right: implicit
        TLS, the credentials, the subscription to device/<serial>/report, and a
        pushall on connect, which is what makes the printer send its full state
        instead of leaving us blind until the next change.
        """
        from bambulabs_api import PrinterMQTTClient

        client = PrinterMQTTClient(self.host, self.access_code, self.serial)
        client.on_message_handler = self._on_message
        client.on_connect_handler = self._on_connect
        client.on_disconnect_handler = self._on_disconnect

        self.client = client
        client.connect()
        client.start()

        logger.info("printer %s: listener started for %s", self.printer_id, self.host)
        await self.publish_status()

    async def stop(self) -> None:
        """Disconnect and release the connection."""
        if self.client is not None:
            with contextlib.suppress(OSError, RuntimeError):
                self.client.stop()

        self.client = None
        self.connected = False
        logger.info("printer %s: listener stopped", self.printer_id)
        await self.publish_status()

    def _on_message(self, mqtt_client: Any, client: Any, userdata: Any, msg: Any) -> None:
        """Hand one report over to our loop. Runs in paho's thread.

        Nothing but parsing happens here. Everything that touches the database
        or the network belongs in the loop, and getting that wrong would block
        paho's thread and stall the connection.
        """
        try:
            payload = json.loads(msg.payload)
        except (TypeError, ValueError):
            logger.warning("printer %s sent a report that is not JSON", self.printer_id)
            return

        if not isinstance(payload, dict):
            return

        asyncio.run_coroutine_threadsafe(self.handle_message(payload), self.loop)

    def _on_connect(self, *_arguments: Any) -> None:
        """Runs in paho's thread when the connection is up."""
        self.connected = True
        self.last_error = None
        asyncio.run_coroutine_threadsafe(self.publish_status(), self.loop)

    def _on_disconnect(self, *_arguments: Any) -> None:
        """Runs in paho's thread when the connection drops. paho reconnects."""
        self.connected = False
        asyncio.run_coroutine_threadsafe(self.publish_status(), self.loop)

    async def handle_message(self, payload: dict, received_at: datetime | None = None) -> None:
        """Merge one report into the state and act on any transition.

        Catches everything on purpose. This runs detached in the loop, nobody
        awaits the future, and an exception would vanish into it and take this
        printer silently out of service. One printer must never take the others
        down, and it must not disappear quietly either.
        """
        moment = received_at or datetime.now(timezone.utc)

        # Whether a state was ever known, not whether a message was ever seen.
        # The first message can be an unrelated reply that carries no
        # gcode_state, and treating the print that follows as a normal start
        # would book a print this plugin only saw the tail of.
        joined_mid_print = gcode_state_of(self.state) is None

        merged = merge_report(self.state, payload)
        transition = detect_transition(self.state, merged)
        self.state = merged

        if transition is None:
            return

        try:
            if transition.kind == TRANSITION_STARTED:
                await self._begin_print(joined_mid_print=joined_mid_print, at=moment)
            else:
                await self._end_print(transition.gcode_state, at=moment)
        except Exception as exc:
            logger.exception("printer %s: handling a report failed", self.printer_id)
            self.last_error = str(exc)[:MAX_ERROR_LENGTH]
            with contextlib.suppress(Exception):
                await self.publish_status()

    async def _begin_print(self, joined_mid_print: bool, at: datetime) -> None:
        """Record a print that is starting, with whatever the 3MF gives us."""
        from . import service, settings
        from .models import STATUS_INCOMPLETE

        job = describe_job(self.state)

        async with self.sessions() as db:
            config = await settings.load_settings(db, self.printer_id)

        if not config.tracking_enabled:
            return

        # Fetched outside a session: this reaches over the network and can take
        # the better part of a minute, and holding a database connection open
        # for that would be felt by every other worker on SQLite.
        metadata, status = await self._fetch_3mf(job)

        if joined_mid_print:
            # The plugin attached in the middle of this print. It is recorded so
            # it does not vanish, and never booked, because how much of it ran
            # before we were listening is unknowable. docs/01_Design.md, 7.
            status = STATUS_INCOMPLETE

        async with self.sessions() as db:
            print_id = await service.start_print(
                db,
                printer_id=self.printer_id,
                file_name=job.file_name,
                print_type=job.print_type,
                metadata=metadata,
                ams_mapping=job.ams_mapping,
                subtask_id=job.subtask_id,
                started_at=at,
                status=status,
            )

        self.current_print_id = print_id
        self.current_file_name = job.file_name
        logger.info(
            "printer %s: print %s started, %s, %d filaments",
            self.printer_id,
            print_id,
            job.file_name,
            len(metadata.filaments),
        )
        await self.publish_status()

    async def _fetch_3mf(self, job: PrintJob) -> tuple[Any, str | None]:
        """Fetch and read the 3MF of *job*.

        Returns the metadata and the status to record with it: None for the
        normal case, no_3mf when the file could not be had. A print without its
        numbers still belongs in the history, so this never raises.
        """
        from . import threemf
        from .models import STATUS_NO_3MF

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "job.3mf"

            try:
                if job.url:
                    await threemf.download_from_url(job.url, destination)
                elif job.remote_path:
                    await threemf.download_from_printer(
                        self.host, self.access_code, job.remote_path, destination
                    )
                else:
                    logger.warning(
                        "printer %s: print %s names no 3MF at all", self.printer_id, job.file_name
                    )
                    return threemf.PrintMetadata(), STATUS_NO_3MF

                return threemf.parse(destination), None

            except threemf.ThreeMFError as exc:
                logger.warning("printer %s: %s", self.printer_id, exc)
                self.last_error = str(exc)[:MAX_ERROR_LENGTH]
                return threemf.PrintMetadata(), STATUS_NO_3MF

    async def _end_print(self, gcode_state: str | None, at: datetime) -> None:
        """Close the running print and let the service decide about booking."""
        from . import service, settings, store
        from .models import STATUS_FAILED, STATUS_FINISHED

        status = STATUS_FINISHED if gcode_state == STATE_FINISH else STATUS_FAILED

        async with self.sessions() as db:
            print_id = self.current_print_id
            if print_id is None:
                # FilaMan may have restarted in the middle of this print.
                record = await store.find_open_print(db, self.printer_id)
                print_id = int(record.id) if record is not None else None

            if print_id is None:
                logger.info(
                    "printer %s: a print ended that this plugin never saw start", self.printer_id
                )
                return

            config = await settings.load_settings(db, self.printer_id)
            await service.finish_print(
                db,
                print_id,
                status,
                config,
                at,
                completed_fraction=completed_fraction(self.state),
            )

        logger.info("printer %s: print %s ended as %s", self.printer_id, print_id, status)
        self.current_print_id = None
        self.current_file_name = None
        await self.publish_status()

    async def publish_status(self) -> None:
        """Write what this listener knows into the table the page reads."""
        from . import store

        async with self.sessions() as db:
            await store.upsert_printer_status(
                db,
                printer_id=self.printer_id,
                printer_name=self.printer_name,
                connected=self.connected,
                tracking_enabled=True,
                updated_at=datetime.now(timezone.utc),
                current_print_id=self.current_print_id,
                current_file_name=self.current_file_name,
                progress_percent=progress_of(self.state),
                last_error=self.last_error,
            )
            await db.commit()
