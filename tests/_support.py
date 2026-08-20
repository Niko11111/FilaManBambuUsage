"""Shared paths for the test suite.

Kept in one place so a directory move does not have to be fixed in four files.
"""

from __future__ import annotations

from pathlib import Path

# The database layer cannot be exercised on the standard library alone. These
# three are optional, documented in pyproject.toml, and the tests that need them
# skip themselves when they are absent, so the suite still runs without setup.
try:
    import aiosqlite  # noqa: F401
    import pydantic  # noqa: F401
    import sqlalchemy  # noqa: F401

    HAS_TEST_DEPENDENCIES = True
except ImportError:  # pragma: no cover, depends on the environment
    HAS_TEST_DEPENDENCIES = False

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "bambu_usage"
LOCALES_DIR = PACKAGE_DIR / "locales"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
PAGE_HTML = PACKAGE_DIR / "page.html"
REFERENCE_LANGUAGE = "en"


def flatten_keys(data: dict, prefix: str = "") -> set[str]:
    """Turn a nested dictionary into the set of its dotted leaf keys."""
    keys: set[str] = set()
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            keys |= flatten_keys(value, dotted + ".")
        else:
            keys.add(dotted)
    return keys
