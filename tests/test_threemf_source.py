"""Tests for where a 3MF is fetched from: the cloud, or which file on the printer.

The locations are the shapes printers have been seen to report in print.url and
print.gcode_file, see docs/03_Bambu_Data_Sources.md. The FTPS connection is
replaced by a stand in that knows a fixed set of remote paths, which is all the
search through the directories reasons about.
"""

from __future__ import annotations

import ftplib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bambu_usage import threemf
from bambu_usage.threemf import (
    ThreeMFError,
    download,
    download_from_printer,
    ftps_search_paths,
    is_http_url,
    is_internal_storage,
    printer_file_name,
)

HISTORY_REPRINT = "file:///userdata/model/history/Cube.gcode.3mf"
INTERNAL_DISPATCH = "brtc://emmc/Cube.gcode.3mf"
EXTERNAL_STORAGE = "ftp:///Cube.gcode.3mf"
CLOUD_URL = "https://example.invalid/job.3mf"


class LocationTest(unittest.TestCase):
    def test_only_http_and_https_are_downloads(self):
        self.assertTrue(is_http_url(CLOUD_URL))
        self.assertTrue(is_http_url("HTTP://example.invalid/job.3mf"))
        for location in (HISTORY_REPRINT, INTERNAL_DISPATCH, EXTERNAL_STORAGE, "/cache/a.3mf"):
            self.assertFalse(is_http_url(location), location)

    def test_the_file_name_drops_scheme_and_directory(self):
        for location in (HISTORY_REPRINT, INTERNAL_DISPATCH, EXTERNAL_STORAGE):
            self.assertEqual(printer_file_name(location), "Cube.gcode.3mf", location)
        self.assertEqual(printer_file_name("Cube.gcode.3mf"), "Cube.gcode.3mf")

    def test_a_location_without_a_file_is_refused(self):
        for location in ("", "file:///userdata/", "/cache/..", "ftp://."):
            with self.assertRaises(ThreeMFError, msg=location):
                printer_file_name(location)

    def test_a_line_break_never_reaches_the_ftp_command(self):
        with self.assertRaises(ThreeMFError):
            printer_file_name("file:///a.3mf\r\nDELE b.3mf")

    def test_internal_storage_is_recognised_with_and_without_a_scheme(self):
        self.assertTrue(is_internal_storage(HISTORY_REPRINT))
        self.assertTrue(is_internal_storage("/userdata/model/history/Cube.gcode.3mf"))
        self.assertFalse(is_internal_storage(INTERNAL_DISPATCH))
        self.assertFalse(is_internal_storage("file:///media/usb0/Cube.gcode.3mf"))

    def test_the_root_is_searched_before_the_cache(self):
        self.assertEqual(ftps_search_paths("a.3mf"), ["/a.3mf", "/cache/a.3mf"])


class DispatchTest(unittest.IsolatedAsyncioTestCase):
    """download() hands a location to the right transfer."""

    async def fetch(self, location):
        with mock.patch.object(threemf, "download_from_url") as from_url, \
                mock.patch.object(threemf, "download_from_printer") as from_printer:
            await download("printer.local", "code", location, Path("job.3mf"))
        return from_url, from_printer

    async def test_a_cloud_url_is_downloaded(self):
        from_url, from_printer = await self.fetch(CLOUD_URL)
        from_url.assert_awaited_once_with(CLOUD_URL, Path("job.3mf"))
        from_printer.assert_not_called()

    async def test_a_file_url_is_fetched_off_the_printer(self):
        # The report behind the error a user saw on an H2C: a print started
        # again from the printer's history. It used to be refused as a URL.
        for location in (HISTORY_REPRINT, INTERNAL_DISPATCH, EXTERNAL_STORAGE):
            from_url, from_printer = await self.fetch(location)
            from_url.assert_not_called()
            from_printer.assert_awaited_once_with(
                "printer.local", "code", location, Path("job.3mf")
            )


class FakeFTPS:
    """Stands in for the FTPS connection, serving a fixed set of remote paths."""

    def __init__(self, files):
        self.files = files
        self.asked = []

    def retrbinary(self, command, callback, blocksize):
        path = command.removeprefix("RETR ")
        self.asked.append(path)
        if path not in self.files:
            # What a printer answers for a file that is not there.
            raise ftplib.error_perm("550 Failed to open file.")
        callback(self.files[path])


class SearchTest(unittest.TestCase):
    """Which directory the 3MF is taken from, over one connection."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dest = Path(directory.name) / "job.3mf"

    def test_the_root_is_taken_when_it_has_the_file(self):
        connection = FakeFTPS({"/a.3mf": b"root", "/cache/a.3mf": b"cache"})
        self.assertTrue(threemf._retrieve_first_found(connection, "a.3mf", self.dest))
        self.assertEqual(self.dest.read_bytes(), b"root")
        self.assertEqual(connection.asked, ["/a.3mf"])

    def test_the_cache_is_tried_when_the_root_has_nothing(self):
        connection = FakeFTPS({"/cache/a.3mf": b"cache"})
        self.assertTrue(threemf._retrieve_first_found(connection, "a.3mf", self.dest))
        self.assertEqual(self.dest.read_bytes(), b"cache")

    def test_nowhere_is_false_not_an_empty_file_booked_at_zero(self):
        connection = FakeFTPS({})
        self.assertFalse(threemf._retrieve_first_found(connection, "a.3mf", self.dest))
        self.assertEqual(connection.asked, ["/a.3mf", "/cache/a.3mf"])

    def test_a_broken_connection_is_not_taken_for_a_missing_file(self):
        connection = mock.Mock()
        connection.retrbinary.side_effect = ftplib.error_temp("421 Timeout.")
        with self.assertRaises(ftplib.error_temp):
            threemf._retrieve_first_found(connection, "a.3mf", self.dest)
        self.assertEqual(connection.retrbinary.call_count, 1)


class MissMessageTest(unittest.IsolatedAsyncioTestCase):
    """What the printer page shows when the file is on none of the paths."""

    async def miss(self, location):
        with mock.patch.object(threemf, "_fetch_over_ftps", return_value=False):
            with self.assertRaises(ThreeMFError) as caught:
                await download_from_printer("printer.local", "code", location, Path("job.3mf"))
        return str(caught.exception)

    async def test_a_history_reprint_says_where_the_file_sits(self):
        message = await self.miss(HISTORY_REPRINT)
        self.assertIn("'Cube.gcode.3mf'", message)
        self.assertIn("internal storage", message)

    async def test_any_other_miss_names_the_directories_searched(self):
        message = await self.miss(INTERNAL_DISPATCH)
        self.assertIn("/, /cache/", message)
        self.assertIn("printer.local", message)


if __name__ == "__main__":
    unittest.main()
