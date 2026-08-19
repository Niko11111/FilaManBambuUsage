#!/usr/bin/env python3
"""Build and validate the installable plugin ZIP.

Every check here mirrors FilaMan's own validation in
``backend/app/services/plugin_service.py``. The point is that a package which
passes this script is accepted by FilaMan, so mistakes surface at build time
instead of during an upload.

Usage:
    python3 tools/build_zip.py             build dist/<key>-<version>.zip
    python3 tools/build_zip.py --check     validate only, write nothing
    python3 tools/build_zip.py --selftest  prove the validation actually bites
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "bambu_usage"
DIST_DIR = REPO_ROOT / "dist"

# --------------------------------------------------------------------------
# Mirrored from FilaMan's plugin_service.py. Keep in sync when FilaMan changes.
# --------------------------------------------------------------------------

MAX_ZIP_SIZE = 10 * 1024 * 1024

ALLOWED_EXTENSIONS = {
    ".py",
    ".json",
    ".md",
    ".txt",
    ".cfg",
    ".ini",
    ".yaml",
    ".yml",
    ".toml",
    ".html",
}

REQUIRED_MANIFEST_FIELDS = {"plugin_key", "name", "version", "description", "author"}
REQUIRED_DRIVER_FIELDS = {"driver_key"}
VALID_PLUGIN_TYPES = {"driver", "import", "integration"}

PLUGIN_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,49}$")
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$")

# Never packed, regardless of extension.
EXCLUDE_DIRS = {"__pycache__", ".git", ".idea", ".vscode"}


class ValidationError(Exception):
    """A packaging fault FilaMan would reject in exactly the same way."""


def iter_plugin_files(plugin_dir: Path) -> list[Path]:
    """Every file that goes into the package, in a stable order."""
    files = []
    for path in sorted(plugin_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in path.relative_to(plugin_dir).parts):
            continue
        files.append(path)
    return files


def validate_extensions(plugin_dir: Path, files: list[Path]) -> None:
    """The extension allow list. Every .png, .css, .js and .svg fails here.

    FilaMan checks ``if suffix and suffix not in ALLOWED_EXTENSIONS``, so a file
    with no extension at all passes. That is mirrored deliberately, so this
    check is never stricter than the original.
    """
    for path in files:
        suffix = path.suffix.lower()
        if suffix and suffix not in ALLOWED_EXTENSIONS:
            rel = path.relative_to(plugin_dir)
            raise ValidationError(
                f"Forbidden file extension: {rel} ({suffix}). "
                f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}. "
                "Images and script files have to be served at runtime through the "
                "router, see docs/01_Design.md section 8.1."
            )


def validate_structure(plugin_dir: Path, plugin_type: str) -> None:
    """The files required for this plugin type."""
    required = ["plugin.json", "__init__.py"]
    if plugin_type == "driver":
        required.append("driver.py")

    for name in required:
        if not (plugin_dir / name).is_file():
            raise ValidationError(f"Required file missing: {name}")


def validate_manifest(plugin_dir: Path) -> dict:
    """Read the manifest and check it against FilaMan's rules."""
    manifest_path = plugin_dir / "plugin.json"
    if not manifest_path.is_file():
        raise ValidationError("plugin.json is missing")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"plugin.json is not valid JSON: {exc}") from exc

    missing = REQUIRED_MANIFEST_FIELDS - manifest.keys()
    if missing:
        raise ValidationError(f"Required manifest fields missing: {', '.join(sorted(missing))}")

    plugin_type = manifest.get("plugin_type", "driver")
    if plugin_type not in VALID_PLUGIN_TYPES:
        raise ValidationError(
            f"Invalid plugin_type '{plugin_type}' "
            f"(allowed: {', '.join(sorted(VALID_PLUGIN_TYPES))})"
        )
    manifest["plugin_type"] = plugin_type

    plugin_key = manifest["plugin_key"]
    if not PLUGIN_KEY_PATTERN.match(plugin_key):
        raise ValidationError(
            f"plugin_key '{plugin_key}' does not match {PLUGIN_KEY_PATTERN.pattern}"
        )

    version = manifest["version"]
    if not SEMVER_PATTERN.match(str(version)):
        raise ValidationError(f"version '{version}' is not valid semver")

    if plugin_type == "driver":
        for field in REQUIRED_DRIVER_FIELDS:
            if not manifest.get(field):
                raise ValidationError(f"Required field '{field}' missing (needed for type 'driver')")

    if plugin_key != plugin_dir.name:
        raise ValidationError(
            f"plugin_key '{plugin_key}' and folder name '{plugin_dir.name}' "
            "have to agree, because FilaMan imports as app.plugins.<key>"
        )

    return manifest


