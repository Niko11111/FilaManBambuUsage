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
    layer_shares,
    parse,
    parse_layer_shares,
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
        self.info = parse_slice_info(raw)
        self.plate_id = self.info.plate_id
        self.filaments = self.info.filaments

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

    def test_reads_what_the_slicer_predicts(self):
        self.assertEqual(self.info.estimated_seconds, 7412)

    def test_counts_the_objects_on_the_plate(self):
        self.assertEqual(self.info.object_count, 1)

    def test_reads_the_nozzle_it_was_sliced_for(self):
        self.assertAlmostEqual(self.info.nozzle_diameter, 0.4)


class ParseSliceInfoRobustnessTest(unittest.TestCase):
    """The file comes off a printer, so it is treated as foreign data."""

    def test_no_plate_yields_nothing(self):
        info = parse_slice_info(b"<config><header/></config>")
        self.assertIsNone(info.plate_id)
        self.assertEqual(info.filaments, [])

    def test_missing_plate_index_is_tolerated(self):
        info = parse_slice_info(
            b'<config><plate><filament id="1" used_g="5"/></plate></config>'
        )
        self.assertIsNone(info.plate_id)
        self.assertEqual(len(info.filaments), 1)

    def test_a_plate_without_objects_counts_none_not_zero(self):
        info = parse_slice_info(b'<config><plate><filament id="1"/></plate></config>')
        self.assertIsNone(info.object_count)

    def test_two_nozzles_keep_the_first(self):
        # An H2D reports both diameters here and the plugin has one field.
        info = parse_slice_info(
            b'<config><plate><metadata key="nozzle_diameters" value="0.4,0.6"/></plate></config>'
        )
        self.assertAlmostEqual(info.nozzle_diameter, 0.4)

    def test_an_unreadable_prediction_becomes_none(self):
        info = parse_slice_info(
            b'<config><plate><metadata key="prediction" value="soon"/></plate></config>'
        )
        self.assertIsNone(info.estimated_seconds)

    def test_unparsable_numbers_become_none_instead_of_raising(self):
        filaments = parse_slice_info(
            b'<config><plate><filament id="1" used_g="n/a" used_m=""/></plate></config>'
        ).filaments
        self.assertIsNone(filaments[0].used_g)
        self.assertIsNone(filaments[0].used_m)

    def test_filament_without_usable_id_is_skipped(self):
        # Without an id it cannot be mapped to a tray, so keeping it would only
        # produce a row nobody can resolve.
        filaments = parse_slice_info(
            b'<config><plate>'
            b'<filament type="PLA" used_g="5"/>'
            b'<filament id="2" used_g="6"/>'
            b'</plate></config>'
        ).filaments
        self.assertEqual([f.filament_id for f in filaments], [2])

    def test_missing_attributes_are_none_not_absent(self):
        filaments = parse_slice_info(
            b'<config><plate><filament id="1"/></plate></config>'
        ).filaments
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
        with self.assertLogs("bambu_usage.threemf", level="WARNING"):
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


# Written in the syntax of a real Bambu plate gcode, checked against three of
# them: the layer marker, relative extrusion, arcs, and tool numbers above the
# real ones that are markers rather than filaments. See tools/check_gcode.py for
# running the parser over an actual file.
PLATE_GCODE = """
; HEADER_BLOCK_START
; total layer number: 3
; HEADER_BLOCK_END
M83
T1000
G1 E20 F1800
; the prime line above belongs to no layer

; CHANGE_LAYER
; layer num/total_layer_count: 1/3
T0
G1 X10 Y10 E4
G1 E-.4 F1800
G1 E.4 F1800
G2 X11 Y11 I.5 J.5 E1

; CHANGE_LAYER
; layer num/total_layer_count: 2/3
G3 X12 Y12 I.5 J.5 E5
T1
G1 X20 Y20 E2

; CHANGE_LAYER
; layer num/total_layer_count: 3/3
T255
G1 X21 Y21 E2
M400
"""


class LayerSharesTest(unittest.TestCase):
    """The curve of how much of a filament each layer has used."""

    def setUp(self):
        self.shares = layer_shares(PLATE_GCODE.splitlines())

    def test_one_curve_per_filament_used(self):
        # Tools are numbered from zero, the slicer counts filaments from one.
        self.assertEqual(sorted(self.shares), [1, 2])

    def test_one_entry_per_layer(self):
        for filament_id, curve in self.shares.items():
            with self.subTest(filament=filament_id):
                self.assertEqual(len(curve), 3)

    def test_every_curve_ends_at_one(self):
        for filament_id, curve in self.shares.items():
            with self.subTest(filament=filament_id):
                self.assertEqual(curve[-1], 1.0)

    def test_arcs_count_as_material(self):
        # Filament 1 lays 5 in layer one and 5 in layer two, and half of that
        # comes from G2 and G3. Arcs carry about a quarter of a real print, so
        # missing them would look plausible and be wrong.
        self.assertEqual(self.shares[1], [0.5, 1.0, 1.0])

    def test_a_filament_used_late_stays_at_zero_before(self):
        # The case that justifies the whole exercise: booking a share of this
        # filament at layer one would charge for material never extruded.
        self.assertEqual(self.shares[2], [0.0, 0.5, 1.0])

    def test_retraction_and_unretraction_cancel(self):
        # Layer one has a -.4 and a +.4 in it, and 4 + 1 is what is left.
        self.assertEqual(self.shares[1][0], 0.5)

    def test_the_prime_line_belongs_to_no_layer(self):
        # 20 before the first marker, against 10 across the layers. Counting it
        # would put every curve out by two thirds.
        self.assertEqual(self.shares[1][0], 0.5)

    def test_marker_tools_are_not_filaments(self):
        # T1000 and T255 are markers. A filament 1001 would be charged for
        # material and nobody would ever look at that row.
        self.assertNotIn(1001, self.shares)
        self.assertNotIn(256, self.shares)

    def test_gcode_without_a_layer_marker_says_nothing(self):
        self.assertEqual(layer_shares(["M83", "T0", "G1 X1 Y1 E5"]), {})

    def test_nothing_at_all(self):
        self.assertEqual(layer_shares([]), {})


class ParseLayerSharesTest(unittest.TestCase):
    """The same, read out of an archive."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)

    def build(self, entries):
        path = self.root / "job.3mf"
        with zipfile.ZipFile(path, "w") as bundle:
            for entry, payload in entries.items():
                bundle.writestr(entry, payload)
        return path

    def test_the_plate_named_in_slice_info(self):
        archive = self.build({"Metadata/plate_6.gcode": PLATE_GCODE})
        self.assertEqual(sorted(parse_layer_shares(archive, 6)), [1, 2])

    def test_a_plate_that_is_not_in_the_archive(self):
        # A 3MF without its gcode is not an error, only a print that keeps
        # working off the slicer estimate.
        archive = self.build({"Metadata/plate_1.gcode": PLATE_GCODE})
        with self.assertLogs("bambu_usage.threemf", level="WARNING"):
            self.assertEqual(parse_layer_shares(archive, 6), {})

    def test_something_that_is_not_an_archive(self):
        broken = self.root / "broken.3mf"
        broken.write_bytes(b"not a zip")
        with self.assertRaises(ThreeMFError):
            parse_layer_shares(broken, 1)
