"""Tests for reading a 3MF and its slice_info.config.

The fixture is hand written, see tests/fixtures/README.md. The values asserted
here are derived from it, so replacing the fixture means revisiting this file.

The archives are assembled on the spot rather than committed. A real 3MF is
several megabytes of somebody else's model, and what parse() reasons about is
the structure, which a handful of entries reproduces exactly.
"""

from __future__ import annotations

import base64
import tempfile
import unittest
import zipfile
from pathlib import Path

from bambu_usage.threemf import (
    MAX_THUMBNAIL_BYTES,
    THUMBNAIL_MIME,
    ThreeMFError,
    parse,
    parse_slice_info,
)

from ._support import FIXTURES_DIR

# The smallest valid PNG there is, so the test suite carries no image file. One
# transparent pixel, 68 bytes.
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

ANOTHER_PNG = ONE_PIXEL_PNG + b"\x00"


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


class ParseArchiveTest(unittest.TestCase):
    """parse() against archives built for the case under test."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.slice_info = (FIXTURES_DIR / "slice_info.config").read_bytes()

    def build(self, entries: dict, name: str = "job.3mf") -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w") as bundle:
            for entry, payload in entries.items():
                bundle.writestr(entry, payload)
        return path

    def test_a_complete_archive(self):
        archive = self.build(
            {
                "Metadata/slice_info.config": self.slice_info,
                "Metadata/plate_1.png": ONE_PIXEL_PNG,
            }
        )
        metadata = parse(archive)

        self.assertEqual(metadata.plate_id, 1)
        self.assertEqual(len(metadata.filaments), 2)
        self.assertEqual(metadata.thumbnail, ONE_PIXEL_PNG)
        self.assertEqual(metadata.thumbnail_mime, THUMBNAIL_MIME)

    def test_the_plate_named_in_slice_info_wins(self):
        # A 3MF can carry several plate images. The one the print refers to is
        # the one slice_info.config names, not the first in the archive.
        archive = self.build(
            {
                "Metadata/slice_info.config": self.slice_info,
                "Metadata/plate_1.png": ONE_PIXEL_PNG,
                "Metadata/plate_2.png": ANOTHER_PNG,
            }
        )
        self.assertEqual(parse(archive).thumbnail, ONE_PIXEL_PNG)

    def test_without_slice_info_the_preview_still_arrives(self):
        # Nothing can be booked, but the print must not lose its picture as
        # well: it still has to be recognisable in the history.
        archive = self.build({"Metadata/plate_1.png": ONE_PIXEL_PNG})
        with self.assertLogs("bambu_usage.threemf", level="WARNING"):
            metadata = parse(archive)

        self.assertIsNone(metadata.plate_id)
        self.assertEqual(metadata.filaments, [])
        self.assertEqual(metadata.thumbnail, ONE_PIXEL_PNG)

    def test_without_a_preview(self):
        archive = self.build({"Metadata/slice_info.config": self.slice_info})
        metadata = parse(archive)

        self.assertEqual(len(metadata.filaments), 2)
        self.assertIsNone(metadata.thumbnail)
        self.assertIsNone(metadata.thumbnail_mime)

    def test_the_small_preview_is_not_the_preview(self):
        # A 3MF also carries plate_1_small.png and plate_no_light_1.png. Only
        # the exact name is the preview.
        archive = self.build(
            {
                "Metadata/plate_1_small.png": ONE_PIXEL_PNG,
                "Metadata/plate_no_light_1.png": ONE_PIXEL_PNG,
            }
        )
        self.assertIsNone(parse(archive).thumbnail)

    def test_malformed_slice_info_is_tolerated(self):
        archive = self.build(
            {
                "Metadata/slice_info.config": b"<config><plate>",
                "Metadata/plate_1.png": ONE_PIXEL_PNG,
            }
        )
        with self.assertLogs("bambu_usage.threemf", level="WARNING"):
            metadata = parse(archive)

        self.assertEqual(metadata.filaments, [])
        self.assertEqual(metadata.thumbnail, ONE_PIXEL_PNG)

    def test_an_oversized_preview_is_dropped_not_stored(self):
        # The preview becomes a BLOB in the database, so an implausible one is
        # skipped rather than kept. The print itself still counts.
        archive = self.build(
            {
                "Metadata/slice_info.config": self.slice_info,
                "Metadata/plate_1.png": b"\x00" * (MAX_THUMBNAIL_BYTES + 1),
            }
        )
        with self.assertLogs("bambu_usage.threemf", level="WARNING"):
            metadata = parse(archive)

        self.assertEqual(len(metadata.filaments), 2)
        self.assertIsNone(metadata.thumbnail)

    def test_a_file_that_is_not_a_zip(self):
        broken = self.root / "broken.3mf"
        broken.write_bytes(b"this is not an archive")
        with self.assertRaises(ThreeMFError):
            parse(broken)

    def test_a_file_that_is_not_there(self):
        with self.assertRaises(ThreeMFError):
            parse(self.root / "absent.3mf")
