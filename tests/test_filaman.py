"""Tests for the pure parts of the seam to FilaMan.

filaman.py imports FilaMan and SQLAlchemy inside the functions that need them,
which is what makes these two testable here at all: the module itself imports
nothing but the standard library.

Both functions under test are private, and that is on purpose. They are the two
places where data from another plugin enters this one, so what they do with
nonsense is exactly what deserves a test.
"""

from __future__ import annotations

import unittest

from bambu_usage.filaman import BambuPrinter, _slot_index_of, _to_bambu_printer


class FakePrinter:
    """The three attributes _to_bambu_printer reads off a Printer record."""

    def __init__(self, driver_config, printer_id=7, name="X1C"):
        self.id = printer_id
        self.name = name
        self.driver_config = driver_config


class FakeSlot:
    """The one attribute _slot_index_of reads off a PrinterSlot."""

    def __init__(self, custom_fields):
        self.custom_fields = custom_fields


COMPLETE_CONFIG = {
    "host": "192.168.4.50",
    "serial": "01P00A000000000",
    "access_code": "12345678",
    "printer_model": "X1C",
}


class ToBambuPrinterTest(unittest.TestCase):
    def test_a_complete_config_becomes_a_dataclass(self):
        printer = _to_bambu_printer(FakePrinter(COMPLETE_CONFIG))
        self.assertEqual(
            printer,
            BambuPrinter(
                printer_id=7,
                name="X1C",
                host="192.168.4.50",
                serial="01P00A000000000",
                access_code="12345678",
            ),
        )

    def test_an_unknown_extra_key_is_ignored(self):
        # driver_config belongs to the Bambu Lab driver and may grow keys.
        config = dict(COMPLETE_CONFIG, some_future_option=True)
        self.assertIsNotNone(_to_bambu_printer(FakePrinter(config)))

    def test_no_config_at_all_is_skipped(self):
        # Skipping quietly would leave the user wondering why one printer is
        # never tracked, so every skip says so in the log.
        with self.assertLogs("bambu_usage.filaman", level="WARNING"):
            self.assertIsNone(_to_bambu_printer(FakePrinter(None)))

    def test_a_config_that_is_not_an_object_is_skipped(self):
        with self.assertLogs("bambu_usage.filaman", level="WARNING"):
            self.assertIsNone(_to_bambu_printer(FakePrinter("192.168.4.50")))

    def test_every_missing_credential_is_skipped(self):
        for key in ("host", "serial", "access_code"):
            with self.subTest(missing=key):
                config = {k: v for k, v in COMPLETE_CONFIG.items() if k != key}
                with self.assertLogs("bambu_usage.filaman", level="WARNING"):
                    self.assertIsNone(_to_bambu_printer(FakePrinter(config)))

    def test_an_empty_credential_counts_as_missing(self):
        # A half configured printer is worse than an unconfigured one, because
        # the connection attempt would fail on every reconnect.
        config = dict(COMPLETE_CONFIG, access_code="")
        with self.assertLogs("bambu_usage.filaman", level="WARNING"):
            self.assertIsNone(_to_bambu_printer(FakePrinter(config)))

    def test_a_numeric_access_code_survives_as_a_string(self):
        # JSON keeps 12345678 as a number if it was ever written as one.
        config = dict(COMPLETE_CONFIG, access_code=12345678)
        printer = _to_bambu_printer(FakePrinter(config))
        self.assertEqual(printer.access_code, "12345678")


class SlotIndexTest(unittest.TestCase):
    def test_the_field_is_read(self):
        self.assertEqual(_slot_index_of(FakeSlot({"slot_index": "1-2"})), "1-2")

    def test_other_custom_fields_do_not_disturb(self):
        slot = FakeSlot({"slot_index": "0-0", "nozzle": "0.4"})
        self.assertEqual(_slot_index_of(slot), "0-0")

    def test_a_slot_without_the_field_has_no_index(self):
        self.assertIsNone(_slot_index_of(FakeSlot({"nozzle": "0.4"})))

    def test_no_custom_fields_at_all(self):
        self.assertIsNone(_slot_index_of(FakeSlot(None)))

    def test_custom_fields_that_are_not_an_object(self):
        self.assertIsNone(_slot_index_of(FakeSlot("1-2")))

    def test_the_external_spool_is_just_another_index(self):
        self.assertEqual(_slot_index_of(FakeSlot({"slot_index": "255-254"})), "255-254")


if __name__ == "__main__":
    unittest.main()
