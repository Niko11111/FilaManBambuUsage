"""Tests for the diagnostics: what of a report is kept, and the text built from it.

The reports are shaped like the ones printers send, see
docs/03_Bambu_Data_Sources.md. The H2C values come from a user's report of
24.11.2026, the rest from the X1C on the test instance.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from bambu_usage.diagnostics import (
    MAX_LIST_ITEMS,
    MAX_STRING_LENGTH,
    FilamentFacts,
    PrinterFacts,
    PrintFacts,
    Versions,
    format_overview,
    format_print,
    snapshot_of,
    without_secrets,
)

NOW = datetime(2026, 11, 26, 9, 30, tzinfo=timezone.utc)
VERSIONS = Versions(plugin="1.0.1", filaman="2.7.0", driver="2.13.0")
SIGNED_URL = "https://public-cdn.bblmw.com/upload/3mf/Cube.3mf?Expires=1&Signature=abc&Key-Pair-Id=K1"

CLOUD_REPORT = {"print": {
    "command": "project_file",
    "print_type": "cloud",
    "url": SIGNED_URL,
    "subtask_name": "Cube",
    "ams_mapping": [-1, 1],
    "ams_mapping2": [{"ams_id": 255, "slot_id": 255}, {"ams_id": 0, "slot_id": 1}],
    "access_code": "12345678",
    "sn": "01P00A000000000",
    "ams": {
        "tray_now": "1",
        "ams": [{"id": "0", "info": "1001", "tray": [{"id": "0"}, {"id": "1"}]}],
    },
    "vt_tray": {"id": "254", "tray_type": "PLA"},
}}


class SnapshotTest(unittest.TestCase):
    def test_keeps_what_explains_a_mapping(self):
        kept = snapshot_of(CLOUD_REPORT)
        self.assertEqual(kept["ams_mapping"], [-1, 1])
        self.assertEqual(kept["ams_mapping2"][1], {"ams_id": 0, "slot_id": 1})
        self.assertEqual(kept["tray_now"], "1")
        self.assertEqual(kept["ams_units"], [{"id": "0", "info": "1001", "trays": ["0", "1"]}])
        self.assertEqual(kept["external"], ["254"])

    def test_keeps_nothing_that_identifies_or_opens_the_printer(self):
        stored = json.dumps(snapshot_of(CLOUD_REPORT))
        for secret in ("12345678", "01P00A000000000", "Signature", "Expires"):
            self.assertNotIn(secret, stored)

    def test_newer_printers_list_every_external_input(self):
        report = {"print": {"vir_slot": [{"id": "254"}, {"id": "255"}], "vt_tray": {"id": "254"}}}
        self.assertEqual(snapshot_of(report)["external"], ["254", "255"])

    def test_a_report_without_a_print_section_keeps_nothing(self):
        self.assertEqual(snapshot_of({}), {})
        self.assertEqual(snapshot_of({"print": "nonsense"}), {})

    def test_whatever_a_firmware_adds_stays_bounded(self):
        report = {"print": {
            "subtask_name": "x" * 5000,
            "ams_mapping": list(range(1000)),
            "ams_mapping2": [[[[["too deep"]]]]],
            "gcode_file": "a\nb\x00c",
        }}
        kept = snapshot_of(report)
        self.assertEqual(len(kept["subtask_name"]), MAX_STRING_LENGTH)
        self.assertEqual(len(kept["ams_mapping"]), MAX_LIST_ITEMS)
        self.assertEqual(kept["ams_mapping2"], [[[None]]])
        self.assertEqual(kept["gcode_file"], "abc")


class WithoutSecretsTest(unittest.TestCase):
    def test_a_signed_cloud_url_loses_its_signature(self):
        self.assertEqual(
            without_secrets(SIGNED_URL), "https://public-cdn.bblmw.com/upload/3mf/Cube.3mf"
        )

    def test_a_password_in_the_url_is_dropped(self):
        self.assertEqual(
            without_secrets("ftp://bblp:secret@printer.local/Cube.3mf"),
            "ftp://printer.local/Cube.3mf",
        )

    def test_a_file_on_the_printer_keeps_its_path(self):
        url = "file:///userdata/model/history/Cube.gcode.3mf"
        self.assertEqual(without_secrets(url), url)


def a_print(**overrides) -> PrintFacts:
    values = {
        "print_id": 42,
        "file_name": "Cube",
        "printer_id": 1,
        "printer_name": "X1C",
        "status": "finished",
        "spent": True,
        "started_at": datetime(2026, 11, 25, 11, 5, tzinfo=timezone.utc),
        "print_type": "cloud",
        "snapshot": snapshot_of(CLOUD_REPORT),
        "filaments": [FilamentFacts(
            filament_id=2, material="PLA", color_hex="#000000", estimated_grams=5.9,
            slot_index="0-1", spool_id=6, spool_source="filaman",
        )],
    }
    values.update(overrides)
    return PrintFacts(**values)


class PrintTextTest(unittest.TestCase):
    def test_names_versions_print_and_the_chain_to_the_spool(self):
        text = format_print(VERSIONS, a_print(), NOW)
        self.assertIn("Plugin 1.0.1, FilaMan 2.7.0, Bambu Lab driver 2.13.0", text)
        self.assertIn('Print #42 "Cube" on X1C (id 1)', text)
        self.assertIn("3MF from     https://public-cdn.bblmw.com/upload/3mf/Cube.3mf", text)
        self.assertIn('ams_mapping2 [{"ams_id":255,"slot_id":255},{"ams_id":0,"slot_id":1}]', text)
        self.assertIn("Filament 2   PLA #000000 5.90 g -> slot 0-1 -> spool #6 (filaman)", text)
        self.assertIn("AMS units    0 (info 1001, trays [\"0\",\"1\"])", text)

    def test_times_are_utc_whatever_the_server_runs_in(self):
        text = format_print(VERSIONS, a_print(), NOW)
        self.assertIn("Generated 2026-11-26 09:30 UTC", text)
        self.assertIn("Started      2026-11-25 11:05 UTC, cloud", text)

    def test_a_print_from_before_the_snapshot_says_so(self):
        text = format_print(VERSIONS, a_print(snapshot=None), NOW)
        self.assertIn("not kept, the print started before 1.0.2", text)
        self.assertIn("AMS units    unknown", text)

    def test_an_unreadable_version_costs_one_word(self):
        text = format_print(Versions(plugin="1.0.1"), a_print(), NOW)
        self.assertIn("FilaMan unknown, Bambu Lab driver unknown", text)


class OverviewTextTest(unittest.TestCase):
    def test_every_printer_with_the_layout_of_its_latest_print(self):
        printers = [
            PrinterFacts(printer_id=1, name="X1C", connected=True, tracking_enabled=True),
            PrinterFacts(printer_id=2, name="H2C", connected=False, tracking_enabled=True,
                         last_error="could not fetch 'Cube.3mf'"),
        ]
        text = format_overview(VERSIONS, printers, [a_print()], NOW)
        self.assertIn("Printer X1C (id 1)\n  connected, tracking on\n  AMS units    0 (info", text)
        self.assertIn("Printer H2C (id 2)\n  not connected, tracking on\n  AMS units    unknown", text)
        self.assertIn("  Last error   could not fetch 'Cube.3mf'", text)
        self.assertIn('Last 1 print starts\n  #42 "Cube" on X1C (id 1)', text)

    def test_without_prints_it_says_so(self):
        text = format_overview(VERSIONS, [], [], NOW)
        self.assertIn("No prints recorded yet", text)


if __name__ == "__main__":
    unittest.main()
