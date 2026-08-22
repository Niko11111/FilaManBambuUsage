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

MAX_FILE_SIZE = 1 * 1024 * 1024

# Never packed, regardless of extension.
EXCLUDE_DIRS = {"__pycache__", ".git", ".idea", ".vscode"}

# Files the operating system writes into the folder by itself. They are not
# part of the package by any definition, and FilaMan rejects every hidden file,
# so a Finder window on the package folder must not be able to break a build.
# Anything else hidden is a mistake and fails validation, see
# validate_hidden_files().
EXCLUDE_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}


class ValidationError(Exception):
    """A packaging fault FilaMan would reject in exactly the same way."""


def iter_plugin_files(plugin_dir: Path) -> tuple[list[Path], list[Path]]:
    """Every file that goes into the package, in a stable order.

    Returns the files to pack and the operating system files left out, so the
    build can name them rather than dropping them silently.
    """
    files = []
    dropped = []
    for path in sorted(plugin_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in path.relative_to(plugin_dir).parts):
            continue
        if path.name in EXCLUDE_FILES:
            dropped.append(path)
            continue
        files.append(path)
    return files, dropped


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


def validate_hidden_files(plugin_dir: Path, files: list[Path]) -> None:
    """No hidden file survives FilaMan's check, whatever its extension.

    ``.DS_Store`` used to reach an upload this way and was refused there,
    which is exactly the kind of surprise this script exists to prevent. The
    operating system's own files are left out while packing, see
    EXCLUDE_FILES; anything else hidden is somebody's decision and fails here.
    """
    for path in files:
        if path.name.startswith("."):
            rel = path.relative_to(plugin_dir)
            raise ValidationError(
                f"Hidden file not allowed: {rel}. FilaMan refuses the upload "
                "with hidden_file."
            )


def validate_file_sizes(plugin_dir: Path, files: list[Path]) -> None:
    """FilaMan's per file limit, which is far below the limit for the ZIP."""
    for path in files:
        size = path.stat().st_size
        if size > MAX_FILE_SIZE:
            rel = path.relative_to(plugin_dir)
            raise ValidationError(
                f"File too large: {rel} ({size} bytes), the limit for a single "
                f"file is {MAX_FILE_SIZE}"
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


def validate(plugin_dir: Path) -> tuple[dict, list[Path], list[Path]]:
    """Run every check, returning the manifest, the file list and what was left out."""
    if not plugin_dir.is_dir():
        raise ValidationError(f"Plugin directory not found: {plugin_dir}")

    manifest = validate_manifest(plugin_dir)
    validate_structure(plugin_dir, manifest["plugin_type"])

    files, dropped = iter_plugin_files(plugin_dir)
    if not files:
        raise ValidationError("No files found to package")

    validate_extensions(plugin_dir, files)
    validate_hidden_files(plugin_dir, files)
    validate_file_sizes(plugin_dir, files)
    check_version_consistency(plugin_dir, manifest)

    return manifest, files, dropped


def build(
    plugin_dir: Path,
    manifest: dict,
    files: list[Path],
    force: bool = False,
    dist_dir: Path | None = None,
) -> Path:
    """Write the ZIP. The plugin folder is kept as the single top level entry.

    Refuses to overwrite an existing build of the same version, because two
    different packages under one version number is a debugging trap: the
    installed files and the version an instance reports no longer agree, and the
    difference is invisible from the outside. Learned the hard way on 0.1.2.
    Bump the version, or pass --force when the previous build was never
    installed anywhere.
    """
    target = dist_dir or DIST_DIR
    target.mkdir(exist_ok=True, parents=True)
    out_path = target / f"{manifest['plugin_key']}-{manifest['version']}.zip"

    if out_path.exists() and not force:
        raise ValidationError(
            f"{out_path.name} already exists. Bump the version in plugin.json and "
            "__init__.py, or pass --force if that build never left this machine."
        )

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

        # The operating system's own file must not reach the ZIP, and must not
        # break the build either. This is what a Finder window leaves behind.
        (staged / ".DS_Store").write_bytes(b"\x00\x00\x01Bud1")
        manifest, files, dropped = validate(staged)
        if [path.name for path in dropped] != [".DS_Store"]:
            print("FAILED: .DS_Store should have been left out of the package")
            return 1
        if any(path.name == ".DS_Store" for path in files):
            print("FAILED: .DS_Store should not be among the packed files")
            return 1

        # Any other hidden file is somebody's decision and has to be refused,
        # because FilaMan refuses it at the upload.
        (staged / ".env").write_text("SECRET=1\n", encoding="utf-8")
        try:
            validate(staged)
        except ValidationError:
            pass
        else:
            print("FAILED: a hidden .env should have been rejected")
            return 1
        (staged / ".env").unlink()

        # Building the same version twice has to be refused. Two different
        # packages under one version number is what makes an installed plugin
        # and the version it reports disagree, with nothing visible from outside.
        manifest, files, _ = validate(staged)
        sandbox = Path(tmp) / "dist"
        build(staged, manifest, files, dist_dir=sandbox)
        try:
            build(staged, manifest, files, dist_dir=sandbox)
        except ValidationError:
            pass
        else:
            print("FAILED: a second build of the same version should have been refused")
            return 1

        # With --force it has to go through, for a build that never left here.
        build(staged, manifest, files, force=True, dist_dir=sandbox)

        # With a forbidden extension it has to be rejected.
        (staged / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        try:
            validate(staged)
        except ValidationError as exc:
            print("Self test passed.")
            print("  clean package             accepted")
            print("  .DS_Store                 left out, build unaffected")
            print("  package with .env         rejected as a hidden file")
            print("  same version twice        refused, --force accepted")
            print(f"  package with preview.png  rejected: {str(exc).splitlines()[0]}")
            return 0

        print("FAILED: preview.png should have been rejected")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="validate only, write nothing")
    parser.add_argument("--selftest", action="store_true", help="test the validation itself")
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing build of the same version",
    )
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    try:
        manifest, files, dropped = validate(PLUGIN_DIR)
    except ValidationError as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1

    print(f"{manifest['name']} {manifest['version']} ({manifest['plugin_key']}, {manifest['plugin_type']})")
    print(f"  {len(files)} files checked, every extension allowed")
    for path in dropped:
        print(f"  left out, the operating system wrote it: {path.relative_to(PLUGIN_DIR)}")

    if args.check:
        print("  validation only, nothing written")
        return 0

    try:
        out_path = build(PLUGIN_DIR, manifest, files, force=args.force)
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
