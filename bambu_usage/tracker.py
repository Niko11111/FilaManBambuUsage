"""Printer listeners.

One listener per active printer with driver_key "bambulab". This is the only
module that knows where print events come from. Should the Bambu Lab driver ever
publish a print_complete event on FilaMan's event bus, this module is the single
one that gets replaced. See docs/01_Design.md, section 4.

What runs these listeners, and in which of the four worker processes, is
supervisor.py, and what a report actually means is report.py. This module is the
connection and what it does about what report.py decided.

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

May import: report, service, settings, store, models, threemf, filaman,
bambulabs_api. Must not import router, supervisor or app. Enforced by
tools/check_architecture.py.

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

from . import rules
from .report import (
    STATE_FINISH,
    TRANSITION_STARTED,
    active_tray,
    completed_fraction,
    describe_job,
    error_code,
    detect_transition,
    gcode_state_of,
    layer_of,
    merge_report,
    progress_of,
    remaining_minutes_of,
    total_layers_of,
    tray_tags,
)

if TYPE_CHECKING:  # imported for annotations only
    from .filaman import BambuPrinter

logger = logging.getLogger(__name__)


class TrackerError(RuntimeError):
    """A listener could not be brought up or kept alive."""

# Reconnecting is paho's own job: connect_async plus loop_start retries with a
# growing delay by itself, and one unreachable printer therefore never stalls
# the others. Nothing in this plugin has to schedule that.

# An error message goes into a column of this width, so it is cut to fit rather
# than failing the insert.
MAX_ERROR_LENGTH = 500

# How often the slot assignment of a running print is compared against what was
# recorded at its start. A spool that runs empty is swapped in minutes, not in
# seconds, and this costs one small query per slot.
ASSIGNMENT_CHECK_INTERVAL_SECONDS = 30

# gcode_state values that matter. PREPARE -> RUNNING marks a local print start.

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
    last_assignment_check: datetime | None = None

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
            try:
                await self._watch_assignments(moment)
            except Exception as exc:
                logger.exception("printer %s: watching the assignment failed", self.printer_id)
                self.last_error = str(exc)[:MAX_ERROR_LENGTH]
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
                tray_tags=self._tray_tags(),
            )

        self.current_print_id = print_id
        self.current_file_name = job.file_name
        self.last_assignment_check = None
        logger.info(
            "printer %s: print %s started, %s, %d filaments",
            self.printer_id,
            print_id,
            job.file_name,
            len(metadata.filaments),
        )
        await self.publish_status()

    def _active_slot_index(self) -> str | None:
        """Which slot the printer is drawing from, named the way FilaMan does.

        Converted here rather than in report.py, which may not import rules.
        """
        tray = active_tray(self.state)
        return None if tray is None else rules.tray_to_slot_index(tray)

    def _tray_tags(self) -> dict[str, str]:
        """What tag sits in which slot, as the printer last reported it.

        Read out of the state that is merged anyway, so no extra subscription
        and no state of its own. The ids become a slot_index here, where both
        report and rules may be imported; report may not import rules.
        """
        return {
            rules.slot_index(tag.ams_id, tag.tray_id): tag.uuid
            for tag in tray_tags(self.state)
        }

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
        from .models import STATUS_CANCELLED, STATUS_FAILED, STATUS_FINISHED

        # A fault leaves a code behind, a print somebody stopped does not. That
        # reading is an assumption, see report.error_code, so the code itself is
        # stored with the print: the first real fault shows whether it holds.
        code = error_code(self.state)
        if gcode_state == STATE_FINISH:
            status = STATUS_FINISHED
        else:
            status = STATUS_FAILED if code else STATUS_CANCELLED

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
                stopped_at_layer=layer_of(self.state),
                printer_error_code=code,
            )

        logger.info("printer %s: print %s ended as %s", self.printer_id, print_id, status)
        self.current_print_id = None
        self.current_file_name = None
        await self.publish_status()

    async def _watch_assignments(self, at: datetime) -> None:
        """Notice a spool being swapped while the print is running.

        A spool that runs empty gets replaced, and from that moment the print
        draws from a different one. Charging the whole print to either of them is
        wrong, so the filament row is split where the change happened and each
        spool is charged its share.

        Three rules, and each of them is there because of a case that would
        otherwise be got wrong:

        * A slot that currently resolves to nothing is ignored. During a swap the
          tray is briefly empty, and splitting on that would invent a stretch of
          print with no spool at all.
        * A row that had no spool to begin with adopts the one that turned up.
          The assignment usually reaches FilaMan a moment after the print
          started, and that is a late arrival, not a change.
        * A row that was already closed off by an earlier split is left alone.
          It belongs to a part of the print that is over.

        Without a usable progress figure nothing is split. The result would land
        on two spools, and a guess is expensive twice over.
        """
        if self.current_print_id is None:
            return

        if self.last_assignment_check is not None:
            since = (at - self.last_assignment_check).total_seconds()
            if since < ASSIGNMENT_CHECK_INTERVAL_SECONDS:
                return
        self.last_assignment_check = at

        from . import filaman, rules, service, store

        fraction = completed_fraction(self.state)
        layer = layer_of(self.state)

        async with self.sessions() as db:
            record = await store.get_print(db, self.current_print_id)
            curves = store.decode_layer_shares(record.layer_shares) if record else {}

            for row in await store.list_filaments(db, self.current_print_id):
                if row.slot_index is None or row.spent_at is not None:
                    continue
                if row.to_fraction is not None:
                    continue

                current = await filaman.resolve_spool_for_slot(
                    db, self.printer_id, row.slot_index
                )
                if current is None or current == row.spool_id:
                    continue

                if row.spool_id is None:
                    await store.set_filament_spool(db, int(row.id), current, manual=False)
                    await db.commit()
                    logger.info(
                        "printer %s: slot %s resolved to spool %s after the print had started",
                        self.printer_id,
                        row.slot_index,
                        current,
                    )
                    continue

                # What the print had laid down of this filament, from the
                # gcode where there is a curve and linearly otherwise.
                exact = rules.share_at_layer(curves.get(int(row.filament_id)), layer)
                at = fraction if exact is None else exact

                if at is None:
                    logger.warning(
                        "printer %s: slot %s changed from spool %s to %s, but the progress "
                        "is unknown, so nothing was split",
                        self.printer_id,
                        row.slot_index,
                        row.spool_id,
                        current,
                    )
                    continue

                if await service.split_filament_row(db, int(row.id), at, current):
                    logger.info(
                        "printer %s: slot %s changed from spool %s to %s at %.0f%% of its filament",
                        self.printer_id,
                        row.slot_index,
                        row.spool_id,
                        current,
                        at * 100,
                    )

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
                layer_num=layer_of(self.state),
                total_layer_num=total_layers_of(self.state),
                remaining_minutes=remaining_minutes_of(self.state),
                active_slot_index=self._active_slot_index(),
                last_error=self.last_error,
            )
            await db.commit()
