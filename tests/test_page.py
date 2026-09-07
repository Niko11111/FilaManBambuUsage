"""Tests for the page's relationship to FilaMan's shell.

Until 0.8.4 the page borrowed FilaMan's shell at runtime, fetching the start
page and lifting the drawer out of it, because a plugin page was served outside
the interface. FilaMan 1.2.45 renders a plugin page in a frame inside its own
shell, so the borrowing came out in 0.9.0.

These tests keep it out. Reaching into somebody else's DOM is the kind of thing
that gets reintroduced by a well meant patch when a page looks bare in an older
FilaMan, and it would fail silently: the borrowing was written to stand down
whenever anything was missing, so a broken version of it looks exactly like a
page that never borrowed at all.
"""

from __future__ import annotations

import re
import unittest

from ._support import PAGE_HTML

# Names out of FilaMan's own DOM, and the two calls that were used to lift nodes
# out of a foreign document. None of them has a legitimate reason to appear in
# a page that is a document of its own.
BORROWED_FROM_FILAMAN = ("#fm-page", "aside.fm-sidebar", "#fm-confirm-overlay", "/_astro/")
LIFTS_FOREIGN_NODES = ("DOMParser", "importNode")


class PageStandsOnItsOwnTest(unittest.TestCase):
    def setUp(self):
        self.html = PAGE_HTML.read_text(encoding="utf-8")
        # Comments may name these mechanisms, the code may not. Section 8.3 of
        # the design explains the history and has to stay free to do so.
        self.code = re.sub(r"/\*.*?\*/", "", self.html, flags=re.DOTALL)

    def test_the_page_names_no_element_of_filamans_shell(self):
        for name in BORROWED_FROM_FILAMAN:
            with self.subTest(name=name):
                self.assertNotIn(name, self.code)

    def test_the_page_lifts_no_nodes_out_of_a_foreign_document(self):
        for call in LIFTS_FOREIGN_NODES:
            with self.subTest(call=call):
                self.assertNotIn(call, self.code)

    def test_the_page_knows_when_it_sits_in_filamans_shell(self):
        # The one thing left of the mechanism, and the whole of it.
        self.assertIn("window.self !== window.top", self.code)
        self.assertIn("in-fm-frame", self.code)

    def test_the_way_back_is_hidden_only_inside_the_shell(self):
        # Opened directly the page is the top window and this link is the only
        # way back, so it must not be hidden by anything weaker than the class.
        self.assertRegex(self.html, r"\.in-fm-frame \.back-link \{[^}]*display:\s*none")
        self.assertIn('class="back-link"', self.html)


if __name__ == "__main__":
    unittest.main()
