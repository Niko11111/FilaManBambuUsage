# Fixtures

## slice_info.config

**Hand written, not a real capture.** It follows the structure documented in
`docs/03_Bambu_Data_Sources.md` section 3 and the attribute names OpenSpoolMan
reads in `tools_3mf.py`, but no printer produced this file.

Replace it with a genuine `Metadata/slice_info.config` out of a real 3MF as soon
as one is available, and keep the two filament entries so `test_threemf.py`
keeps covering the multi filament case. The expected values in that test are
derived from this file, so they have to move with it.

What the tests rely on:

- one `<plate>` element carrying `<metadata key="index" value="1"/>`
- two `<filament>` entries with 1-based `id`, `type`, `color`, `used_g`,
  `used_m` and `tray_info_idx`
- unrelated elements (`header`, `object`, other `metadata` keys) present, so the
  parser is exercised against noise rather than a stripped down file
