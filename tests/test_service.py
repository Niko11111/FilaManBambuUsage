"""Tests for the pure helpers in service.py.

These run without a printer, without FilaMan and without a database, which is
the whole point of keeping service.py free of MQTT and HTTP.
"""

from __future__ import annotations

import unittest

from bambu_usage.service import (
    EXTERNAL_SLOT_INDEX,
    EXTERNAL_SPOOL_AMS_ID,
    EXTERNAL_SPOOL_TRAY_ID,
    TRAYS_PER_AMS,
    tray_to_slot_index,
)


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


if __name__ == "__main__":
    unittest.main()
