#!/usr/bin/env python3
"""Check the layer share parser against a real 3MF.

A plate gcode of tens of megabytes cannot live in this repository, and a parser
for an undocumented format cannot be trusted to a hand written fixture alone.
This runs the real parser over a real file and checks the properties the design
actually relies on:

  * one share per layer, and as many layers as the file's own header claims
  * every curve rises and never falls
  * every curve ends at exactly 1.0

It deliberately does not compare millimetres against the slicer's totals. Those
disagree by design, and why is written down in threemf.layer_shares.

Usage:
    python3 tools/check_gcode.py <file.3mf> [more.3mf ...]
"""

from __future__ import annotations

import io
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bambu_usage import threemf  # noqa: E402

TOTAL_LAYERS = re.compile(r"total layer number\s*:\s*(\d+)")


def declared_layers(archive: Path, plate_id: int) -> int | None:
    """The layer count the gcode states about itself, for an outside opinion."""
    name = threemf.PLATE_GCODE_TEMPLATE.format(plate_id=plate_id)

    with zipfile.ZipFile(archive) as bundle, bundle.open(name) as entry:
        for line in io.TextIOWrapper(entry, encoding="utf-8", errors="replace"):
            if line.startswith("; HEADER_BLOCK_END"):
                return None
            found = TOTAL_LAYERS.search(line)
            if found:
                return int(found.group(1))
    return None


def check(archive: Path) -> bool:
    """Report on one file and say whether it passed."""
    print(f"\n{archive.name}  ({archive.stat().st_size / 1024 / 1024:.1f} MB)")

    metadata = threemf.parse(archive)
    if metadata.plate_id is None:
        print("  no plate in slice_info.config")
        return False

    print(f"  plate {metadata.plate_id}, {len(metadata.filaments)} filament(s) in slice_info")
    for filament in metadata.filaments:
        print(f"    {filament.filament_id}: {filament.material} {filament.used_g} g")

    started = time.time()
    shares = threemf.parse_layer_shares(archive, metadata.plate_id)
    took = time.time() - started

    if not shares:
        print("  no layer shares")
        return False

    stated = declared_layers(archive, metadata.plate_id)
    lengths = {len(curve) for curve in shares.values()}
    print(f"  parsed in {took:.1f}s, {len(shares)} curve(s), {lengths} layers, header says {stated}")

    passed = True
    if len(lengths) != 1:
        print("  FAILED: the curves are of different lengths")
        passed = False
    if stated is not None and lengths and stated not in lengths:
        print(f"  FAILED: {lengths} layers parsed, {stated} declared")
        passed = False

    for filament_id, curve in sorted(shares.items()):
        rises = all(later >= earlier for earlier, later in zip(curve, curve[1:]))
        ends = abs(curve[-1] - 1.0) < 1e-9
        marks = " ".join(f"{curve[at] * 100:.0f}%" for at in _samples(len(curve)))
        print(f"    filament {filament_id}: {marks}")
        if not rises:
            print(f"    FAILED: filament {filament_id} falls somewhere")
            passed = False
        if not ends:
            print(f"    FAILED: filament {filament_id} ends at {curve[-1]}, not at 1.0")
            passed = False

    return passed


def _samples(count: int) -> list[int]:
    """A handful of positions across a curve, for a readable line."""
    if count <= 8:
        return list(range(count))
    return [round(step * (count - 1) / 7) for step in range(8)]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    results = [check(Path(name)) for name in sys.argv[1:]]
    print()
    print("all passed" if all(results) else "SOMETHING FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