def check_version_consistency(plugin_dir: Path, manifest: dict) -> None:
    """Hold __version__ in the package against the manifest version.

    Not a FilaMan rule, but exactly the kind of divergence you only notice after
    shipping.
    """
    init_text = (plugin_dir / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init_text, re.MULTILINE)
    if not match:
        raise ValidationError("__version__ is missing from __init__.py")
    if match.group(1) != manifest["version"]:
        raise ValidationError(
            f"Versions disagree: __init__.py says {match.group(1)}, "
            f"plugin.json says {manifest['version']}"
        )


def validate(plugin_dir: Path) -> tuple[dict, list[Path]]:
    """Run every check, returning the manifest and the file list."""
    if not plugin_dir.is_dir():
        raise ValidationError(f"Plugin directory not found: {plugin_dir}")

    manifest = validate_manifest(plugin_dir)
    validate_structure(plugin_dir, manifest["plugin_type"])

    files = iter_plugin_files(plugin_dir)
    if not files:
        raise ValidationError("No files found to package")

    validate_extensions(plugin_dir, files)
    check_version_consistency(plugin_dir, manifest)

    return manifest, files


def build(plugin_dir: Path, manifest: dict, files: list[Path]) -> Path:
    """Write the ZIP. The plugin folder is kept as the single top level entry."""
    DIST_DIR.mkdir(exist_ok=True)
    out_path = DIST_DIR / f"{manifest['plugin_key']}-{manifest['version']}.zip"

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            arcname = Path(plugin_dir.name) / path.relative_to(plugin_dir)
            zf.write(path, arcname.as_posix())

    size = out_path.stat().st_size
    if size > MAX_ZIP_SIZE:
        out_path.unlink()
        raise ValidationError(
            f"ZIP is too large: {size} bytes, the limit is {MAX_ZIP_SIZE}"
        )

    return out_path


def selftest() -> int:
    """Prove the validation actually bites.

    Copies the package, slips a .png into it and expects a failure. A check that
    is never seen failing is a check nobody can trust.
    """
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / PLUGIN_DIR.name
        shutil.copytree(PLUGIN_DIR, staged)

        # A clean package has to pass.
        try:
            validate(staged)
        except ValidationError as exc:
            print(f"FAILED: a clean package was rejected: {exc}")
            return 1

        # With a forbidden extension it has to be rejected.
        (staged / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        try:
            validate(staged)
        except ValidationError as exc:
            print("Self test passed.")
            print("  clean package          accepted")
            print(f"  package with preview.png rejected: {str(exc).splitlines()[0]}")
            return 0

        print("FAILED: preview.png should have been rejected")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="validate only, write nothing")
    parser.add_argument("--selftest", action="store_true", help="test the validation itself")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    try:
        manifest, files = validate(PLUGIN_DIR)
    except ValidationError as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1

    print(f"{manifest['name']} {manifest['version']} ({manifest['plugin_key']}, {manifest['plugin_type']})")
    print(f"  {len(files)} files checked, every extension allowed")

    if args.check:
        print("  validation only, nothing written")
        return 0

    try:
        out_path = build(PLUGIN_DIR, manifest, files)
    except ValidationError as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1

    size_kb = out_path.stat().st_size / 1024
    print(f"  written: {out_path.relative_to(REPO_ROOT)} ({size_kb:.1f} KB)")
    print()
    print("Upload it in FilaMan under Admin, Plugins.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
