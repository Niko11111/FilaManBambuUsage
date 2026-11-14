"""Whole print courses through supervisor, tracker and service, on a real database.

What is faked is only what lives outside this plugin: the MQTT client, the 3MF
download and FilaMan's spool service. Everything in between runs for real on
SQLite, which is what these tests are for: the plugin switch and an end that
nobody saw are about how the parts work together, not about any one of them.

The scaffolding follows the tests akira69 wrote for pull request #2.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bambu_usage import filaman, supervisor
from bambu_usage.threemf import FilamentInfo, PrintMetadata
from bambu_usage.tracker import PrinterListener

from ._support import HAS_TEST_DEPENDENCIES

if HAS_TEST_DEPENDENCIES:
    from sqlalchemy import Boolean, Column, MetaData, String, Table, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from bambu_usage import models, settings
    from bambu_usage.schemas import PluginSettings

    # The one FilaMan table the plugin reads here, reduced to the two columns
    # filaman.is_plugin_active touches.
    host_metadata = MetaData()
    installed_plugins = Table(
        "installed_plugins",
        host_metadata,
        Column("plugin_key", String, primary_key=True),
        Column("is_active", Boolean, nullable=False),
    )

PRINTER_ID = 1
SPOOL_BY_SLOT = {"0-0": 7, "0-1": 8}
GRAMS_BY_SPOOL = [(7, 10.0), (8, 20.0)]


class FakeMQTT:
    """Stands in for bambulabs_api.PrinterMQTTClient. Reports are fed by hand."""

    def __init__(self, host, access_code, serial):
        self.running = False

    def connect(self):
        pass

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


@unittest.skipUnless(HAS_TEST_DEPENDENCIES, "needs sqlalchemy, aiosqlite and pydantic")
class TrackingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        directory = stack.enter_context(tempfile.TemporaryDirectory())
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{directory}/test.sqlite")
        self.addAsyncCleanup(self.engine.dispose)
        await models.ensure_tables(self.engine)
        async with self.engine.begin() as db:
            await db.run_sync(host_metadata.create_all)
            await db.execute(
                installed_plugins.insert().values(plugin_key=filaman.PLUGIN_KEY, is_active=True)
            )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            await settings.save_settings(
                db, PluginSettings(printer_id=PRINTER_ID, tracking_enabled=True)
            )
            await db.commit()

        self.owner = supervisor._Supervisor()
        self.owner.sessions = self.sessions
        self.owner.loop = asyncio.get_running_loop()
        self.consumptions = []

        printer = filaman.BambuPrinter(PRINTER_ID, "X1C", "printer.local", "serial", "code")
        for patch in (
            mock.patch.object(supervisor, "_supervisor", self.owner),
            mock.patch.dict(sys.modules, {
                "app.models.plugin": SimpleNamespace(InstalledPlugin=installed_plugins.c),
                "bambulabs_api": SimpleNamespace(PrinterMQTTClient=FakeMQTT),
            }),
            mock.patch.object(filaman, "list_bambu_printers", return_value=[printer]),
            mock.patch.object(filaman, "resolve_spool_for_slot", side_effect=self.resolve_spool),
            mock.patch.object(filaman, "load_spool", side_effect=self.load_spool),
            mock.patch.object(filaman, "record_consumption", side_effect=self.record_consumption),
            mock.patch.object(PrinterListener, "_fetch_3mf", side_effect=self.fetch_3mf),
        ):
            stack.enter_context(patch)

    async def resolve_spool(self, db, printer_id, slot_index):
        return SPOOL_BY_SLOT.get(slot_index)

    async def load_spool(self, db, spool_id):
        return SimpleNamespace(id=spool_id)

    async def record_consumption(self, db, spool, grams, at, note=None):
        await db.commit()
        self.consumptions.append((spool.id, grams))

    async def fetch_3mf(self, job):
        return PrintMetadata(filaments=[
            FilamentInfo(filament_id=1, material="PLA", used_g=10),
            FilamentInfo(filament_id=2, material="PLA", used_g=20),
        ]), None

    async def switch_plugin(self, active):
        """Flip FilaMan's switch and let one reconciliation round see it."""
        async with self.sessions() as db:
            await db.execute(installed_plugins.update().values(is_active=active))
            await db.commit()
        await supervisor.refresh_printers()

    async def report(self, state, subtask_id="job-1", **fields):
        """Feed one printer report to the current listener."""
        listener = self.owner.listeners[PRINTER_ID]
        await listener.handle_message({"print": {
            "gcode_state": state,
            "subtask_id": subtask_id,
            "subtask_name": f"{subtask_id}.3mf",
            "ams_mapping": [0, 1],
            **fields,
        }})

    async def start_print(self):
        await supervisor.refresh_printers()
        await self.report("IDLE")
        await self.report("RUNNING")

    async def prints(self):
        async with self.sessions() as db:
            return (await db.execute(select(models.prints_table))).all()

    async def test_a_switched_off_plugin_starts_no_listener(self):
        await self.switch_plugin(False)
        self.assertEqual(supervisor.watched_printers(), 0)

    async def test_switching_off_closes_the_listener_and_keeps_the_settings(self):
        await supervisor.refresh_printers()
        self.assertEqual(supervisor.watched_printers(), 1)

        await self.switch_plugin(False)

        self.assertEqual(supervisor.watched_printers(), 0)
        async with self.sessions() as db:
            self.assertTrue((await settings.load_settings(db, PRINTER_ID)).tracking_enabled)

    async def test_a_plugin_without_a_row_counts_as_switched_on(self):
        # A hand copied development install has no row. Never tracking there
        # without a word would be the harder fault to find.
        async with self.sessions() as db:
            await db.execute(installed_plugins.delete())
            await db.commit()
        await supervisor.refresh_printers()
        self.assertEqual(supervisor.watched_printers(), 1)

    async def test_a_print_running_across_off_and_on_is_booked_once_at_its_end(self):
        await self.start_print()
        await self.switch_plugin(False)
        await self.switch_plugin(True)

        # The new listener joins the print in the middle and takes it over.
        await self.report("RUNNING", mc_percent=70)
        await self.report("FINISH", mc_percent=100)

        prints = await self.prints()
        self.assertEqual(len(prints), 1)
        self.assertEqual(prints[0].status, models.STATUS_FINISHED)
        self.assertEqual(self.consumptions, GRAMS_BY_SPOOL)

    async def test_a_print_that_ended_while_switched_off_is_booked_on_return(self):
        await self.start_print()
        await self.switch_plugin(False)
        await self.switch_plugin(True)

        await self.report("FINISH", mc_percent=100)

        prints = await self.prints()
        self.assertEqual(prints[0].status, models.STATUS_FINISHED)
        self.assertIsNotNone(prints[0].finished_at)
        self.assertEqual(self.consumptions, GRAMS_BY_SPOOL)

    async def test_a_print_that_ended_during_a_restart_is_booked(self):
        # A fresh listener, as after a FilaMan restart, whose first report
        # already says the print is over. Before 1.0.0 the print stayed
        # "running" for good and could not even be booked by hand.
        await self.start_print()
        await supervisor._drop_listener(PRINTER_ID)
        await supervisor.refresh_printers()

        await self.report("FINISH", mc_percent=100)

        self.assertEqual((await self.prints())[0].status, models.STATUS_FINISHED)
        self.assertEqual(self.consumptions, GRAMS_BY_SPOOL)

    async def test_a_print_cancelled_while_switched_off_ends_as_cancelled(self):
        # Whether it is then booked is the spend_on_cancel setting's business,
        # exactly as for a cancel somebody saw.
        await self.start_print()
        await self.switch_plugin(False)
        await self.switch_plugin(True)

        await self.report("FAILED", mc_percent=40)

        self.assertEqual((await self.prints())[0].status, models.STATUS_CANCELLED)

    async def test_a_print_booked_long_ago_is_not_booked_again(self):
        # After every restart the printer still reports yesterday's job as
        # finished, under the same subtask id.
        await self.start_print()
        await self.report("FINISH")
        self.assertEqual(self.consumptions, GRAMS_BY_SPOOL)

        await self.switch_plugin(False)
        await self.switch_plugin(True)
        await self.report("FINISH")

        self.assertEqual(self.consumptions, GRAMS_BY_SPOOL)

    async def test_a_print_superseded_while_nobody_listened_is_closed(self):
        # Job 1 ended while the plugin was off, and job 2 already runs when it
        # is back. Job 1 must not stay "running" for good.
        await self.start_print()
        await self.switch_plugin(False)
        await self.switch_plugin(True)

        await self.report("RUNNING", subtask_id="job-2")

        by_job = {row.subtask_id: row for row in await self.prints()}
        self.assertEqual(by_job["job-1"].status, models.STATUS_END_UNSEEN)
        self.assertIsNotNone(by_job["job-1"].finished_at)
        self.assertEqual(by_job["job-2"].status, models.STATUS_INCOMPLETE)
        self.assertEqual(self.consumptions, [])

    async def test_expired_history_is_deleted_once_a_day(self):
        now = datetime.now(timezone.utc)
        await self.start_print()
        await self.report("FINISH")
        async with self.sessions() as db:
            await db.execute(
                models.prints_table.update().values(started_at=now - timedelta(days=400))
            )
            await db.commit()

        self.assertEqual(await supervisor.purge_history_if_due(now), 1)
        self.assertEqual(await self.prints(), [])
        # Booked stays booked: tidying the history does not refill a spool.
        self.assertEqual(self.consumptions, GRAMS_BY_SPOOL)

        await self.start_print()
        async with self.sessions() as db:
            await db.execute(
                models.prints_table.update().values(started_at=now - timedelta(days=400))
            )
            await db.commit()
        self.assertEqual(await supervisor.purge_history_if_due(now + timedelta(hours=1)), 0)
        self.assertEqual(len(await self.prints()), 1)

    async def test_an_end_does_not_overtake_its_own_start(self):
        # Cancelled within seconds, while the 3MF is still on its way. Each
        # report is its own task, and the end must wait for the start.
        download_done = asyncio.Event()
        fetch = self.fetch_3mf

        async def slow_fetch(job):
            await download_done.wait()
            return await fetch(job)

        await supervisor.refresh_printers()
        await self.report("IDLE")
        with mock.patch.object(PrinterListener, "_fetch_3mf", side_effect=slow_fetch):
            start = asyncio.create_task(self.report("RUNNING"))
            end = asyncio.create_task(self.report("FINISH"))
            await asyncio.sleep(0)
            download_done.set()
            await asyncio.gather(start, end)

        prints = await self.prints()
        self.assertEqual(len(prints), 1)
        self.assertEqual(prints[0].status, models.STATUS_FINISHED)
        self.assertEqual(self.consumptions, GRAMS_BY_SPOOL)

    async def test_an_unseen_end_of_a_job_never_recorded_is_ignored(self):
        await supervisor.refresh_printers()
        await self.report("FINISH", subtask_id="somebody-elses")

        self.assertEqual(await self.prints(), [])
        self.assertEqual(self.consumptions, [])


class StartupTest(unittest.IsolatedAsyncioTestCase):
    """The parts of starting up that need neither a database nor a printer."""

    async def test_a_failed_start_is_tried_again(self):
        # FilaMan still migrating on a slow machine. Giving up would leave this
        # worker holding the lock with nothing running behind it.
        start = mock.AsyncMock(side_effect=[RuntimeError("database is locked"), None])
        with (
            mock.patch.object(supervisor, "start_tracking", start),
            mock.patch.object(supervisor, "stop_tracking", mock.AsyncMock()),
            mock.patch.object(supervisor.asyncio, "sleep", mock.AsyncMock()),
            self.assertLogs("bambu_usage.supervisor", level="ERROR"),
        ):
            await supervisor._start_until_it_works()

        self.assertEqual(start.await_count, 2)

    async def test_an_unreadable_switch_counts_as_on(self):
        unreadable = mock.AsyncMock(side_effect=filaman.FilaManUnavailableError("moved"))
        with (
            mock.patch.object(filaman, "is_plugin_active", unreadable),
            self.assertLogs("bambu_usage.supervisor", level="WARNING"),
        ):
            self.assertTrue(await supervisor._plugin_switched_on(None))


if __name__ == "__main__":
    unittest.main()
