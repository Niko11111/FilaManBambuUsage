"""Tests for the translations.

The most valuable file in this suite. A forgotten key is invisible in review and
only shows up as a blank label in somebody else's language, so it is checked
mechanically instead.
"""

from __future__ import annotations

import json
import re
import unittest

from ._support import LOCALES_DIR, PACKAGE_DIR, PAGE_HTML, REFERENCE_LANGUAGE, flatten_keys

# Interpolation placeholders, matching FilaMan's own {name} syntax.
PLACEHOLDER_PATTERN = re.compile(r"\{([a-zA-Z0-9_]+)\}")

# data-i18n, data-i18n-placeholder and data-i18n-title in the page.
HTML_KEY_PATTERN = re.compile(r'data-i18n(?:-placeholder|-title)?="([^"]+)"')

# Quoted dotted keys anywhere in the page script. Matching the t( call itself
# would miss the ternary form t(flag ? 'a.b' : 'c.d'), which is exactly the sort
# of key that then goes unchecked.
SCRIPT_KEY_PATTERN = re.compile(r"'([a-z][a-zA-Z0-9]*(?:\.[a-zA-Z0-9]+)+)'")

# Error codes the backend hands out for the page to translate.
ERROR_CODE_PATTERN = re.compile(r'"code":\s*"([^"]+)"')


def load(language: str) -> dict:
    return json.loads((LOCALES_DIR / f"{language}.json").read_text(encoding="utf-8"))


def languages() -> list[str]:
    return sorted(path.stem for path in LOCALES_DIR.glob("*.json"))


def leaf_values(data: dict, prefix: str = "") -> dict[str, str]:
    flat = {}
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(leaf_values(value, dotted + "."))
        else:
            flat[dotted] = value
    return flat


class LocaleFilesTest(unittest.TestCase):
    def test_reference_language_exists(self):
        self.assertIn(REFERENCE_LANGUAGE, languages())

    def test_more_than_one_language_ships(self):
        # A single language never exercises the fallback path.
        self.assertGreater(len(languages()), 1)

    def test_every_file_is_valid_json(self):
        for language in languages():
            with self.subTest(language=language):
                self.assertIsInstance(load(language), dict)

    def test_every_value_is_a_string(self):
        for language in languages():
            for key, value in leaf_values(load(language)).items():
                with self.subTest(language=language, key=key):
                    self.assertIsInstance(value, str)
                    self.assertNotEqual(value.strip(), "")


class KeyCoverageTest(unittest.TestCase):
    def setUp(self):
        self.reference = flatten_keys(load(REFERENCE_LANGUAGE))

    def test_every_language_covers_the_reference(self):
        for language in languages():
            if language == REFERENCE_LANGUAGE:
                continue
            with self.subTest(language=language):
                missing = self.reference - flatten_keys(load(language))
                self.assertEqual(missing, set(), f"{language}.json is missing keys")

    def test_no_language_carries_keys_the_reference_lacks(self):
        # An orphan key is either a typo or a leftover, and both should be seen.
        for language in languages():
            if language == REFERENCE_LANGUAGE:
                continue
            with self.subTest(language=language):
                extra = flatten_keys(load(language)) - self.reference
                self.assertEqual(extra, set(), f"{language}.json has unknown keys")

    def test_placeholders_survive_translation(self):
        # Dropping {driver} while translating leaves the sentence incomplete and
        # nothing else would notice.
        reference = leaf_values(load(REFERENCE_LANGUAGE))
        for language in languages():
            if language == REFERENCE_LANGUAGE:
                continue
            translated = leaf_values(load(language))
            for key, text in reference.items():
                expected = set(PLACEHOLDER_PATTERN.findall(text))
                actual = set(PLACEHOLDER_PATTERN.findall(translated.get(key, "")))
                with self.subTest(language=language, key=key):
                    self.assertEqual(expected, actual)


class UsageTest(unittest.TestCase):
    """Keys used in the page and in the backend have to exist, and vice versa."""

    def setUp(self):
        self.reference = flatten_keys(load(REFERENCE_LANGUAGE))
        self.html = PAGE_HTML.read_text(encoding="utf-8")
        self.used = set(HTML_KEY_PATTERN.findall(self.html))
        self.used |= set(SCRIPT_KEY_PATTERN.findall(self.html))
        self.codes = set()
        for module in PACKAGE_DIR.glob("*.py"):
            self.codes |= set(ERROR_CODE_PATTERN.findall(module.read_text(encoding="utf-8")))

    def test_every_key_used_in_the_page_exists(self):
        self.assertEqual(self.used - self.reference, set())

    def test_every_error_code_the_backend_emits_exists(self):
        self.assertEqual(self.codes - self.reference, set())

    def test_no_key_is_defined_but_unused(self):
        # Dead keys are dead code, and translators pay for them.
        self.assertEqual(self.reference - (self.used | self.codes), set())

    def test_every_marked_attribute_is_actually_translated(self):
        # A key can be spelled right, exist in every language and still never
        # reach the page, because translatePage only looks at the attributes it
        # knows. That is how the search box shipped without a placeholder.
        marked = set(re.findall(r"data-i18n(-[a-z]+)?=", self.html))
        handled = set(re.findall(r"querySelectorAll\('\[data-i18n(-[a-z]+)?\]'\)", self.html))
        self.assertEqual(marked - handled, set())

    def test_the_page_carries_no_hardcoded_sentences(self):
        # A data-i18n element must not also hold literal prose, or the two
        # disagree the moment a translation changes. If the dictionary fails to
        # load, t() falls back to showing the key, so nothing goes blank.
        for match in re.finditer(r'data-i18n="([^"]+)"[^>]*>([^<]*)<', self.html):
            key, literal = match.group(1), match.group(2).strip()
            with self.subTest(key=key):
                self.assertEqual(literal, "", f"{key} also holds literal text")


if __name__ == "__main__":
    unittest.main()
