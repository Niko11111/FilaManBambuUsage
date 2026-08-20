#!/usr/bin/env python3
"""Enforce the module boundaries this project is built on.

Layering is only real if something checks it. This script parses every module
under bambu_usage/ with ast and fails when a rule is broken. The rules are the
written half of what each module docstring declares and what
docs/01_Design.md section 11 describes; the three must always agree.

Checked here:

  imports        a module may not reach across the layers it is separated from
  shared state   no module level mutable container, and no `global`, unless the
                 name is private to its module
  bare except    an exception you cannot name is an exception you cannot handle

Usage:
    python3 tools/check_architecture.py           report and exit non-zero on failure
    python3 tools/check_architecture.py --list    print the rule table and exit
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "bambu_usage"

# Every module of the plugin, used to express "any other plugin module".
PLUGIN_MODULES = {
    "tracker",
    "threemf",
    "service",
    "models",
    "schemas",
    "settings",
    "router",
    "filaman",
    "store",
    "supervisor",
    "rules",
    "views",
    "report",
}

# FilaMan's own package. Only filaman.py may reach into it, so that a FilaMan
# update breaks one file of this plugin instead of five. router.py is the one
# exception, and a forced one: FastAPI resolves dependencies while the route
# decorators run, so the authentication dependencies cannot be fetched lazily.
FILAMAN_PACKAGE = "app"
FILAMAN_PACKAGE_REASON = "only filaman.py talks to FilaMan's internals"

# What each module must not import, and why. The reason is printed on failure,
# because a rule nobody understands gets deleted rather than followed.
FORBIDDEN_IMPORTS: dict[str, dict[str, str]] = {
    "service.py": {
        "tracker": "business logic must not depend on where events come from",
        "router": "business logic must not depend on the HTTP layer",
        "bambulabs_api": "business logic must stay callable without a printer",
        "fastapi": "business logic must stay callable without a web framework",
        FILAMAN_PACKAGE: FILAMAN_PACKAGE_REASON,
    },
    "store.py": {
        "tracker": "the queries must not depend on the runtime",
        "router": "the queries must not depend on the HTTP layer",
        "service": "the queries must not depend on the business logic",
        "filaman": "our own tables and FilaMan's are read by different modules",
        "bambulabs_api": "no printer access from the database layer",
        "fastapi": "the database layer stays callable without a web framework",
        FILAMAN_PACKAGE: FILAMAN_PACKAGE_REASON,
    },
    "filaman.py": {
        **{m: "the seam to FilaMan must not depend on the plugin" for m in PLUGIN_MODULES},
        "bambulabs_api": "reading FilaMan has nothing to do with reaching a printer",
        "fastapi": "the seam to FilaMan must stay callable without a web framework",
    },
    "report.py": {
        **{m: "reading a report needs nothing but the report" for m in PLUGIN_MODULES},
        "fastapi": "reading a report stays pure",
        "sqlalchemy": "reading a report stays pure",
        "bambulabs_api": "reading a report is not talking to a printer",
        FILAMAN_PACKAGE: FILAMAN_PACKAGE_REASON,
    },
    "views.py": {
        "tracker": "the read side must not depend on the runtime",
        "supervisor": "the read side must not depend on the runtime",
        "router": "the read side must not depend on the HTTP layer",
        "service": "reading the history is not the booking path",
        "bambulabs_api": "no printer access from the read side",
        "fastapi": "the read side stays callable without a web framework",
        FILAMAN_PACKAGE: FILAMAN_PACKAGE_REASON,
    },
    "rules.py": {
        # threemf is missing from this list on purpose: it is pure as well, and
        # rules names one of its dataclasses in a type annotation. Everything
        # else would drag a database, a printer or FilaMan into arithmetic.
        **{
            m: "the arithmetic stays free of everything it is decided without"
            for m in PLUGIN_MODULES - {"threemf"}
        },
        "fastapi": "the arithmetic stays pure",
        "sqlalchemy": "the arithmetic stays pure",
        "bambulabs_api": "the arithmetic stays pure",
        FILAMAN_PACKAGE: FILAMAN_PACKAGE_REASON,
    },
    "threemf.py": {
        **{m: "threemf stays pure, so it can be tested against a file on disk" for m in PLUGIN_MODULES},
        "fastapi": "threemf stays pure",
        "sqlalchemy": "threemf stays pure",
        FILAMAN_PACKAGE: FILAMAN_PACKAGE_REASON,
    },
    "models.py": {
        "tracker": "the table definitions must not depend on the runtime",
        "router": "the table definitions must not depend on the HTTP layer",
        "service": "the table definitions must not depend on the business logic",
        "schemas": "database layout and wire format are deliberately separate",
        FILAMAN_PACKAGE: FILAMAN_PACKAGE_REASON,
    },
    "schemas.py": {
        "tracker": "the wire format must not depend on the runtime",
        "router": "the wire format must not depend on the HTTP layer",
        "service": "the wire format must not depend on the business logic",
        "models": "database layout and wire format are deliberately separate",
        FILAMAN_PACKAGE: FILAMAN_PACKAGE_REASON,
    },
    "settings.py": {
        "tracker": "settings must not depend on the runtime",
        "router": "settings must not depend on the HTTP layer",
        "service": "settings must not depend on the business logic",
        FILAMAN_PACKAGE: FILAMAN_PACKAGE_REASON,
    },
    "router.py": {
        "bambulabs_api": "no printer access from inside an HTTP handler",
        "tracker": "endpoints go through service, never straight at the listeners",
    },
    "tracker.py": {
        "router": "the runtime must not depend on the HTTP layer",
        "supervisor": "a listener must not know who decided to run it",
        FILAMAN_PACKAGE: FILAMAN_PACKAGE_REASON,
    },
    "supervisor.py": {
        "router": "the runtime must not depend on the HTTP layer",
        "service": "starting listeners is not business logic, that goes through tracker",
        "threemf": "the supervisor never reads a 3MF, the listener does",
        "bambulabs_api": "only a listener talks to a printer",
        "fastapi": "the runtime stays callable without a web framework",
        FILAMAN_PACKAGE: FILAMAN_PACKAGE_REASON,
    },
}

# Assigning one of these at module level creates shared mutable state. This is
# the OpenSpoolMan failure mode: PRINTER_STATE, PENDING_PRINT_METADATA and
# SPOOLS are module level dicts mutated from MQTT callbacks, and that is what
# makes it unreadable and untestable.
MUTABLE_FACTORIES = {"dict", "list", "set"}


@dataclass
class Violation:
    """One broken rule, with enough context to fix it without asking."""

    file: str
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"  {self.file}:{self.line}  [{self.rule}] {self.detail}"


def _imported_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Return the module names an import statement pulls in.

    A relative import is reduced to its module name, so `from .schemas import X`
    and `from . import schemas` both come back as "schemas".
    """
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]

    if node.level:
        # Relative: `from .schemas import X` has module "schemas",
        # `from . import schemas, service` carries them as names instead.
        if node.module:
            return [node.module.split(".")[0]]
        return [alias.name for alias in node.names]

    return [node.module.split(".")[0]] if node.module else []


