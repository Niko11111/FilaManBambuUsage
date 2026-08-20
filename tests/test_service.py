"""Tests for the pure helpers in service.py.

These run without a printer, without FilaMan and without a database, which is
the whole point of keeping service.py free of MQTT and HTTP.
"""

from __future__ import annotations

import unittest

from bambu_usage.service import (
    EXTERNAL_SLOT_INDEX,
    booking_factor,
    EXTERNAL_SPOOL_AMS_ID,
    EXTERNAL_SPOOL_TRAY_ID,
    TRAYS_PER_AMS,
    resolve_slot_indexes,
    should_spend,
    split_share,
    sum_grams_per_spool,
    tray_to_slot_index,
)
from bambu_usage.threemf import FilamentInfo


class TrayToSlotIndexTest(unittest.TestCase):
    """Bambu numbers trays globally, FilaMan addresses them per AMS unit."""

    def test_first_unit(self):
        self.assertEqual(tray_to_slot_index(0), "0-0")
        self.assertEqual(tray_to_slot_index(3), "0-3")

    def test_crosses_into_the_second_unit(self):
        # The interesting boundary: tray 4 is the first slot of the second unit,
        # not the fifth slot of the first one.
        self.assertEqual(tray_to_slot_index(4), "1-0")
        self.assertEqual(tray_to_slot_index(5), "1-1")

    def test_third_unit(self):
        self.assertEqual(tray_to_slot_index(11), "2-3")

    def test_negative_means_external_spool(self):
        # ams_mapping uses -1 for a filament that does not come from the AMS.
        self.assertEqual(tray_to_slot_index(-1), EXTERNAL_SLOT_INDEX)
        self.assertEqual(tray_to_slot_index(-99), EXTERNAL_SLOT_INDEX)

    def test_external_constant_matches_filaman_format(self):
        self.assertEqual(
            EXTERNAL_SLOT_INDEX,
            f"{EXTERNAL_SPOOL_AMS_ID}-{EXTERNAL_SPOOL_TRAY_ID}",
        )
        self.assertEqual(EXTERNAL_SLOT_INDEX, "255-254")

    def test_every_slot_of_a_unit_maps_back(self):
        for unit in range(3):
            for slot in range(TRAYS_PER_AMS):
                tray = unit * TRAYS_PER_AMS + slot
                self.assertEqual(tray_to_slot_index(tray), f"{unit}-{slot}")


class ResolveSlotIndexesTest(unittest.TestCase):
    """The slicer counts filaments from 1, ams_mapping is indexed from 0."""

    def filaments(self, *ids):
        return [FilamentInfo(filament_id=one_based) for one_based in ids]

    def test_the_mapping_is_read_one_off(self):
        # Filament 1 takes ams_mapping[0], filament 2 takes ams_mapping[1].
        # Reading it straight would charge every spool to its neighbour.
        resolved = resolve_slot_indexes(self.filaments(1, 2), [1, 0])
        self.assertEqual(resolved, {1: "0-1", 2: "0-0"})

    def test_a_filament_beyond_the_mapping_stays_open(self):
        resolved = resolve_slot_indexes(self.filaments(1, 2, 3), [0, 1])
        self.assertIsNone(resolved[3])

    def test_an_empty_mapping_leaves_everything_open(self):
        resolved = resolve_slot_indexes(self.filaments(1, 2), [])
        self.assertEqual(resolved, {1: None, 2: None})

    def test_minus_one_is_the_external_spool(self):
        resolved = resolve_slot_indexes(self.filaments(1), [-1])
        self.assertEqual(resolved[1], EXTERNAL_SLOT_INDEX)

    def test_a_tray_number_written_as_text_still_works(self):
        # The value comes out of JSON off a printer. A digit string is still a
        # tray number, and refusing it would lose a perfectly good assignment.
        resolved = resolve_slot_indexes(self.filaments(1), ["5"])
        self.assertEqual(resolved[1], "1-1")

    def test_nonsense_in_the_mapping_stays_open_rather_than_guessed(self):
        for value in (None, "left", {}, []):
            with self.subTest(value=value):
                self.assertIsNone(resolve_slot_indexes(self.filaments(1), [value])[1])

    def test_the_second_ams_unit(self):
        resolved = resolve_slot_indexes(self.filaments(1, 2), [4, 7])
        self.assertEqual(resolved, {1: "1-0", 2: "1-3"})


