"""Tests for reading slice_info.config.

The fixture is hand written, see tests/fixtures/README.md. The values asserted
here are derived from it, so replacing the fixture means revisiting this file.
"""

from __future__ import annotations

import unittest

from bambu_usage.threemf import parse_slice_info

from ._support import FIXTURES_DIR


class ParseSliceInfoTest(unittest.TestCase):
    def setUp(self):
        raw = (FIXTURES_DIR / "slice_info.config").read_bytes()
        self.plate_id, self.filaments = parse_slice_info(raw)

    def test_reads_the_plate_id(self):
        # Names the preview image (plate_1.png) and the gcode entry.
        self.assertEqual(self.plate_id, 1)

    def test_reads_every_filament(self):
        self.assertEqual(len(self.filaments), 2)

    def test_filament_ids_stay_one_based(self):
        # The slicer counts from 1 while ams_mapping is indexed from 0. Getting
        # this wrong is the off-by-one that misassigns every spool.
        self.assertEqual([f.filament_id for f in self.filaments], [1, 2])

    def test_reads_the_consumption(self):
        first = self.filaments[0]
        self.assertAlmostEqual(first.used_g, 41.20)
        self.assertAlmostEqual(first.used_m, 12.34)

    def test_reads_the_descriptive_attributes(self):
        first, second = self.filaments
        self.assertEqual(first.material, "PLA")
        self.assertEqual(first.color_hex, "#FFFFFF")
        self.assertEqual(first.tray_info_idx, "GFA00")
        self.assertEqual(second.material, "PETG")
        self.assertEqual(second.tray_info_idx, "GFG00")

    def test_ignores_unrelated_elements(self):
        # The fixture also holds header, object and other metadata entries.
        self.assertEqual(len(self.filaments), 2)


class ParseSliceInfoRobustnessTest(unittest.TestCase):
    """The file comes off a printer, so it is treated as foreign data."""

    def test_no_plate_yields_nothing(self):
        plate_id, filaments = parse_slice_info(b"<config><header/></config>")
        self.assertIsNone(plate_id)
        self.assertEqual(filaments, [])

    def test_missing_plate_index_is_tolerated(self):
        plate_id, filaments = parse_slice_info(
            b'<config><plate><filament id="1" used_g="5"/></plate></config>'
        )
        self.assertIsNone(plate_id)
        self.assertEqual(len(filaments), 1)

    def test_unparsable_numbers_become_none_instead_of_raising(self):
        _, filaments = parse_slice_info(
            b'<config><plate><filament id="1" used_g="n/a" used_m=""/></plate></config>'
        )
        self.assertIsNone(filaments[0].used_g)
        self.assertIsNone(filaments[0].used_m)

    def test_filament_without_usable_id_is_skipped(self):
        # Without an id it cannot be mapped to a tray, so keeping it would only
        # produce a row nobody can resolve.
        _, filaments = parse_slice_info(
            b'<config><plate>'
            b'<filament type="PLA" used_g="5"/>'
            b'<filament id="2" used_g="6"/>'
            b'</plate></config>'
        )
        self.assertEqual([f.filament_id for f in filaments], [2])

    def test_missing_attributes_are_none_not_absent(self):
        _, filaments = parse_slice_info(b'<config><plate><filament id="1"/></plate></config>')
        only = filaments[0]
        self.assertIsNone(only.material)
        self.assertIsNone(only.color_hex)
        self.assertIsNone(only.tray_info_idx)
        self.assertIsNone(only.used_g)


if __name__ == "__main__":
    unittest.main()
