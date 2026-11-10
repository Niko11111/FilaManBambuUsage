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
from xml.etree import ElementTree

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
    ".svg",
}

REQUIRED_MANIFEST_FIELDS = {"plugin_key", "name", "version", "description", "author"}
REQUIRED_DRIVER_FIELDS = {"driver_key"}
VALID_PLUGIN_TYPES = {"driver", "import", "integration"}

PLUGIN_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,49}$")
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$")

MAX_FILE_SIZE = 1 * 1024 * 1024

MAX_ICON_BYTES = 4 * 1024
MAX_LEGACY_SVG_BYTES = 64 * 1024
SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"
SVG_ELEMENTS = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "title", "desc"}
SVG_ATTRIBUTES = {
    "viewBox", "width", "height", "fill", "stroke", "stroke-width",
    "stroke-linecap", "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray",
    "stroke-dashoffset", "fill-rule", "clip-rule", "opacity", "fill-opacity",
    "stroke-opacity", "transform", "d", "x", "y", "x1", "y1", "x2", "y2",
    "cx", "cy", "r", "rx", "ry", "points", "preserveAspectRatio",
}


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


def validate_icon(plugin_dir: Path, icon: str, *, max_bytes: int = MAX_ICON_BYTES) -> None:
    """Mirror FilaMan's plugin_icon_service.py without requiring its app runtime."""
    try:
        if (
            not isinstance(icon, str)
            or len(icon) > 250
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*\.svg", icon)
            or any(part in ("", ".", "..") for part in icon.split("/"))
            or plugin_dir.is_symlink()
        ):
            raise ValueError("icon must be a relative .svg path within the plugin package")
        path = (plugin_dir / icon).resolve()
        if not path.is_relative_to(plugin_dir.resolve()):
            raise ValueError("icon must stay within the plugin package")
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"icon must be at most {max_bytes // 1024} KiB")
        source = data.decode("utf-8")
        if "<!" in source or re.search(r"<\?(?!xml\s)", source, re.IGNORECASE):
            raise ValueError("icon must not contain declarations, entities or comments")
        try:
            root = ElementTree.fromstring(source)
        except ElementTree.ParseError as error:
            raise ValueError("icon must be valid SVG XML") from error
        if root.tag != SVG_NAMESPACE + "svg":
            raise ValueError("icon must have an SVG root with the SVG namespace")
        for node in root.iter():
            if node.tag not in {SVG_NAMESPACE + tag for tag in SVG_ELEMENTS}:
                raise ValueError("icon contains an unsupported SVG element")
            for attribute, value in node.attrib.items():
                if attribute not in SVG_ATTRIBUTES or re.search(r"url\s*\(|[\\\x00-\x1f]", value, re.IGNORECASE):
                    raise ValueError("icon contains an unsupported SVG attribute or reference")
    except (OSError, ValueError) as exc:
        raise ValidationError(f"Invalid plugin icon {icon!r}: {exc}") from exc


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
    """The extension allow list. SVG icons are allowed; .png, .css and .js still fail here.

    FilaMan checks ``if suffix and suffix not in ALLOWED_EXTENSIONS``, so a file
    with no extension at all passes. That is mirrored deliberately, so this
    check is never stricter than the original.
    """
    for path in files:
        suffix = path.suffix.lower()
        if suffix == ".svg":
            validate_icon(plugin_dir, str(path.relative_to(plugin_dir)), max_bytes=MAX_LEGACY_SVG_BYTES)
        if suffix and suffix not in ALLOWED_EXTENSIONS:
            rel = path.relative_to(plugin_dir)
            raise ValidationError(
                f"Forbidden file extension: {rel} ({suffix}). "
                f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}. "
                "Raster images and script files have to be served at runtime through the "
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

    if manifest.get("icon") is not None:
        validate_icon(plugin_dir, manifest["icon"])

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

    icon = manifest.get("icon")
    if icon is not None and plugin_dir / icon not in files:
        raise ValidationError(f"Declared icon is excluded from the package: {icon}")

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

        # Exercise the same SVG and manifest boundary as the host installer.
        manifest_path = staged / "plugin.json"
        original_manifest = manifest_path.read_text(encoding="utf-8")
        original_icon = (staged / "icon.svg").read_bytes()
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h24v24z"/></svg>'
        failures = []

        def expect_rejected(label: str) -> None:
            try:
                validate(staged)
            except ValidationError:
                return
            failures.append(label)
            print(f"FAILED: {label} should have been rejected")

        for label, content in (
            ("SVG style", b'<svg xmlns="http://www.w3.org/2000/svg"><style>path{fill:red}</style></svg>'),
            ("SVG script", b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'),
            ("SVG event handler", b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'),
            ("SVG reference", b'<svg xmlns="http://www.w3.org/2000/svg"><path fill="url(https://example.com/a)"/></svg>'),
            ("SVG declaration", b'<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg"/>'),
            ("invalid SVG XML", b'<svg'),
            ("invalid SVG encoding", b'\xff'),
            ("declared icon over 4 KiB", svg.ljust(4097)),
        ):
            (staged / "icon.svg").write_bytes(content)
            expect_rejected(label)
        (staged / "icon.svg").write_bytes(svg.ljust(4096))
        validate(staged)
        (staged / "icon.svg").write_bytes(original_icon)

        (staged / "extra.svg").write_bytes(svg.ljust(65536))
        validate(staged)
        (staged / "extra.svg").write_bytes(svg.ljust(65537))
        expect_rejected("undeclared SVG over 64 KiB")
        (staged / "extra.svg").write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><style/></svg>')
        expect_rejected("undeclared invalid SVG")
        (staged / "extra.svg").unlink()

        manifest = json.loads(original_manifest)
        (Path(tmp) / "outside.svg").write_bytes(svg)
        for icon in ("missing.svg", "../outside.svg", "/icon.svg", "assets/../icon.svg", "icon.png", "", 42):
            manifest_path.write_text(json.dumps({**manifest, "icon": icon}), encoding="utf-8")
            expect_rejected(f"icon path {icon!r}")
        (staged / "linked.svg").symlink_to(Path(tmp) / "outside.svg")
        manifest_path.write_text(json.dumps({**manifest, "icon": "linked.svg"}), encoding="utf-8")
        expect_rejected("icon symlink outside package")
        (staged / "linked.svg").unlink()

        (staged / "assets").mkdir()
        excluded_icon = staged / "assets/.git/icon.svg"
        excluded_icon.parent.mkdir()
        excluded_icon.write_bytes(svg)
        manifest_path.write_text(json.dumps({**manifest, "icon": "assets/.git/icon.svg"}), encoding="utf-8")
        expect_rejected("icon excluded from ZIP")
        shutil.rmtree(excluded_icon.parent)
        (staged / "assets/icon.svg").write_bytes(svg)
        manifest_path.write_text(json.dumps({**manifest, "icon": "assets/icon.svg"}), encoding="utf-8")
        validate(staged)
        for legacy in ({**manifest, "icon": None}, {k: v for k, v in manifest.items() if k != "icon"}):
            manifest_path.write_text(json.dumps(legacy), encoding="utf-8")
            validate(staged)
        manifest_path.write_text(original_manifest, encoding="utf-8")
        if failures:
            return 1

        # With a forbidden extension it has to be rejected.
        (staged / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        try:
            validate(staged)
        except ValidationError as exc:
            print("Self test passed.")
            print("  SVG content, size, paths   validated; legacy/nested icons accepted")
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
