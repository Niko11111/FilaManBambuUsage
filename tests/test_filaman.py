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

from bambu_usage.filaman import BambuPrinter, _slot_index_of, _to_bambu_printer, price_per_gram


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


class FakeSpool:
    """The three numbers a price per gram is worked out from."""

    def __init__(self, purchase_price=25.0, initial_total_weight_g=1250.0, empty_spool_weight_g=250.0):
        self.purchase_price = purchase_price
        self.initial_total_weight_g = initial_total_weight_g
        self.empty_spool_weight_g = empty_spool_weight_g


class PricePerGramTest(unittest.TestCase):
    """FilaMan's own net weight arithmetic, borrowed rather than invented."""

    def test_a_spool_with_everything(self):
        # 25 currency for a kilo of material on a 250 g spool.
        self.assertAlmostEqual(price_per_gram(FakeSpool()), 0.025)

    def test_every_missing_number_means_no_price(self):
        for field in ("purchase_price", "initial_total_weight_g", "empty_spool_weight_g"):
            with self.subTest(missing=field):
                self.assertIsNone(price_per_gram(FakeSpool(**{field: None})))

    def test_a_spool_that_weighs_less_than_its_core(self):
        # Nonsense in, nothing out. Dividing by zero or by a negative net weight
        # would put an absurd price on every gram of a print.
        self.assertIsNone(price_per_gram(FakeSpool(initial_total_weight_g=200.0)))
        self.assertIsNone(price_per_gram(FakeSpool(initial_total_weight_g=250.0)))

    def test_a_free_spool_is_still_a_price(self):
        # Zero is a number, not a missing one.
        self.assertEqual(price_per_gram(FakeSpool(purchase_price=0.0)), 0.0)


if __name__ == "__main__":
    unittest.main()
