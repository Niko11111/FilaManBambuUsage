"""FilaMan plugin: Bambu Lab print consumption tracking.

Watches Bambu Lab printers for print jobs, reads the per-filament weight
estimate out of the 3MF the printer is running, resolves each slicer filament
to a physical AMS tray and from there to the FilaMan spool sitting in it, and
deducts the consumed grams once the print finishes.

The plugin deliberately does not duplicate anything the Bambu Lab driver
plugin already provides: AMS overview, slot assignment, RFID and auto matching
stay where they are. This plugin only reads that state.

See docs/01_Design.md in the repository for the full design.
"""

__version__ = "0.1.0"
