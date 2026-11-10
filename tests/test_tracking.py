"""Exercise toggle reconciliation and report writes with a real database."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bambu_usage import filaman, supervisor
from bambu_usage.threemf import FilamentInfo, PrintMetadata
from bambu_usage.tracker import PrinterListener

from ._support import HAS_TEST_DEPENDENCIES

if HAS_TEST_DEPENDENCIES:
    from bambu_usage import models, settings, store
    from bambu_usage.schemas import PluginSettings
    from sqlalchemy import Boolean, Column, MetaData, String, Table
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    host_metadata = MetaData()
    installed_plugins = Table(
        "installed_plugins",
        host_metadata,
        Column("plugin_key", String, primary_key=True),
        Column("is_active", Boolean, nullable=False),
    )


class FakeMQTT:
    """Stand in for the printer connection without changing listener behavior."""

    def __init__(self, host, access_code, serial):
        self.running = False

    def connect(self):
        pass

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


@unittest.skipUnless(HAS_TEST_DEPENDENCIES, "needs sqlalchemy, aiosqlite and pydantic")
class PluginToggleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        directory = stack.enter_context(tempfile.TemporaryDirectory())
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{directory}/test.sqlite")
        self.addAsyncCleanup(self.engine.dispose)
        await models.ensure_tables(self.engine)
        async with self.engine.begin() as db:
            await db.run_sync(host_metadata.create_all)
            await db.execute(installed_plugins.insert().values(plugin_key="bambu_usage", is_active=True))
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            await settings.save_settings(db, PluginSettings(printer_id=1, tracking_enabled=True))
            await db.commit()

        self.owner = self.new_supervisor()
        stack.enter_context(mock.patch.object(supervisor, "_supervisor", self.owner))
        stack.enter_context(mock.patch.object(supervisor, "LOCK_PATH", Path(directory) / "tracker.lock"))
        stack.enter_context(mock.patch.dict(sys.modules, {
            "app.models.plugin": SimpleNamespace(InstalledPlugin=installed_plugins.c),
            "bambulabs_api": SimpleNamespace(PrinterMQTTClient=FakeMQTT),
        }))
        printer = filaman.BambuPrinter(1, "Bambu", "printer.local", "serial", "code")
        stack.enter_context(mock.patch.object(filaman, "list_bambu_printers", return_value=[printer]))
        stack.enter_context(mock.patch.object(filaman, "resolve_spool_for_slot", side_effect=self.resolve_spool))
        stack.enter_context(mock.patch.object(filaman, "load_spool", side_effect=self.load_spool))
        stack.enter_context(mock.patch.object(filaman, "record_consumption", side_effect=self.record_consumption))
        stack.enter_context(mock.patch.object(PrinterListener, "_fetch_3mf", side_effect=self.fetch_3mf))
        self.consumptions = []
        self.slots = {"0-0": 7, "0-1": 8}

    def new_supervisor(self):
        owner = supervisor._Supervisor()
        owner.sessions = self.sessions
        owner.loop = asyncio.get_running_loop()
        return owner

    async def set_enabled(self, enabled):
        async with self.sessions() as db:
            await db.execute(installed_plugins.update().values(is_active=enabled))
            await db.commit()

    async def resolve_spool(self, db, printer_id, slot_index):
        return self.slots.get(slot_index)

    async def load_spool(self, db, spool_id):
        return SimpleNamespace(id=spool_id)

    async def record_consumption(self, db, spool, grams, at, note):
        await db.commit()
        self.consumptions.append((spool.id, grams))

    async def fetch_3mf(self, job):
        return PrintMetadata(filaments=[
            FilamentInfo(filament_id=1, material="PLA", used_g=10),
            FilamentInfo(filament_id=2, material="PLA", used_g=20),
        ]), None

    async def report(self, listener, state, subtask_id="job-1", **fields):
        await listener.handle_message({"print": {
            "gcode_state": state,
            "subtask_id": subtask_id,
            "subtask_name": f"{subtask_id}.3mf",
            "ams_mapping": [0, 1],
            **fields,
        }})

    async def start_job(self):
        await supervisor.refresh_printers()
        listener = self.owner.listeners[1]
        await self.report(listener, "IDLE")
        await self.report(listener, "RUNNING")
        self.assertIsNotNone(listener.current_print_id)
        return listener

    async def test_disabled_plugin_does_not_start_listeners(self):
        await self.set_enabled(False)
        await supervisor.refresh_printers()
        self.assertEqual(supervisor.watched_printers(), 0)

    async def test_disable_during_reconciliation_does_not_connect_a_new_listener(self):
        printers = await filaman.list_bambu_printers(None)

        async def list_printers(db):
            await self.set_enabled(False)
            return printers

        with (
            mock.patch.object(filaman, "list_bambu_printers", side_effect=list_printers),
            mock.patch.object(FakeMQTT, "connect") as connect,
        ):
            await supervisor.refresh_printers()

        connect.assert_not_called()
        self.assertEqual(supervisor.watched_printers(), 0)

    async def test_mqtt_starts_before_releasing_the_startup_transaction(self):
        actions = []

        class DisableAfterCommit(AsyncSession):
            async def commit(session):
                await super().commit()
                await self.set_enabled(False)
                actions.append("disabled")

        listener = self.startup_listener(DisableAfterCommit)
        with mock.patch.object(FakeMQTT, "start", side_effect=lambda: actions.append("started")):
            await listener.start()

        self.assertEqual(actions, ["started", "disabled"])

    async def test_mqtt_stops_if_startup_commit_fails(self):
        class FailingCommit(AsyncSession):
            async def commit(self):
                raise RuntimeError("commit failed")

        listener = self.startup_listener(FailingCommit)
        client = FakeMQTT("host", "code", "serial")
        with mock.patch("bambulabs_api.PrinterMQTTClient", return_value=client) as factory:
            with self.assertRaisesRegex(RuntimeError, "commit failed"):
                await listener.start()

        factory.assert_called_once()
        self.assertFalse(client.running)
        self.assertIsNone(listener.client)
        async with self.sessions() as db:
            self.assertEqual(await store.list_printer_status(db), [])

    def startup_listener(self, session_class):
        return PrinterListener(
            1, "Bambu", "printer.local", "serial", "code",
            async_sessionmaker(self.engine, class_=session_class), self.owner.loop,
        )

    async def test_reconciliation_disconnects_listener_and_keeps_printer_settings(self):
        listener = await self.start_job()
        client = listener.client
        await self.set_enabled(False)
        await supervisor.refresh_printers()

        self.assertEqual(supervisor.watched_printers(), 0)
        self.assertFalse(client.running)
        async with self.sessions() as db:
            self.assertTrue((await settings.load_settings(db, 1)).tracking_enabled)

    async def test_finish_while_disabled_is_not_booked_then_or_after_resume(self):
        listener = await self.start_job()
        print_id = listener.current_print_id
        await self.set_enabled(False)
        await self.report(listener, "FINISH")
        self.assertEqual(self.consumptions, [])

        await self.set_enabled(True)
        await supervisor.refresh_printers()
        listener = self.owner.listeners[1]
        await self.report(listener, "RUNNING")
        await self.report(listener, "FINISH")
        async with self.sessions() as db:
            interrupted = await store.get_print(db, print_id)
            self.assertEqual(interrupted.status, models.STATUS_INCOMPLETE)
            self.assertIsNone(await store.find_open_print(db, 1))
        self.assertEqual(self.consumptions, [])

        await self.report(listener, "RUNNING", "job-2")
        await self.report(listener, "FINISH", "job-2")
        await self.report(listener, "FINISH", "job-2")
        self.assertEqual(self.consumptions, [(7, 10.0), (8, 20.0)])

    async def test_resumed_report_preserves_interrupted_filament_history(self):
        self.slots.pop("0-1")
        listener = await self.start_job()
        print_id = listener.current_print_id
        await self.set_enabled(False)
        await supervisor.refresh_printers()

        async with self.sessions() as db:
            interrupted = await store.get_print(db, print_id)
            original_rows = await store.list_filaments(db, print_id)
        self.assertEqual(interrupted.status, models.STATUS_INCOMPLETE)
        self.assertEqual([row.spool_id for row in original_rows], [7, None])

        await self.set_enabled(True)
        await supervisor.refresh_printers()
        listener = self.owner.listeners[1]
        await self.report(listener, "RUNNING")
        self.slots.update({"0-0": 9, "0-1": 8})
        await self.report(listener, "RUNNING", mc_percent=50)

        async with self.sessions() as db:
            self.assertEqual(await store.get_print(db, print_id), interrupted)
            self.assertEqual(await store.list_filaments(db, print_id), original_rows)
            self.assertEqual(len(await store.list_prints(db)), 1)

    async def test_disable_closes_an_already_incomplete_print(self):
        self.slots.pop("0-1")
        await supervisor.refresh_printers()
        await self.report(self.owner.listeners[1], "RUNNING")
        print_id = self.owner.listeners[1].current_print_id

        await self.set_enabled(False)
        await supervisor.refresh_printers()
        await self.set_enabled(True)
        await supervisor.refresh_printers()
        await self.report(self.owner.listeners[1], "RUNNING")
        self.slots["0-1"] = 8
        await self.report(self.owner.listeners[1], "RUNNING", mc_percent=50)

        async with self.sessions() as db:
            interrupted = await store.get_print(db, print_id)
            self.assertEqual(interrupted.status, models.STATUS_INCOMPLETE)
            self.assertIsNotNone(interrupted.finished_at)
            rows = await store.list_filaments(db, print_id)
            self.assertEqual([row.spool_id for row in rows], [7, None])

    async def test_inflight_3mf_fetch_does_not_write_after_disable(self):
        await supervisor.refresh_printers()
        listener = self.owner.listeners[1]
        await self.report(listener, "IDLE")

        async def download(job):
            await self.set_enabled(False)
            return await self.fetch_3mf(job)

        with mock.patch.object(listener, "_fetch_3mf", side_effect=download):
            await self.report(listener, "RUNNING")

        async with self.sessions() as db:
            self.assertEqual(await store.list_prints(db), [])

    async def test_reenabled_plugin_does_not_revive_a_stopped_listener(self):
        await supervisor.refresh_printers()
        listener = self.owner.listeners[1]
        await self.report(listener, "IDLE")

        async def download(job):
            await self.set_enabled(False)
            await supervisor.refresh_printers()
            await self.set_enabled(True)
            await supervisor.refresh_printers()
            replacement = self.owner.listeners[1]
            await self.report(replacement, "IDLE", "job-2")
            await self.report(replacement, "RUNNING", "job-2")
            return await self.fetch_3mf(job)

        with mock.patch.object(listener, "_fetch_3mf", side_effect=download):
            await self.report(listener, "RUNNING")

        async with self.sessions() as db:
            history = await store.list_prints(db)
            self.assertEqual([(row.subtask_id, row.status) for row in history], [
                ("job-2", models.STATUS_RUNNING),
            ])

    async def test_disable_during_finish_before_the_first_booking(self):
        listener = await self.start_job()
        print_id = listener.current_print_id

        async def load_spool(db, spool_id):
            await self.set_enabled(False)
            return await self.load_spool(db, spool_id)

        with mock.patch.object(filaman, "load_spool", side_effect=load_spool):
            await self.report(listener, "FINISH")

        self.assertEqual(self.consumptions, [])
        async with self.sessions() as db:
            rows = await store.list_filaments(db, print_id)
            self.assertEqual([row.spent_at for row in rows], [None, None])

        await self.set_enabled(True)
        await supervisor.refresh_printers()
        listener = self.owner.listeners[1]
        await self.report(listener, "RUNNING")
        await self.report(listener, "FINISH")
        self.assertEqual(self.consumptions, [])

    async def test_disable_after_a_booking_preserves_that_booking(self):
        listener = await self.start_job()
        print_id = listener.current_print_id

        async def record_then_disable(db, spool, grams, at, note):
            await self.record_consumption(db, spool, grams, at, note)
            await self.set_enabled(False)

        with mock.patch.object(filaman, "record_consumption", side_effect=record_then_disable):
            await self.report(listener, "FINISH")

        self.assertEqual(self.consumptions, [(7, 10.0)])
        async with self.sessions() as db:
            rows = await store.list_filaments(db, print_id)
            self.assertEqual([row.spent_grams for row in rows], [10.0, None])
            self.assertFalse((await store.get_print(db, print_id)).spent)

    async def test_disable_after_the_last_booking_keeps_print_marked_spent(self):
        listener = await self.start_job()
        print_id = listener.current_print_id

        async def record_then_disable(db, spool, grams, at, note):
            await self.record_consumption(db, spool, grams, at, note)
            if spool.id == 8:
                await self.set_enabled(False)

        with mock.patch.object(filaman, "record_consumption", side_effect=record_then_disable):
            await self.report(listener, "FINISH")

        self.assertEqual(self.consumptions, [(7, 10.0), (8, 20.0)])
        async with self.sessions() as db:
            self.assertTrue((await store.get_print(db, print_id)).spent)

    async def test_disabled_report_does_not_split_filament_or_publish_progress(self):
        listener = await self.start_job()
        print_id = listener.current_print_id
        await self.set_enabled(False)
        self.slots["0-0"] = 9
        await self.report(listener, "RUNNING", mc_percent=50)
        await listener.publish_status()

        async with self.sessions() as db:
            rows = await store.list_filaments(db, print_id)
            self.assertEqual([row.spool_id for row in rows], [7, 8])
            status = await store.list_printer_status(db)
            self.assertTrue(all(row.progress_percent is None for row in status))

    async def test_disabled_worker_takeover_keeps_the_old_print_unbookable(self):
        self.assertTrue(supervisor._acquire_lock())
        self.addCleanup(self.owner.lock_handle.close)
        listener = await self.start_job()
        print_id = listener.current_print_id

        successor = self.new_supervisor()
        with mock.patch.object(supervisor, "_supervisor", successor):
            self.assertFalse(supervisor._acquire_lock())
            self.owner.lock_handle.close()
            self.assertTrue(supervisor._acquire_lock())
            self.addCleanup(successor.lock_handle.close)
            await self.set_enabled(False)
            await supervisor.refresh_printers()
            self.assertEqual(supervisor.watched_printers(), 0)

            await self.set_enabled(True)
            await supervisor.refresh_printers()
            await self.report(successor.listeners[1], "RUNNING")
            await self.report(successor.listeners[1], "FINISH")

        async with self.sessions() as db:
            interrupted = await store.get_print(db, print_id)
            self.assertEqual(interrupted.status, models.STATUS_INCOMPLETE)
        self.assertEqual(self.consumptions, [])