class SumGramsPerSpoolTest(unittest.TestCase):
    """One spool, one booking, however many filaments pointed at it."""

    def test_two_filaments_on_one_spool_are_summed(self):
        # The multi colour case from docs/04 section 6. Booking them apart would
        # hang two events off one spool and blur which print cost what.
        self.assertEqual(sum_grams_per_spool([(7, 41.2), (7, 12.5)]), {7: 53.7})

    def test_different_spools_stay_apart(self):
        self.assertEqual(sum_grams_per_spool([(7, 10.0), (8, 5.0)]), {7: 10.0, 8: 5.0})

    def test_a_row_without_a_spool_is_dropped(self):
        self.assertEqual(sum_grams_per_spool([(None, 10.0)]), {})

    def test_a_row_without_an_amount_is_dropped(self):
        self.assertEqual(sum_grams_per_spool([(7, None)]), {})

    def test_zero_and_negative_amounts_are_dropped(self):
        # A zero gram event is noise in the spool log, a negative one is wrong.
        self.assertEqual(sum_grams_per_spool([(7, 0.0), (7, -3.0)]), {})

    def test_nothing_at_all(self):
        self.assertEqual(sum_grams_per_spool([]), {})


class ShouldSpendTest(unittest.TestCase):
    """Booking happens at the end of a print, and only when allowed."""

    def test_a_finished_print_is_booked(self):
        self.assertTrue(should_spend(finished_normally=True, auto_spend=True, spend_on_cancel=False))

    def test_auto_spend_off_never_books(self):
        # The mode for watching along before trusting the plugin with the spools.
        self.assertFalse(should_spend(finished_normally=True, auto_spend=False, spend_on_cancel=True))

    def test_an_abort_is_not_booked_by_default(self):
        # An abort does not consume the full estimate. This is the case
        # OpenSpoolMan gets wrong by booking at the start.
        self.assertFalse(should_spend(finished_normally=False, auto_spend=True, spend_on_cancel=False))

    def test_an_abort_is_booked_when_asked_for(self):
        self.assertTrue(should_spend(finished_normally=False, auto_spend=True, spend_on_cancel=True))


class BookingFactorTest(unittest.TestCase):
    """What share of the estimate a print costs."""

    def test_a_print_that_ran_to_the_end_costs_all_of_it(self):
        self.assertEqual(booking_factor(was_stopped=False, completed_fraction=None), 1.0)
        # A share reported for a finished print changes nothing.
        self.assertEqual(booking_factor(was_stopped=False, completed_fraction=0.5), 1.0)

    def test_a_stopped_print_costs_its_share(self):
        self.assertEqual(booking_factor(was_stopped=True, completed_fraction=0.5), 0.5)

    def test_a_stopped_print_with_no_share_costs_nothing(self):
        # Not the full estimate. That is the mistake this exists to avoid.
        self.assertEqual(booking_factor(was_stopped=True, completed_fraction=None), 0.0)

    def test_a_share_outside_the_range_is_pulled_back_in(self):
        # A printer can report nonsense, and a factor above one would deduct
        # more than the print could possibly have used.
        self.assertEqual(booking_factor(was_stopped=True, completed_fraction=1.4), 1.0)
        self.assertEqual(booking_factor(was_stopped=True, completed_fraction=-0.2), 0.0)


class SplitShareTest(unittest.TestCase):
    """Dividing a filament row where the spool it draws from changed."""

    def test_a_whole_row_split_in_the_middle(self):
        # Both ends empty is the normal case: the row covers the whole print.
        self.assertEqual(split_share(None, None, 0.4), 0.4)

    def test_a_row_that_was_already_split_once(self):
        # The second half of a print, split again halfway through it. Compared
        # loosely because (0.7 - 0.4) / (1 - 0.4) is not exactly a half in binary,
        # and rounding a share to make a test happy would be the wrong fix.
        self.assertAlmostEqual(split_share(0.4, None, 0.7), 0.5)

    def test_a_row_that_ends_early(self):
        self.assertEqual(split_share(0.0, 0.4, 0.2), 0.5)

    def test_splitting_at_an_edge_would_leave_an_empty_row(self):
        for at in (0.0, 1.0):
            with self.subTest(at=at):
                self.assertIsNone(split_share(None, None, at))
        self.assertIsNone(split_share(0.4, None, 0.4))

    def test_a_moment_outside_the_span_belongs_to_another_row(self):
        # That part of the print is over, and its row was closed off already.
        self.assertIsNone(split_share(0.4, None, 0.2))
        self.assertIsNone(split_share(0.0, 0.4, 0.6))


if __name__ == "__main__":
    unittest.main()
