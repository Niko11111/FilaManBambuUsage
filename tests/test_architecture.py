"""Tests for the architecture checker itself.

A checker nobody verifies is a checker that silently passes everything. These
tests prove it both accepts the real package and rejects each thing it claims to
catch.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from ._support import PACKAGE_DIR, REPO_ROOT


def load_checker():
    """Import tools/check_architecture.py, which is a script and not a package.

    The module has to be registered in sys.modules before it is executed,
    otherwise dataclass resolution cannot find the class it is defining.
    """
    path = REPO_ROOT / "tools" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("check_architecture", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = load_checker()


def check_source(file_name: str, source: str):
    """Run the checker against synthetic source saved under *file_name*.

    Rules are selected by file name, so a temporary file is enough and no copy
    of the package is needed.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / file_name
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return checker.check_file(path)


def rules_of(violations) -> set[str]:
    return {v.rule for v in violations}


class RealPackageTest(unittest.TestCase):
    def test_the_package_passes(self):
        violations = [v for path in sorted(PACKAGE_DIR.glob("*.py")) for v in checker.check_file(path)]
        self.assertEqual([str(v) for v in violations], [])

    def test_every_module_is_covered_by_a_rule_or_deliberately_not(self):
        # __init__.py has no boundary of its own; every other module must be
        # named in the table, or a new module could quietly escape the checker.
        modules = {p.name for p in PACKAGE_DIR.glob("*.py")} - {"__init__.py"}
        self.assertEqual(modules - set(checker.FORBIDDEN_IMPORTS), set())


class ForbiddenImportTest(unittest.TestCase):
    def test_service_may_not_import_tracker(self):
        violations = check_source("service.py", "from . import tracker\n")
        self.assertIn("import", rules_of(violations))

    def test_service_may_not_import_the_printer_library(self):
        violations = check_source("service.py", "import bambulabs_api\n")
        self.assertIn("import", rules_of(violations))

    def test_router_may_not_reach_at_the_printer(self):
        violations = check_source("router.py", "from bambulabs_api import Printer\n")
        self.assertIn("import", rules_of(violations))

    def test_threemf_may_not_import_any_plugin_module(self):
        violations = check_source("threemf.py", "from .models import prints_table\n")
        self.assertIn("import", rules_of(violations))

    def test_an_allowed_import_passes(self):
        self.assertEqual(check_source("service.py", "from .schemas import PrintRecord\n"), [])

    def test_the_reason_is_reported(self):
        violations = check_source("service.py", "from . import tracker\n")
        self.assertIn("where events come from", violations[0].detail)


class SharedStateTest(unittest.TestCase):
    def test_module_level_dict_is_rejected(self):
        # The OpenSpoolMan failure mode, verbatim.
        violations = check_source("service.py", "SPOOLS = {}\nspools = {}\n")
        self.assertIn("shared-state", rules_of(violations))

    def test_module_level_list_is_rejected(self):
        self.assertIn("shared-state", rules_of(check_source("service.py", "queue = []\n")))

    def test_global_on_a_public_name_is_rejected(self):
        source = """
            def f():
                global cache
                cache = {}
            """
        self.assertIn("shared-state", rules_of(check_source("service.py", source)))

    def test_a_private_cache_is_allowed(self):
        # A memoisation cache behind functions of one module is a known pattern,
        # not the disease. The underscore is what marks the difference.
        source = """
            _cached = {}

            def load():
                global _cached
                return _cached
            """
        self.assertEqual(check_source("service.py", source), [])

    def test_definition_objects_are_not_state(self):
        # MetaData(), Table() and APIRouter() are declarations, not state.
        source = """
            from sqlalchemy import MetaData
            metadata = MetaData()
            """
        self.assertEqual(rules_of(check_source("models.py", source)), set())


class BareExceptTest(unittest.TestCase):
    def test_bare_except_is_rejected(self):
        source = """
            def f():
                try:
                    pass
                except:
                    pass
            """
        self.assertIn("bare-except", rules_of(check_source("service.py", source)))

    def test_a_named_exception_passes(self):
        source = """
            def f():
                try:
                    pass
                except OSError:
                    pass
            """
        self.assertEqual(check_source("service.py", source), [])


if __name__ == "__main__":
    unittest.main()
