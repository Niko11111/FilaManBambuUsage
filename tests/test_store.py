"""Tests for the queries on the plugin's own tables.

These need SQLAlchemy and aiosqlite. Both are optional, so without them this
file skips itself and the rest of the suite still runs on the standard library
alone. See tests/_support.py and the note in pyproject.toml.

An in-memory database is built per test. It is the same schema the plugin
creates in FilaMan, because it is created by the same ensure_tables().
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from ._support import HAS_TEST_DEPENDENCIES

if HAS_TEST_DEPENDENCIES:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from bambu_usage import models, store

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


@unittest.skipUnless(HAS_TEST_DEPENDENCIES, "needs sqlalchemy, aiosqlite and pydantic")
class StoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # StaticPool keeps every session on the one connection an in-memory
        # SQLite database lives in. Without it each session gets its own
        # database and every table looks missing.
        self.engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        self.addAsyncCleanup(self.engine.dispose)
        await models.ensure_tables(self.engine)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def make_print(self, db, **overrides):
        values = {
            "printer_id": 1,
            "file_name": "cube.3mf",
            "print_type": "cloud",
            "status": models.STATUS_RUNNING,
            "started_at": NOW,
            "subtask_id": "task-1",
        }
        values.update(overrides)
        return await store.create_print(db, **values)

    async def three_prints(self, db):
        """One finished, one cancelled, one older, with different names."""
        await self.make_print(
            db, file_name="cube.3mf", subtask_id="a", status=models.STATUS_FINISHED,
            started_at=NOW, estimated_seconds=600,
        )
        await self.make_print(
            db, file_name="Balcony_stopper.3mf", subtask_id="b", status=models.STATUS_CANCELLED,
            started_at=NOW - timedelta(hours=1), estimated_seconds=7200,
        )
        await self.make_print(
            db, file_name="cube_v2.3mf", subtask_id="c", status=models.STATUS_FINISHED,
            started_at=NOW - timedelta(days=2), estimated_seconds=60,
        )
        await db.commit()

    async def test_the_search_matches_a_part_of_the_file_name(self):
        async with self.sessions() as db:
            await self.three_prints(db)
            found = await store.list_prints(db, search="cube")

        self.assertEqual([row.file_name for row in found], ["cube.3mf", "cube_v2.3mf"])

    async def test_the_search_ignores_case(self):
        async with self.sessions() as db:
            await self.three_prints(db)
            found = await store.list_prints(db, search="BALCONY")

        self.assertEqual([row.file_name for row in found], ["Balcony_stopper.3mf"])

    async def test_a_wildcard_in_the_search_is_not_a_wildcard(self):
        # An underscore means "any character" to LIKE. A file name full of them
        # would otherwise match everything the moment somebody types one.
        async with self.sessions() as db:
            await self.three_prints(db)
            found = await store.list_prints(db, search="cube_")

        self.assertEqual([row.file_name for row in found], ["cube_v2.3mf"])

    async def test_hiding_failed_leaves_the_stopped_ones_out(self):
        async with self.sessions() as db:
            await self.three_prints(db)
            found = await store.list_prints(db, hide_failed=True)

        self.assertEqual([row.file_name for row in found], ["cube.3mf", "cube_v2.3mf"])

    async def test_the_order_can_be_turned_around(self):
        async with self.sessions() as db:
            await self.three_prints(db)
            newest = await store.list_prints(db, order="newest")
            oldest = await store.list_prints(db, order="oldest")
            longest = await store.list_prints(db, order="largest")

        self.assertEqual(newest[0].file_name, "cube.3mf")
        self.assertEqual(oldest[0].file_name, "cube_v2.3mf")
        self.assertEqual(longest[0].file_name, "Balcony_stopper.3mf")

    async def test_an_unknown_order_falls_back_to_newest(self):
        # The endpoint refuses anything else, but the query must not depend on
        # somebody else's validation to stay sane.
        async with self.sessions() as db:
            await self.three_prints(db)
            found = await store.list_prints(db, order="sideways")

        self.assertEqual(found[0].file_name, "cube.3mf")

    async def test_a_column_added_later_reaches_an_existing_table(self):
        """The update path: a table created by an older version gains a column.

        create_all() would not notice, and every query naming the column would
        fail on exactly the instances that have history worth keeping.
        """
        async with self.engine.begin() as connection:
            await connection.execute(
                text(f"ALTER TABLE {models.prints_table.name} DROP COLUMN completed_fraction")
            )

        await models.ensure_tables(self.engine)

        async with self.sessions() as db:
            print_id = await self.make_print(db)
            await store.set_print_status(
                db, print_id, models.STATUS_FAILED, completed_fraction=0.5
            )
            await db.commit()

            self.assertEqual((await store.get_print(db, print_id)).completed_fraction, 0.5)

    async def test_a_print_comes_back_as_it_went_in(self):
        async with self.sessions() as db:
            print_id = await self.make_print(db)
            await db.commit()

            record = await store.get_print(db, print_id)
            self.assertEqual(record.file_name, "cube.3mf")
            self.assertEqual(record.status, models.STATUS_RUNNING)
            self.assertFalse(record.spent)

    async def test_a_print_is_found_by_its_subtask(self):
        async with self.sessions() as db:
            print_id = await self.make_print(db, subtask_id="abc")
            await db.commit()

            found = await store.find_print_by_subtask(db, 1, "abc")
            self.assertEqual(found.id, print_id)
            self.assertIsNone(await store.find_print_by_subtask(db, 1, "other"))
            # Another printer may well run a job of the same name.
            self.assertIsNone(await store.find_print_by_subtask(db, 2, "abc"))

    async def test_a_print_is_found_by_name_and_start(self):
        async with self.sessions() as db:
            print_id = await self.make_print(db, subtask_id=None)
            await db.commit()

            found = await store.find_print_by_start(db, 1, "cube.3mf", NOW)
            self.assertEqual(found.id, print_id)
            self.assertIsNone(
                await store.find_print_by_start(db, 1, "cube.3mf", NOW + timedelta(minutes=1))
            )

    async def test_the_open_print_is_the_newest_running_one(self):
        async with self.sessions() as db:
            await self.make_print(db, subtask_id="old", started_at=NOW - timedelta(hours=2))
            newest = await self.make_print(db, subtask_id="new", started_at=NOW)
            await self.make_print(
                db, subtask_id="done", started_at=NOW, status=models.STATUS_FINISHED
            )
            await db.commit()

            self.assertEqual((await store.find_open_print(db, 1)).id, newest)

    async def test_an_open_print_is_more_than_a_running_one(self):
        # A print recorded without its 3MF, or one joined in the middle, is just
        # as unfinished. Missing them here leaves them open for good.
        for status in (models.STATUS_INCOMPLETE, models.STATUS_NO_3MF):
            with self.subTest(status=status):
                async with self.sessions() as db:
                    print_id = await self.make_print(db, subtask_id=status, status=status)
                    await db.commit()

                    found = await store.find_open_print(db, 1)
                    self.assertEqual(found.id, print_id)

                    await store.set_print_status(db, print_id, models.STATUS_FINISHED)
                    await db.commit()

    async def test_filament_rows_keep_the_slicer_order(self):
        async with self.sessions() as db:
            print_id = await self.make_print(db)
            await store.add_filament_rows(
                db,
                print_id,
                [
                    store.FilamentRow(filament_id=2, slot_index="0-1", estimated_grams=12.5),
                    store.FilamentRow(filament_id=1, slot_index="0-0", estimated_grams=41.2),
                ],
            )
            await db.commit()

            rows = await store.list_filaments(db, print_id)
            self.assertEqual([row.filament_id for row in rows], [1, 2])
            self.assertEqual(rows[0].estimated_grams, 41.2)
            self.assertFalse(rows[0].manual_override)

    async def test_the_booking_state_counts_only_rows_with_a_spool(self):
        async with self.sessions() as db:
            print_id = await self.make_print(db)
            await store.add_filament_rows(
                db,
                print_id,
                [
                    store.FilamentRow(filament_id=1, spool_id=7, estimated_grams=41.2),
                    store.FilamentRow(filament_id=2, spool_id=None, estimated_grams=12.5),
                ],
            )
            await db.commit()

            # The row without a spool is not an open booking, it is an open
            # assignment. Counting it would keep the print unbookable forever.
            self.assertEqual(await store.booking_state(db, print_id), (1, 0))

            rows = await store.list_filaments(db, print_id)
            await store.mark_filament_spent(db, rows[0].id, 41.2, NOW)
            await db.commit()

            self.assertEqual(await store.booking_state(db, print_id), (0, 1))

    async def test_a_correction_settles_the_row(self):
        async with self.sessions() as db:
            print_id = await self.make_print(db)
            await store.add_filament_rows(
                db, print_id, [store.FilamentRow(filament_id=1, spool_id=7, estimated_grams=41.2)]
            )
            await db.commit()

            row = (await store.list_filaments(db, print_id))[0]
            await store.override_filament_amount(db, row.id, 30.0, NOW)
            await db.commit()

            corrected = await store.get_filament(db, row.id)
            self.assertEqual(corrected.spent_grams, 30.0)
            self.assertTrue(corrected.manual_override)
            self.assertIsNotNone(corrected.spent_at)
            # The slicer estimate stays comparable against the scale.
            self.assertEqual(corrected.estimated_grams, 41.2)

    async def test_the_preview_comes_back_with_its_type(self):
        async with self.sessions() as db:
            with_image = await self.make_print(
                db, subtask_id="a", thumbnail=b"PNGDATA", thumbnail_mime="image/png"
            )
            without = await self.make_print(db, subtask_id="b")
            await db.commit()

            self.assertEqual(await store.read_thumbnail(db, with_image), (b"PNGDATA", "image/png"))
            self.assertIsNone(await store.read_thumbnail(db, without))
            self.assertIsNone(await store.read_thumbnail(db, 999))

    async def test_the_history_query_leaves_the_blob_behind(self):
        async with self.sessions() as db:
            await self.make_print(db, thumbnail=b"PNGDATA", thumbnail_mime="image/png")
            await db.commit()

            row = (await store.list_prints(db))[0]
            # Shipping every preview with every history page would be megabytes
            # per request. The mime column is what tells the page a picture is
            # there to fetch.
            self.assertNotIn("thumbnail", row._mapping)
            self.assertEqual(row.thumbnail_mime, "image/png")

    async def test_purging_takes_the_filament_rows_along(self):
        async with self.sessions() as db:
            old = await self.make_print(db, subtask_id="old", started_at=NOW - timedelta(days=400))
            recent = await self.make_print(db, subtask_id="new", started_at=NOW)
            for print_id in (old, recent):
                await store.add_filament_rows(
                    db, print_id, [store.FilamentRow(filament_id=1, spool_id=7)]
                )
            await db.commit()

            removed = await models.purge_expired_history(db, retention_days=365, now=NOW)
            await db.commit()

            self.assertEqual(removed, 1)
            self.assertIsNone(await store.get_print(db, old))
            self.assertIsNotNone(await store.get_print(db, recent))
            # Orphaned filament rows would be invisible in the interface, which
            # is the worst kind of leak.
            self.assertEqual(await store.list_filaments(db, old), [])
            self.assertEqual(len(await store.list_filaments(db, recent)), 1)

    async def test_keeping_everything(self):
        async with self.sessions() as db:
            await self.make_print(db, started_at=NOW - timedelta(days=4000))
            await db.commit()

            self.assertEqual(await models.purge_expired_history(db, retention_days=0, now=NOW), 0)


    async def test_the_status_row_is_written_once_and_then_updated(self):
        async with self.sessions() as db:
            for connected in (False, True):
                await store.upsert_printer_status(
                    db,
                    printer_id=1,
                    printer_name="X1C",
                    connected=connected,
                    tracking_enabled=True,
                    updated_at=NOW,
                    current_file_name="cube.3mf",
                )
            await db.commit()

            rows = await store.list_printer_status(db)
            # One printer, one row, however often it reports.
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0].connected)
            self.assertEqual(rows[0].current_file_name, "cube.3mf")

    async def test_a_printer_that_is_gone_loses_its_row(self):
        async with self.sessions() as db:
            for printer_id in (1, 2, 3):
                await store.upsert_printer_status(
                    db,
                    printer_id=printer_id,
                    printer_name=f"printer {printer_id}",
                    connected=True,
                    tracking_enabled=True,
                    updated_at=NOW,
                )
            await db.commit()

            # A printer removed or deactivated in FilaMan would otherwise sit on
            # the page as connected for good.
            await store.forget_printers(db, [1, 3])
            await db.commit()

            self.assertEqual([row.printer_id for row in await store.list_printer_status(db)], [1, 3])

    async def test_forgetting_everything(self):
        async with self.sessions() as db:
            await store.upsert_printer_status(
                db,
                printer_id=1,
                printer_name="X1C",
                connected=True,
                tracking_enabled=True,
                updated_at=NOW,
            )
            await db.commit()

            await store.forget_printers(db, [])
            await db.commit()

            self.assertEqual(await store.list_printer_status(db), [])


if __name__ == "__main__":
    unittest.main()
