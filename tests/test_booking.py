"""Tests for the booking path, the one thing that must never break.

The database is real, an in-memory SQLite built by the same ensure_tables() the
plugin uses in FilaMan. FilaMan itself is replaced by fakes, because everything
this plugin reads or writes over there goes through filaman.py, which is exactly
what that module exists for.

The read side lives in views.py and is exercised from here as well, because what
the history shows only means anything against a booking that really happened.

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

from bambu_usage import filaman, service, views
from bambu_usage.threemf import FilamentInfo, PrintMetadata

from ._support import HAS_TEST_DEPENDENCIES

if HAS_TEST_DEPENDENCIES:
    from sqlalchemy.exc import InvalidRequestError
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
        # rfid tag -> spool id, as FilaMan's spool table would have it.
        self.tags: dict[str, int] = {}
        # spool id -> spool, so a deleted spool is simply one that is missing.
        self.spools: dict[int, SimpleNamespace] = {}
        self.consumptions: list[tuple[int, float, str | None]] = []
        self.adjustments: list[tuple[int, float]] = []
        # spool id -> price of one gram, empty unless a test says otherwise.
        self.prices: dict[int, float] = {}
        # Spools whose booking refuses, to exercise the path that skips one and
        # carries on with the rest.
        self.refuse: set[int] = set()

        for name, replacement in (
            ("resolve_spool_for_slot", self.fake_resolve_spool),
            ("find_spool_by_rfid", self.fake_find_by_rfid),
            ("load_spool", self.fake_load_spool),
            ("record_consumption", self.fake_record_consumption),
            ("record_adjustment", self.fake_record_adjustment),
            ("load_spool_prices", self.fake_load_spool_prices),
        ):
            patcher = mock.patch.object(filaman, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def fake_resolve_spool(self, db, printer_id, slot_index):
        return self.slots.get(slot_index)

    async def fake_find_by_rfid(self, db, uid):
        # Case insensitive, like the query it stands in for.
        return next((s for tag, s in self.tags.items() if tag.lower() == uid.lower()), None)

    async def fake_load_spool(self, db, spool_id):
        return self.spools.get(spool_id)

    async def fake_record_consumption(self, db, spool, grams, event_at, note=None):
        if spool.id in self.refuse:
            # What a lazily loaded relationship answers with in an async session,
            # and the class of error the booking loop is built to survive.
            raise InvalidRequestError("greenlet_spawn has not been called")

        self.consumptions.append((spool.id, round(grams, 3), note))
        await db.commit()

    async def fake_record_adjustment(self, db, spool, delta_grams, event_at, note=None):
        self.adjustments.append((spool.id, round(delta_grams, 3)))
        await db.commit()

    async def fake_load_spool_prices(self, db, spool_ids):
        return {spool_id: self.prices[spool_id] for spool_id in spool_ids if spool_id in self.prices}

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

    async def start(self, db, ams_mapping, subtask_id="task-1", metadata=None, tray_tags=None):
        return await service.start_print(
            db,
            printer_id=1,
            file_name="cube.3mf",
            print_type="cloud",
            metadata=metadata or self.two_filaments(),
            ams_mapping=ams_mapping,
            subtask_id=subtask_id,
            started_at=NOW,
            tray_tags=tray_tags,
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

    async def test_a_starting_print_keeps_what_the_slicer_said(self):
        # Shown in the history and nowhere else, so nothing depends on it, but
        # a print that loses it cannot get it back: the 3MF is gone by then.
        metadata = self.two_filaments()
        metadata.estimated_seconds = 7412
        metadata.object_count = 3
        metadata.nozzle_diameter = 0.4

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1], metadata=metadata)
            record = await store.get_print(db, print_id)

        self.assertEqual(record.estimated_seconds, 7412)
        self.assertEqual(record.object_count, 3)
        self.assertAlmostEqual(record.nozzle_diameter, 0.4)

    async def test_a_slot_without_an_assignment_is_resolved_by_its_tag(self):
        # FilaMan's Bambu Lab driver keeps a tray's type and colour but not its
        # uuid, so nothing over there can match the tag to the spool carrying
        # it. Without this every print would arrive with nothing assigned.
        self.slots = {}
        self.tags = {"841CCD522B68431BB6ED54893395307B": 25}

        async with self.sessions() as db:
            print_id = await self.start(
                db, [0, 1], tray_tags={"0-0": "841CCD522B68431BB6ED54893395307B"}
            )
            rows = await store.list_filaments(db, print_id)

        self.assertEqual([row.spool_id for row in rows], [25, None])

    async def test_the_tag_is_matched_whatever_the_case(self):
        # The printer reports upper case; what is stored is whatever the person
        # who entered it typed.
        self.tags = {"841ccd522b68431bb6ed54893395307b": 25}

        async with self.sessions() as db:
            print_id = await self.start(
                db, [0], tray_tags={"0-0": "841CCD522B68431BB6ED54893395307B"}
            )
            rows = await store.list_filaments(db, print_id)

        self.assertEqual(rows[0].spool_id, 25)

    async def test_filamans_own_assignment_wins_over_the_tag(self):
        # A person or the driver put that spool there. A tag read off a printer
        # does not overrule it.
        self.slots = {"0-0": 7}
        self.tags = {"TAG": 25}

        async with self.sessions() as db:
            print_id = await self.start(db, [0], tray_tags={"0-0": "TAG"})
            rows = await store.list_filaments(db, print_id)

        self.assertEqual(rows[0].spool_id, 7)

    async def test_a_tag_nobody_carries_leaves_the_row_open(self):
        # Not guessed at. The row waits for somebody to assign it.
        self.tags = {"SOMETHING ELSE": 25}

        async with self.sessions() as db:
            print_id = await self.start(db, [0], tray_tags={"0-0": "TAG"})
            rows = await store.list_filaments(db, print_id)

        self.assertIsNone(rows[0].spool_id)

    async def test_the_row_records_where_its_spool_came_from(self):
        # Three ways lead to a spool, and afterwards nobody can tell which it
        # was. The tag is the one that surprises: a spool turns up that nobody
        # assigned.
        self.slots = {"0-0": 7}
        self.tags = {"TAG": 25}

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1], tray_tags={"0-1": "TAG"})
            rows = await store.list_filaments(db, print_id)

        self.assertEqual(
            [(row.spool_id, row.spool_source) for row in rows],
            [(7, models.SPOOL_FROM_FILAMAN), (25, models.SPOOL_FROM_TAG)],
        )

    async def test_a_row_nobody_could_resolve_records_no_source(self):
        async with self.sessions() as db:
            print_id = await self.start(db, [0])
            rows = await store.list_filaments(db, print_id)

        self.assertIsNone(rows[0].spool_id)
        self.assertIsNone(rows[0].spool_source)

    async def test_assigning_by_hand_says_so_without_touching_the_amount(self):
        # These used to be one flag, and the card then claimed "corrected by
        # hand" about a row where only the spool had been picked.
        self.have_spools(9)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            row = (await store.list_filaments(db, print_id))[0]
            await service.assign_spool(db, row.id, 9, spend_now=False)

            assigned = await store.get_filament(db, row.id)

        self.assertEqual(assigned.spool_source, models.SPOOL_FROM_HAND)
        self.assertFalse(assigned.manual_override)

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

    async def test_an_abort_books_the_share_it_got_through(self):
        # Half the layers, half the estimate. Booking the full amount is what
        # OpenSpoolMan gets wrong, booking nothing leaves the spool wrong.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(
                db, print_id, models.STATUS_FAILED, AUTO_WITH_CANCEL, NOW, completed_fraction=0.5
            )

            rows = await store.list_filaments(db, print_id)

        self.assertEqual(sorted(self.consumptions), [(7, 20.6, "cube.3mf"), (8, 6.25, "cube.3mf")])
        # The slicer estimate stays untouched, only what was booked is the share.
        self.assertEqual([row.estimated_grams for row in rows], [41.2, 12.5])
        self.assertEqual([row.spent_grams for row in rows], [20.6, 6.25])

    async def test_an_abort_books_what_the_gcode_says_not_what_the_layers_suggest(self):
        # Filament 2 is only laid down in the last layer. Stopping at layer two
        # of three costs none of it, while the linear share would have charged
        # two thirds. This is the difference the plate gcode is read for.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        metadata = self.two_filaments()
        metadata.layer_shares = {1: [0.5, 0.8, 1.0], 2: [0.0, 0.0, 1.0]}

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1], metadata=metadata)
            await service.finish_print(
                db,
                print_id,
                models.STATUS_FAILED,
                AUTO_WITH_CANCEL,
                NOW,
                completed_fraction=0.66,
                stopped_at_layer=2,
            )

        # 80 per cent of 41.2 for the one, nothing at all for the other.
        self.assertEqual(self.consumptions, [(7, 32.96, "cube.3mf")])

    async def test_without_a_curve_the_linear_share_still_applies(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(
                db, print_id, models.STATUS_FAILED, AUTO_WITH_CANCEL, NOW,
                completed_fraction=0.5, stopped_at_layer=2,
            )

        self.assertEqual(sorted(self.consumptions), [(7, 20.6, "cube.3mf"), (8, 6.25, "cube.3mf")])

    async def test_an_abort_without_a_known_share_books_nothing(self):
        # A printer that never reported its progress leaves us guessing, and a
        # guess on a spool is worse than a row somebody can correct by hand.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FAILED, AUTO_WITH_CANCEL, NOW)

            self.assertEqual(self.consumptions, [])
            self.assertFalse((await store.get_print(db, print_id)).spent)

    async def test_the_share_survives_for_a_later_booking_by_hand(self):
        # auto_spend off: the history offers the button, and it has to book the
        # same share the automatic path would have.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(
                db, print_id, models.STATUS_FAILED, MANUAL, NOW, completed_fraction=0.25
            )
            booked = await service.spend_print(db, print_id)

        self.assertEqual(booked, {7: 10.3, 8: 3.125})

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

    async def test_moving_a_booked_row_moves_its_booking_too(self):
        # Changing only the label would leave the consumption on a spool that
        # never printed it, and both spools would be wrong from then on.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8, 9)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)
            self.assertEqual(sorted(self.consumptions), [(7, 41.2, "cube.3mf"), (8, 12.5, "cube.3mf")])

            booked = (await store.list_filaments(db, print_id))[0]
            await service.assign_spool(db, booked.id, 9)

            moved = await store.get_filament(db, booked.id)

        # Charged where it belongs now, given back where it does not.
        self.assertEqual(self.consumptions[-1], (9, 41.2, "moved from spool 7"))
        self.assertEqual(self.adjustments, [(7, 41.2)])
        self.assertEqual(moved.spool_id, 9)
        # What was booked does not change by moving it.
        self.assertEqual(moved.spent_grams, 41.2)

    async def test_moving_to_the_same_spool_does_nothing(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)
            booked = (await store.list_filaments(db, print_id))[0]

            await service.assign_spool(db, booked.id, 7)

        self.assertEqual(self.adjustments, [])
        self.assertEqual(len(self.consumptions), 2)

    async def test_an_unbooked_row_is_only_reassigned(self):
        # Nothing has moved on any spool yet, so nothing has to move back.
        self.slots = {"0-0": 7}
        self.have_spools(7, 9)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            row = (await store.list_filaments(db, print_id))[0]

            await service.assign_spool(db, row.id, 9, spend_now=False)
            moved = await store.get_filament(db, row.id)

        self.assertEqual(moved.spool_id, 9)
        self.assertEqual(self.consumptions, [])
        self.assertEqual(self.adjustments, [])

    async def test_a_booked_row_cannot_be_moved_to_nothing(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)
            booked = (await store.list_filaments(db, print_id))[0]

            with self.assertRaises(service.UsageError):
                await service.assign_spool(db, booked.id, None)

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

    async def test_a_print_somebody_stopped_is_not_called_broken(self):
        # The printer leaves a code behind after a fault and none after a stop.
        # Without this every cancelled print looked like a defective machine.
        self.slots = {"0-0": 7}
        self.have_spools(7)

        async with self.sessions() as db:
            print_id = await self.start(db, [0])
            await service.finish_print(
                db, print_id, models.STATUS_CANCELLED, AUTO_WITH_CANCEL, NOW,
                completed_fraction=0.5, stopped_at_layer=2, printer_error_code=None,
            )
            record = await store.get_print(db, print_id)

        self.assertEqual(record.status, models.STATUS_CANCELLED)
        self.assertIsNone(record.printer_error_code)

    async def test_a_fault_keeps_its_code(self):
        # Stored because the rule that reads it is an assumption: the first real
        # fault shows whether it stands the right way round.
        self.slots = {"0-0": 7}
        self.have_spools(7)

        async with self.sessions() as db:
            print_id = await self.start(db, [0])
            await service.finish_print(
                db, print_id, models.STATUS_FAILED, AUTO_WITH_CANCEL, NOW,
                completed_fraction=0.5, stopped_at_layer=2, printer_error_code=50348044,
            )
            record = await store.get_print(db, print_id)

        self.assertEqual(record.status, models.STATUS_FAILED)
        self.assertEqual(record.printer_error_code, 50348044)

    async def test_a_running_print_is_not_booked(self):
        # It has laid down some share nobody knows the end of yet. Booking it
        # would charge the full estimate for material still on the spool.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])

            with self.assertRaises(service.UsageError):
                await service.spend_print(db, print_id)

        self.assertEqual(self.consumptions, [])

    async def test_a_running_print_cannot_be_corrected_either(self):
        self.slots = {"0-0": 7}
        self.have_spools(7)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            row = (await store.list_filaments(db, print_id))[0]

            with self.assertRaises(service.UsageError):
                await service.correct_usage(db, row.id, 12.0)

        self.assertEqual(self.consumptions, [])
        self.assertEqual(self.adjustments, [])

    async def test_an_abort_gives_back_what_was_booked_too_early(self):
        # A spool assigned by hand mid print books the full estimate. When the
        # print is then cancelled, that row is the one nobody would look at
        # again, and the spool would keep material it never laid down.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        metadata = self.two_filaments()
        metadata.layer_shares = {1: [0.5, 0.8, 1.0], 2: [0.0, 0.0, 1.0]}

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1], metadata=metadata)
            row = (await store.list_filaments(db, print_id))[0]

            # What an early booking leaves behind: the full estimate, spent.
            await store.mark_filament_spent(db, row.id, 41.2, NOW)
            await db.commit()

            await service.finish_print(
                db, print_id, models.STATUS_CANCELLED, AUTO_WITH_CANCEL, NOW,
                completed_fraction=0.66, stopped_at_layer=2,
            )

            corrected = await store.get_filament(db, row.id)

        # 80 per cent of 41.2 is what layer two had used, so 20 per cent comes
        # back. The other row books normally, at zero, because its filament is
        # only laid down in the last layer.
        self.assertAlmostEqual(corrected.spent_grams, 41.2 * 0.8)
        self.assertEqual(len(self.adjustments), 1)
        self.assertEqual(self.adjustments[0][0], 7)
        self.assertAlmostEqual(self.adjustments[0][1], 41.2 * 0.2)

    async def test_an_abort_leaves_a_hand_corrected_row_alone(self):
        # Somebody weighed it. No recomputation overrules that.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        metadata = self.two_filaments()
        metadata.layer_shares = {1: [0.5, 0.8, 1.0], 2: [0.0, 0.0, 1.0]}

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1], metadata=metadata)
            row = (await store.list_filaments(db, print_id))[0]
            await store.override_filament_amount(db, row.id, 30.0, NOW)
            await db.commit()
            self.adjustments.clear()
            self.consumptions.clear()

            await service.finish_print(
                db, print_id, models.STATUS_CANCELLED, AUTO_WITH_CANCEL, NOW,
                completed_fraction=0.66, stopped_at_layer=2,
            )

            untouched = await store.get_filament(db, row.id)

        self.assertEqual(untouched.spent_grams, 30.0)
        self.assertEqual(self.adjustments, [])

    async def test_deleting_a_print_takes_its_filament_rows_with_it(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

            await service.forget_print(db, print_id)

            self.assertIsNone(await store.get_print(db, print_id))
            self.assertEqual(await store.list_filaments(db, print_id), [])

        # The material was really used, so it stays booked. Tidying a list is
        # not a reason for a spool to refill itself.
        self.assertEqual(sorted(self.consumptions), [(7, 41.2, "cube.3mf"), (8, 12.5, "cube.3mf")])
        self.assertEqual(self.adjustments, [])

    async def test_deleting_a_print_that_is_not_there_says_so(self):
        async with self.sessions() as db:
            with self.assertRaises(service.UsageError):
                await service.forget_print(db, 999)

    async def test_the_history_carries_the_breakdown(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

            history = await views.get_history(db)

        self.assertEqual(len(history), 1)
        record = history[0]
        self.assertEqual(record.id, print_id)
        self.assertEqual(record.file_name, "cube.3mf")
        self.assertTrue(record.has_thumbnail)
        self.assertEqual([usage.spool_id for usage in record.filaments], [7, 8])
        self.assertEqual([usage.spent_grams for usage in record.filaments], [41.2, 12.5])
        # No price on either spool, so no claim about what it cost.
        self.assertIsNone(record.cost)

    async def test_a_running_print_says_what_it_has_used_so_far(self):
        # Filament 2 is laid down only in the last layer. At layer two of three
        # the answer for it is zero, and the linear share would have claimed
        # two thirds: the same difference the booking of an abort turns on.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        metadata = self.two_filaments()
        metadata.layer_shares = {1: [0.5, 0.8, 1.0], 2: [0.0, 0.0, 1.0]}

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1], metadata=metadata)
            await store.upsert_printer_status(
                db,
                printer_id=1,
                printer_name="X1C",
                connected=True,
                tracking_enabled=True,
                updated_at=NOW,
                current_print_id=print_id,
                layer_num=2,
            )
            await db.commit()

            history = await views.get_history(db)

        # 80 per cent of 41.2 for the one, nothing at all for the other.
        self.assertEqual(
            [usage.used_so_far for usage in history[0].filaments], [41.2 * 0.8, 0.0]
        )
        # Nothing was booked by looking at it.
        self.assertEqual(self.consumptions, [])

    async def test_a_print_nobody_is_printing_has_no_used_so_far(self):
        # A finished print says what was booked; a second, differently computed
        # number beside it would only invite the question which one is true.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        metadata = self.two_filaments()
        metadata.layer_shares = {1: [0.5, 0.8, 1.0], 2: [0.0, 0.0, 1.0]}

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1], metadata=metadata)
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

            history = await views.get_history(db)

        self.assertEqual([usage.used_so_far for usage in history[0].filaments], [None, None])

    async def test_without_a_curve_a_running_print_claims_nothing(self):
        # The share of the layers is not the share of a filament, and a number
        # that looks like a measurement is worse than an empty field.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await store.upsert_printer_status(
                db,
                printer_id=1,
                printer_name="X1C",
                connected=True,
                tracking_enabled=True,
                updated_at=NOW,
                current_print_id=print_id,
                layer_num=2,
            )
            await db.commit()

            history = await views.get_history(db)

        self.assertEqual([usage.used_so_far for usage in history[0].filaments], [None, None])

    async def test_the_history_carries_what_the_print_cost(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)
        self.prices = {7: 0.025, 8: 0.04}

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

            history = await views.get_history(db)

        # 41.2 g at 2.5 cent plus 12.5 g at 4 cent.
        self.assertAlmostEqual(history[0].cost, 41.2 * 0.025 + 12.5 * 0.04)

    async def test_the_preview_is_served_from_the_database(self):
        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            self.assertEqual(await views.get_thumbnail(db, print_id), (b"PNGDATA", "image/png"))

    async def test_a_print_joined_in_the_middle_is_never_booked(self):
        # How much of it ran before this plugin was listening is unknowable, so
        # the estimate would overstate. It is recorded and left alone.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await service.start_print(
                db,
                printer_id=1,
                file_name="cube.3mf",
                print_type="cloud",
                metadata=self.two_filaments(),
                ams_mapping=[0, 1],
                subtask_id="task-1",
                started_at=NOW,
                status=models.STATUS_INCOMPLETE,
            )
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

            record = await store.get_print(db, print_id)

        self.assertEqual(self.consumptions, [])
        self.assertEqual(record.status, models.STATUS_INCOMPLETE)
        self.assertIsNotNone(record.finished_at)

    async def test_a_print_without_its_3mf_keeps_saying_so(self):
        # Relabelling it as finished would hide the one thing worth seeing.
        async with self.sessions() as db:
            print_id = await service.start_print(
                db,
                printer_id=1,
                file_name="cube.3mf",
                print_type="cloud",
                metadata=PrintMetadata(),
                ams_mapping=[],
                subtask_id="task-1",
                started_at=NOW,
                status=models.STATUS_NO_3MF,
            )
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

            self.assertEqual((await store.get_print(db, print_id)).status, models.STATUS_NO_3MF)

    async def test_such_a_print_can_still_be_booked_by_hand(self):
        # Automatic booking refuses, a human may still decide otherwise.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)

        async with self.sessions() as db:
            print_id = await service.start_print(
                db,
                printer_id=1,
                file_name="cube.3mf",
                print_type="cloud",
                metadata=self.two_filaments(),
                ams_mapping=[0, 1],
                subtask_id="task-1",
                started_at=NOW,
                status=models.STATUS_INCOMPLETE,
            )
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)
            booked = await service.spend_print(db, print_id)

        self.assertEqual(booked, {7: 41.2, 8: 12.5})

    async def test_a_spool_swapped_mid_print_is_charged_its_share(self):
        # The case OpenSpoolMan cannot express: one spool runs empty at 40 per
        # cent and is replaced. Charging either one for the whole print is wrong
        # in one direction or the other.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8, 9)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            first = (await store.list_filaments(db, print_id))[0]

            self.assertTrue(await service.split_filament_row(db, first.id, 0.4, 9))

            rows = await store.list_filaments(db, print_id)
            self.assertEqual(len(rows), 3)

            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

        # 41.2 g split at 40 per cent, and the second filament untouched.
        self.assertEqual(
            sorted(self.consumptions),
            [(7, 16.48, "cube.3mf"), (8, 12.5, "cube.3mf"), (9, 24.72, "cube.3mf")],
        )
        # What the two halves cost together is still the slicer estimate.
        self.assertAlmostEqual(16.48 + 24.72, 41.2, places=2)

    async def test_both_halves_keep_the_slot_and_the_material(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8, 9)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            first = (await store.list_filaments(db, print_id))[0]
            await service.split_filament_row(db, first.id, 0.4, 9)

            halves = [
                row for row in await store.list_filaments(db, print_id) if row.filament_id == 1
            ]

        self.assertEqual([row.spool_id for row in halves], [7, 9])
        self.assertEqual([row.slot_index for row in halves], ["0-0", "0-0"])
        self.assertEqual([row.material for row in halves], ["PLA", "PLA"])
        self.assertEqual([row.from_fraction for row in halves], [None, 0.4])
        self.assertEqual([row.to_fraction for row in halves], [0.4, None])
        self.assertAlmostEqual(halves[0].estimated_grams, 16.48, places=2)
        self.assertAlmostEqual(halves[1].estimated_grams, 24.72, places=2)

    async def test_a_row_can_be_split_more_than_once(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8, 9, 10)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            first = (await store.list_filaments(db, print_id))[0]
            await service.split_filament_row(db, first.id, 0.4, 9)

            # The second spool runs out too, at 70 per cent of the print.
            second = [
                row
                for row in await store.list_filaments(db, print_id)
                if row.filament_id == 1 and row.spool_id == 9
            ][0]
            await service.split_filament_row(db, second.id, 0.7, 10)

            thirds = [
                row for row in await store.list_filaments(db, print_id) if row.filament_id == 1
            ]

        self.assertEqual([row.spool_id for row in thirds], [7, 9, 10])
        # 40, 30 and 30 per cent of 41.2 g.
        self.assertAlmostEqual(thirds[0].estimated_grams, 16.48, places=2)
        self.assertAlmostEqual(thirds[1].estimated_grams, 12.36, places=2)
        self.assertAlmostEqual(thirds[2].estimated_grams, 12.36, places=2)

    async def test_a_booked_row_is_not_split_any_more(self):
        # The print is over as far as that row is concerned.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8, 9)

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

            booked = (await store.list_filaments(db, print_id))[0]
            self.assertFalse(await service.split_filament_row(db, booked.id, 0.4, 9))
            self.assertEqual(len(await store.list_filaments(db, print_id)), 2)

    async def test_a_refused_booking_lands_where_it_can_be_seen(self):
        # It used to live in the container log and nowhere else, which cost an
        # afternoon of guessing.
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)
        self.refuse = {7}

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            with self.assertLogs("bambu_usage.service", level="ERROR"):
                await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

            record = await store.get_print(db, print_id)

        self.assertIn("spool 7", record.error)
        self.assertFalse(record.spent)
        # The other spool went through regardless, which is the point of
        # surviving one failure.
        self.assertEqual(self.consumptions, [(8, 12.5, "cube.3mf")])

    async def test_a_booking_that_works_clears_the_old_warning(self):
        self.slots = {"0-0": 7, "0-1": 8}
        self.have_spools(7, 8)
        self.refuse = {7}

        async with self.sessions() as db:
            print_id = await self.start(db, [0, 1])
            with self.assertLogs("bambu_usage.service", level="ERROR"):
                await service.finish_print(db, print_id, models.STATUS_FINISHED, AUTO, NOW)

            self.refuse = set()
            await service.spend_print(db, print_id)

            record = await store.get_print(db, print_id)

        self.assertIsNone(record.error)
        self.assertTrue(record.spent)

    async def test_booking_a_print_that_does_not_exist(self):
        async with self.sessions() as db:
            with self.assertRaises(service.UsageError):
                await service.spend_print(db, 999)


if __name__ == "__main__":
    unittest.main()