def _is_mutable_literal(value: ast.expr) -> bool:
    """True for `{}`, `[]`, `set()`, `dict()`, `list()` and their populated forms."""
    if isinstance(value, (ast.Dict, ast.List, ast.Set)):
        return True
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in MUTABLE_FACTORIES
    )


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    """Return the plain names a module level assignment binds."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [t.id for t in targets if isinstance(t, ast.Name)]


def check_imports(path: Path, tree: ast.AST) -> list[Violation]:
    """Report imports that cross a boundary this module is separated from."""
    forbidden = FORBIDDEN_IMPORTS.get(path.name, {})
    if not forbidden:
        return []

    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for name in _imported_names(node):
            reason = forbidden.get(name)
            if reason:
                violations.append(
                    Violation(path.name, node.lineno, "import", f"imports '{name}': {reason}")
                )
    return violations


def check_shared_state(path: Path, tree: ast.AST) -> list[Violation]:
    """Report module level mutable state and `global` on a public name.

    A private cache behind functions of the same module is allowed, which is why
    the rule only bites on names without a leading underscore. What it targets is
    state shared across modules and mutated from callbacks.
    """
    violations = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            if not _is_mutable_literal(node.value):
                continue
            for name in _assigned_names(node):
                if name.startswith("_") or name.isupper():
                    continue
                violations.append(
                    Violation(
                        path.name,
                        node.lineno,
                        "shared-state",
                        f"module level mutable '{name}'. Put it on an object or in "
                        "the database, or make it private with a leading underscore",
                    )
                )

    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            public = [n for n in node.names if not n.startswith("_")]
            if public:
                violations.append(
                    Violation(
                        path.name,
                        node.lineno,
                        "shared-state",
                        f"global {', '.join(public)}. Rebinding public module state "
                        "is the OpenSpoolMan failure mode this project avoids",
                    )
                )

    return violations


def check_bare_except(path: Path, tree: ast.AST) -> list[Violation]:
    """Report `except:` without an exception type."""
    return [
        Violation(path.name, node.lineno, "bare-except", "name the exception you are handling")
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.type is None
    ]


def check_file(path: Path) -> list[Violation]:
    """Run every check against one module."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [Violation(path.name, exc.lineno or 0, "syntax", str(exc.msg))]

    return (
        check_imports(path, tree)
        + check_shared_state(path, tree)
        + check_bare_except(path, tree)
    )


def print_rules() -> None:
    """Print the import table, so the rules can be read without the code."""
    for module in sorted(FORBIDDEN_IMPORTS):
        print(module)
        for name, reason in sorted(FORBIDDEN_IMPORTS[module].items()):
            print(f"    must not import {name:<16} {reason}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the plugin's module boundaries.")
    parser.add_argument("--list", action="store_true", help="print the rule table and exit")
    args = parser.parse_args()

    if args.list:
        print_rules()
        return 0

    if not PACKAGE_DIR.is_dir():
        print(f"Package directory not found: {PACKAGE_DIR}", file=sys.stderr)
        return 1

    files = sorted(PACKAGE_DIR.glob("*.py"))
    violations = [v for path in files for v in check_file(path)]

    if violations:
        print(f"Architecture check failed, {len(violations)} violation(s):\n")
        for violation in violations:
            print(violation)
        print("\nThe rules live in tools/check_architecture.py, in each module's")
        print("docstring, and in docs/01_Design.md section 11. Moving a boundary")
        print("means changing all three on purpose.")
        return 1

    print(f"Architecture check passed, {len(files)} modules, no boundary crossed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
