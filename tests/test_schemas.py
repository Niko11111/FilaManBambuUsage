"""Tests for the wire format.

Only the parts that decide something. Field names and defaults are read in
review; what needs a test is the conversion a timestamp goes through on its way
out, because getting it wrong is invisible in the JSON and only shows up as an
hour or two of drift on somebody's screen.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from ._support import HAS_TEST_DEPENDENCIES

if HAS_TEST_DEPENDENCIES:
    from pydantic import ValidationError

    from bambu_usage.schemas import (
        AssignSpoolRequest,
        CorrectUsageRequest,
        FilamentUsage,
        PluginSettings,
        PrinterStatus,
        PrintRecord,
    )

NAIVE = datetime(2026, 8, 20, 21, 25, 36)


@unittest.skipUnless(HAS_TEST_DEPENDENCIES, "needs pydantic")
class UtcDatetimeTest(unittest.TestCase):
    def record(self, **overrides):
        values = {
            "id": 1,
            "printer_id": 1,
            "file_name": "cube.3mf",
            "print_type": "cloud",
            "started_at": NAIVE,
            "status": "finished",
            "spent": True,
            "has_thumbnail": False,
        }
        values.update(overrides)
        return PrintRecord(**values)

    def test_a_naive_timestamp_comes_out_as_utc(self):
        # SQLite gives the value back without the offset it was written with.
        # Handing that to a browser is what makes a print look two hours early.
        self.assertEqual(self.record().started_at.tzinfo, timezone.utc)

    def test_the_instant_itself_does_not_move(self):
        # Stamping, not converting. 21:25 UTC stays 21:25 UTC.
        started = self.record().started_at
        self.assertEqual(started.replace(tzinfo=None), NAIVE)

    def test_an_aware_timestamp_is_left_alone(self):
        berlin = timezone(timedelta(hours=2))
        aware = NAIVE.replace(tzinfo=berlin)

        self.assertEqual(self.record(started_at=aware).started_at.utcoffset(), timedelta(hours=2))

    def test_none_stays_none(self):
        self.assertIsNone(self.record(finished_at=None).finished_at)

    def test_it_serialises_with_the_offset(self):
        # The point of the exercise: the JSON the page receives says which zone.
        text = self.record().model_dump_json()
        self.assertIn("2026-08-20T21:25:36Z", text)

    def test_every_other_timestamp_of_the_wire_format_too(self):
        # One definition, applied per field, and a field that forgets it would
        # be the only one drifting. Cheaper to check than to notice later.
        usage = FilamentUsage(id=1, filament_id=1, spent_at=NAIVE)
        status = PrinterStatus(
            printer_id=1,
            printer_name="X1C",
            connected=True,
            tracking_enabled=True,
            updated_at=NAIVE,
        )

        self.assertEqual(usage.spent_at.tzinfo, timezone.utc)
        self.assertEqual(status.updated_at.tzinfo, timezone.utc)


@unittest.skipUnless(HAS_TEST_DEPENDENCIES, "needs pydantic")
class RequestBoundsTest(unittest.TestCase):
    def test_a_correction_refuses_what_is_not_a_number_of_grams(self):
        # Infinity would clamp the spool to zero and leave a history that no
        # longer serialises as JSON.
        for grams in (float("inf"), float("nan"), -1, 10_001):
            with self.subTest(grams=grams), self.assertRaises(ValidationError):
                CorrectUsageRequest(grams=grams)

    def test_a_real_correction_passes(self):
        self.assertEqual(CorrectUsageRequest(grams=41.2).grams, 41.2)

    def test_ids_beyond_any_database_column_are_refused(self):
        with self.assertRaises(ValidationError):
            PluginSettings(printer_id=99999999999999999999)
        with self.assertRaises(ValidationError):
            AssignSpoolRequest(spool_id=99999999999999999999)

    def test_a_retention_whose_cutoff_would_overflow_is_refused(self):
        with self.assertRaises(ValidationError):
            PluginSettings(history_retention_days=10**7)


if __name__ == "__main__":
    unittest.main()
