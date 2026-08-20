"""Tests for the booking path, the one thing that must never break.

The database is real, an in-memory SQLite built by the same ensure_tables() the
plugin uses in FilaMan. FilaMan itself is replaced by fakes, because everything
this plugin reads or writes over there goes through filaman.py, which is exactly
what that module exists for.

The consumption fake commits the session, like the real SpoolService does. That
is not decoration: the order of marking and booking in service.spend_print only
makes sense against a service that commits, and a fake that does not would let a
broken order pass.

Needs the optional test dependencies, see tests/_support.py.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from bambu_usage import filaman, service
from bambu_usage.threemf import FilamentInfo, PrintMetadata

from ._support import HAS_TEST_DEPENDENCIES

if HAS_TEST_DEPENDENCIES:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from bambu_usage import models, store

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

AUTO = SimpleNamespace(auto_spend=True, spend_on_cancel=False)
MANUAL = SimpleNamespace(auto_spend=False, spend_on_cancel=False)
AUTO_WITH_CANCEL = SimpleNamespace(auto_spend=True, spend_on_cancel=True)


@unittest.skipUnless(HAS_TEST_DEPENDENCIES, "needs sqlalchemy, aiosqlite and pydantic")
class BookingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        self.addAsyncCleanup(self.engine.dispose)
        await models.ensure_tables(self.engine)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        # slot_index -> spool id, as PrinterSlotAssignment would have it.
        self.slots: dict[str, int] = {}
        # spool id -> spool, so a deleted spool is simply one that is missing.
        self.spools: dict[int, SimpleNamespace] = {}
        self.consumptions: list[tuple[int, float, str | None]] = []
        self.adjustments: list[tuple[int, float]] = []

        for name, replacement in (
            ("resolve_spool_for_slot", self.fake_resolve_spool),
            ("load_spool", self.fake_load_spool),
            ("record_consumption", self.fake_record_consumption),
            ("record_adjustment", self.fake_record_adjustment),
        ):
            patcher = mock.patch.object(filaman, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def fake_resolve_spool(self, db, printer_id, slot_index):
        return self.slots.get(slot_index)

    async def fake_load_spool(self, db, spool_id):
        return self.spools.get(spool_id)

    async def fake_record_consumption(self, db, spool, grams, event_at, note=None):
        self.consumptions.append((spool.id, round(grams, 3), note))
        await db.commit()

    async def fake_record_adjustment(self, db, spool, delta_grams, event_at, note=None):
        self.adjustments.append((spool.id, round(delta_grams, 3)))
        await db.commit()

    def have_spools(self, *spool_ids):
        for spool_id in spool_ids:
            self.spools[spool_id] = SimpleNamespace(id=spool_id)

    def two_filaments(self):
        return PrintMetadata(
            plate_id=1,
            filaments=[
                FilamentInfo(filament_id=1, material="PLA", used_g=41.2, used_m=12.34),
                FilamentInfo(filament_id=2, material="PETG", used_g=12.5, used_m=3.75),
            ],
            thumbnail=b"PNGDATA",
            thumbnail_mime="image/png",
        )

    async def start(self, db, ams_mapping, subtask_id="task-1", metadata=None):
        return await service.start_print(
            db,
            printer_id=1,
            file_name="cube.3mf",
            print_type="cloud",
            metadata=metadata or self.two_filaments(),
            ams_mapping=ams_mapping,
            subtask_id=subtask_id,
            started_at=NOW,
        )

    async def test_a_starting_print_resolves_its_spools(self):
        self.slots = {"0-0": 7, "0-1": 8}

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            rows = await store.list_filaments(db, print_id)

        self.assertEqual([row.slot_index for row in rows], ["0-0", "0-1"])
        self.assertEqual([row.spool_id for row in rows], [7, 8])
        self.assertEqual([row.estimated_grams for row in rows], [41.2, 12.5])
        # Nothing is booked at the start, only recorded.
        self.assertEqual(self.consumptions, [])

    async def test_the_same_message_twice_creates_one_print(self):
        async with self.sessions() as db:
            first = await self.start(db, [0, 1])
            second = await self.start(db, [0, 1])

            self.assertEqual(first, second)
            self.assertEqual(len(await store.list_prints(db)), 1)
            self.assertEqual(len(await store.list_filaments(db, first)), 2)

    async def test_two_filaments_on_one_spool_are_booked_once(self):
        # The multi colour case: one spool, one event, the sum of both rows.
        self.slots = {"0-0": 7, "0-1": 7}
        self.have_spools(7)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

            self.assertEqual(self.consumptions, [(7, 53.7, "cube.3mf")])

            record = await store.get_print(db, print_id)
            self.assertEqual(record.status, models.STATUS_FINISHED)
            self.assertTrue(record.spent)
            rows = await store.list_filaments(db, print_id)
            self.assertEqual([row.spent_grams for row in rows], [41.2, 12.5])

    async def test_nothing_is_booked_twice(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)
            booked_again = await service.spend_print(db, print_id)

        self.assertEqual(len(self.consumptions), 2)
        self.assertEqual(booked_again, {})

    async def test_a_print_without_a_resolvable_slot_stays_open(self):
        # No assignment, no guess. The row waits for a human.
        self.slots = {}

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

            record = await store.get_print(db, print_id)
            self.assertEqual(self.consumptions, [])
            # Booking nothing is not the same as being booked, or the one case
            # the history exists to surface would look settled.
            self.assertFalse(record.spent)

    async def test_an_abort_is_not_booked(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FAILED, AUTO, NOW)

            self.assertEqual(self.consumptions, [])
            self.assertEqual((await store.get_print(db, print_id)).status, models.STATUS_FAILED)

    async def test_an_abort_is_booked_when_the_setting_says_so(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FAILED, AUTO_WITH_CANCEL, NOW)

        self.assertEqual(len(self.consumptions), 2)

    async def test_auto_spend_off_records_everything_and_books_nothing(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, MANUAL, NOW)

            self.assertEqual(self.consumptions, [])
            self.assertFalse((await store.get_print(db, print_id)).spent)

            # The history offers the button, and this is what it does.
            booked = await service.spend_print(db, print_id)
            self.assertEqual(booked, {7: 41.2, 8: 12.5})
            self.assertTrue((await store.get_print(db, print_id)).spent)

    async def test_a_spool_assigned_afterwards_can_be_booked_right_away(self):
        self.slots = {"0-0": 7}
        self.have_spools(7, 9)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)
            self.assertEqual(self.consumptions, [(7, 41.2, "cube.3mf")])

            open_row = [row for row in await store.list_filaments(db, print_id) if not row.spool_id]
            await service.assign_spool(db, open_row[0].id, 9, spend_now=True)

            self.assertEqual(self.consumptions[-1], (9, 12.5, "cube.3mf"))
            self.assertTrue((await store.get_print(db, print_id)).spent)

    async def test_assigning_without_booking_leaves_the_print_open(self):
        self.slots = {"0-0": 7}
        self.have_spools(7, 9)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

            open_row = [row for row in await store.list_filaments(db, print_id) if not row.spool_id]
            await service.assign_spool(db, open_row[0].id, 9, spend_now=False)

            self.assertEqual(len(self.consumptions), 1)
            self.assertFalse((await store.get_print(db, print_id)).spent)

    async def test_correcting_upwards_consumes_the_difference(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

            row = (await store.list_filaments(db, print_id))[0]
            await service.correct_usage(db, row.id, 50.0)

            self.assertEqual(self.consumptions[-1], (7, 8.8, "correction"))
            corrected = await store.get_filament(db, row.id)
            self.assertEqual(corrected.spent_grams, 50.0)
            self.assertTrue(corrected.manual_override)
            self.assertEqual(corrected.estimated_grams, 41.2)

    async def test_correcting_downwards_gives_material_back(self):
        # record_consumption can only ever deduct, so the way back is an
        # adjustment. Getting this wrong would silently deduct twice.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

            row = (await store.list_filaments(db, print_id))[0]
            await service.correct_usage(db, row.id, 30.0)

            self.assertEqual(self.adjustments, [(7, 11.2)])
            self.assertEqual((await store.get_filament(db, row.id)).spent_grams, 30.0)

    async def test_correcting_to_the_same_amount_moves_nothing(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)
            booked = len(self.consumptions)

            row = (await store.list_filaments(db, print_id))[0]
            await service.correct_usage(db, row.id, 41.2)

            self.assertEqual(len(self.consumptions), booked)
            self.assertEqual(self.adjustments, [])

    async def test_a_row_without_a_spool_cannot_be_corrected(self):
        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            row = (await store.list_filaments(db, print_id))[0]

            with self.assertRaises(service.UsageError):
                await service.correct_usage(db, row.id, 30.0)

    async def test_a_deleted_spool_does_not_stop_the_others(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(8)  # spool 7 was deleted in FilaMan in the meantime

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            with self.assertLogs("bambu_usage.service", level="WARNING"):
                await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

            self.assertEqual(self.consumptions, [(8, 12.5, "cube.3mf")])
            # The print stays open, because one row is still waiting.
            self.assertFalse((await store.get_print(db, print_id)).spent)

    async def test_the_history_carries_the_breakdown(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

            history = await service.get_history(db)

        self.assertEqual(len(history), 1)
        record = history[0]
        self.assertEqual(record.id, print_id)
        self.assertEqual(record.file_name, "cube.3mf")
        self.assertTrue(record.has_thumbnail)
        self.assertEqual([usage.spool_id for usage in record.filaments], [7, 8])
        self.assertEqual([usage.spent_grams for usage in record.filaments], [41.2, 12.5])

    async def test_the_preview_is_served_from_the_database(self):
        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            self.assertEqual(await service.get_thumbnail(db, print_id), (b"PNGDATA", "image/png"))

    async def test_booking_a_print_that_does_not_exist(self):
        async with self.sessions() as db:
            with self.assertRaises(service.UsageError):
                await service.spend_print(db, 999)


if __name__ == "__main__":
    unittest.main()
